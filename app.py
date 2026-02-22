# =========================
# IMPORTS
# =========================
import os
import json
import base64
import re
import time
from typing import Dict, List, Any, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SA_JSON_B64 = os.environ["GOOGLE_SA_JSON_B64"]

DEFAULT_SHEET_NAME = os.environ.get("DEFAULT_SHEET_NAME", "تجربة")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")

PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")
OPENAI_MAX_IMAGES = int(os.environ.get("OPENAI_MAX_IMAGES", "7"))

oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# CLASSIFICATIONS
# =========================
CLASSIFICATIONS: Dict[str, Dict[str, str]] = {}

def _norm_ar(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip().replace("ـ", "")
    s = re.sub(r"\s+", " ", s)
    return s

def load_classifications():
    global CLASSIFICATIONS
    try:
        with open("classifications.json", "r", encoding="utf-8") as f:
            obj = json.load(f)
        CLASSIFICATIONS = {_norm_ar(k): v for k, v in obj.get("by_L3", {}).items()}
        print("[OK] loaded classifications:", len(CLASSIFICATIONS))
    except Exception as e:
        CLASSIFICATIONS = {}
        print("[ERR] classification load fail:", e)


# =========================
# GOOGLE SHEETS
# =========================
COLUMNS = [
    "DEPARTMENT","SECTION","ROOM_ID","ROOM_NAME","TAG_NUMBER",
    "DESCRIPTION_AR","DESCRIPTION_EN",
    "DESCRIPTION_L1","DESCRIPTION_L2","DESCRIPTION_L3","DESCRIPTION_L4",
    "MANUFACTURER_NAME","SERIAL_NUMBER","MODEL_NUMBER"
]

def get_gspread_client():
    sa = json.loads(base64.b64decode(GOOGLE_SA_JSON_B64).decode("utf-8"))
    creds = Credentials.from_service_account_info(sa, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)

def get_or_create_worksheet(sh, title: str):
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title=title, rows=2000, cols=20)
        ws.append_row(COLUMNS, value_input_option="RAW")
    # تأكد من الهيدر
    first = ws.row_values(1)
    if not first:
        ws.append_row(COLUMNS, value_input_option="RAW")
    return ws

def build_row_by_header(ws, data: Dict[str, str]) -> List[str]:
    header = ws.row_values(1) or COLUMNS
    return [data.get(h, "") for h in header]

async def write_to_sheet(data: Dict[str, str]):
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = get_or_create_worksheet(sh, data.get("DEPARTMENT") or DEFAULT_SHEET_NAME)
    ws.append_row(build_row_by_header(ws, data), value_input_option="RAW")


# =========================
# STATE
# =========================
# Key = (chat_id, user_id)
USER_PLACE: Dict[Tuple[int,int], Dict[str,str]] = {}
ALBUM_BUFFER: Dict[Tuple[int,int], Dict[str,Any]] = {}
PENDING_CHOICES: Dict[Tuple[int,int], Dict[str,Any]] = {}


# =========================
# PARSE PLACE
# =========================
def parse_place_line(text: str) -> Optional[Dict[str,str]]:
    if "=" not in text:
        return None
    p = [x.strip() for x in text.split("=") if x.strip()]
    if len(p) != 4:
        return None
    return dict(DEPARTMENT=p[0], SECTION=p[1], ROOM_NAME=p[2], ROOM_ID=p[3])


# =========================
# SHORTLIST
# =========================
def _tokenize(s: str) -> List[str]:
    s = _norm_ar(s).lower()
    s = re.sub(r"[^\w\u0600-\u06FF]+", " ", s)
    return [t for t in s.split() if len(t) >= 2]

def shortlist_l3(query: str, k: int = 35) -> List[str]:
    if not CLASSIFICATIONS:
        return []
    qt = set(_tokenize(query))
    if not qt:
        return list(CLASSIFICATIONS.keys())[:k]

    scored: List[Tuple[float,str]] = []
    for l3 in CLASSIFICATIONS.keys():
        lt = set(_tokenize(l3))
        if not lt:
            continue
        inter = len(qt & lt)
        union = len(qt | lt) or 1
        score = inter / union
        if any(t in l3.lower() for t in qt):
            score += 0.05
        scored.append((score, l3))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [l3 for score, l3 in scored[:k] if score > 0]
    return top or [l3 for _, l3 in scored[:k]]


# =========================
# OPENAI VISION
# =========================
def _safe_json_loads(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))

def call_vision_extract(images_b64: List[str], l3_candidates: List[str]) -> Dict[str, Any]:
    if oa_client is None:
        raise RuntimeError("OPENAI_API_KEY missing")

    l3_candidates = l3_candidates[:40]
    images_b64 = images_b64[:OPENAI_MAX_IMAGES]

    system = (
        "أنت مساعد جرد أجهزة.\n"
        "استخرج المعلومات من صور الجهاز (التاق/لوحة البيانات/الشكل العام).\n"
        "ثم اختر أقرب L3 من قائمة L3 المعطاة فقط.\n"
        "ممنوع اختراع L3 خارج القائمة.\n"
        "أخرج JSON فقط."
    )

    user = (
        "أعد JSON بهذا الشكل فقط:\n"
        "{\n"
        '  "fields": {\n'
        '    "TAG_NUMBER": "",\n'
        '    "DESCRIPTION_AR": "",\n'
        '    "DESCRIPTION_EN": "",\n'
        '    "MANUFACTURER_NAME": "",\n'
        '    "MODEL_NUMBER": "",\n'
        '    "SERIAL_NUMBER": ""\n'
        "  },\n"
        '  "choices": ["L3_1","L3_2","L3_3"]\n'
        "}\n\n"
        "الشروط:\n"
        "- إذا ما تقدر تقرأ الحقل اتركه فارغ.\n"
        "- choices لازم تكون من قائمة L3 التالية حرفيًا:\n"
        f"{l3_candidates}\n"
        "- إذا واثق جدًا ضع خيار واحد فقط.\n"
    )

    content = [{"type": "text", "text": user}]
    for b in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}})

    # response_format قد لا تدعمه بعض الإصدارات/الموديلات؛ إذا فشل نعيد بدونها
    try:
        r = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        )
    except Exception:
        r = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        )

    t = r.choices[0].message.content or "{}"
    return _safe_json_loads(t)

def format_model(place: Dict[str,str], fields: Dict[str,str], l1: str, l2: str, l3: str) -> str:
    return (
        f"المركز: {place.get('DEPARTMENT','')}\n"
        f"القسم: {place.get('SECTION','')}\n"
        f"اسم الغرفة: {place.get('ROOM_NAME','')}\n"
        f"رقم الغرفة: {place.get('ROOM_ID','')}\n"
        f"رقم التاق: {fields.get('TAG_NUMBER','')}\n"
        f"الوصف عربي: {fields.get('DESCRIPTION_AR','')}\n"
        f"الوصف انجليزي: {fields.get('DESCRIPTION_EN','')}\n"
        f"المصنع: {fields.get('MANUFACTURER_NAME','')}\n"
        f"الموديل: {fields.get('MODEL_NUMBER','')}\n"
        f"السيريال: {fields.get('SERIAL_NUMBER','')}\n"
        f"المستوى الأول: {l1}\n"
        f"المستوى الثاني: {l2}\n"
        f"المستوى الثالث: {l3}\n"
    )


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "جاهز ✅\n\n"
        "أرسل المكان:\nمستشفى=قسم=غرفة=رقم\n"
        "ثم صوّر الجهاز (صور متعددة) ثم ارسل Done\n\n"
        "اكتب: جاهز  ← يعطيك الحالة\n"
        "/cancel يلغي الجهاز الحالي (الصور/الاختيار)\n"
        "/reset يمسح المكان أيضًا\n"
        "/place يعرض المكان\n"
    )

async def cmd_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = (update.effective_chat.id, update.effective_user.id)
    p = USER_PLACE.get(key)
    await update.message.reply_text(
        "📌 المكان الحالي:\n" +
        (f"{p['DEPARTMENT']} / {p['SECTION']} / {p['ROOM_NAME']} / {p['ROOM_ID']}" if p else "مافي مكان محفوظ")
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = (update.effective_chat.id, update.effective_user.id)
    ALBUM_BUFFER.pop(key, None)
    PENDING_CHOICES.pop(key, None)
    await update.message.reply_text("✅ تم إلغاء الجهاز الحالي (الصور/الاختيار).")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = (update.effective_chat.id, update.effective_user.id)
    USER_PLACE.pop(key, None)
    ALBUM_BUFFER.pop(key, None)
    PENDING_CHOICES.pop(key, None)
    await update.message.reply_text("✅ تم مسح كل الحالة (بما فيها المكان).")


# =========================
# HANDLE TEXT
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    key = (update.effective_chat.id, update.effective_user.id)
    text = update.message.text.strip()

    # جاهز (حالة)
    if text.lower() == "جاهز":
        p = USER_PLACE.get(key)
        buf = ALBUM_BUFFER.get(key, {})
        n = len(buf.get("photos", []))
        if not p:
            await update.message.reply_text("أنا جاهز ✅ لكن ما حددت المكان.\nأرسل: مستشفى=قسم=غرفة=رقم")
        else:
            await update.message.reply_text(
                "أنا جاهز ✅\n"
                f"المكان: {p['DEPARTMENT']} / {p['SECTION']} / {p['ROOM_NAME']} / {p['ROOM_ID']}\n"
                f"عدد الصور المستلمة للجهاز الحالي: {n}\n"
                "أرسل صور ثم Done."
            )
        return

    # اختيار تصنيف
    if key in PENDING_CHOICES:
        if not text.isdigit():
            await update.message.reply_text("ارسل رقم الخيار فقط (مثال: 1) أو /cancel")
            return
        idx = int(text) - 1
        pending = PENDING_CHOICES[key]
        options = pending.get("options", [])
        if idx < 0 or idx >= len(options):
            await update.message.reply_text("رقم غير صحيح. اختر من القائمة أو /cancel")
            return

        opt = options[idx]
        data = pending["data"]
        data["DESCRIPTION_L1"] = opt["L1"]
        data["DESCRIPTION_L2"] = opt["L2"]
        data["DESCRIPTION_L3"] = opt["L3"]

        try:
            await write_to_sheet(data)
            await update.message.reply_text("✅ تم الحفظ في الشيت.")
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ أثناء الحفظ: {e}")

        PENDING_CHOICES.pop(key, None)
        ALBUM_BUFFER.pop(key, None)
        return

    # مكان
    place = parse_place_line(text)
    if place:
        USER_PLACE[key] = place
        ALBUM_BUFFER.pop(key, None)
        PENDING_CHOICES.pop(key, None)
        await update.message.reply_text(
            "✅ تم حفظ المكان.\n"
            f"{place['DEPARTMENT']} / {place['SECTION']} / {place['ROOM_NAME']} / {place['ROOM_ID']}\n"
            "الآن أرسل صور الجهاز ثم Done."
        )
        return

    # Done
    if text.lower() == "done":
        try:
            if key not in USER_PLACE:
                await update.message.reply_text("❌ ارسل المكان أولاً: مستشفى=قسم=غرفة=رقم")
                return

            buf = ALBUM_BUFFER.get(key)
            photos = (buf or {}).get("photos", [])
            if not photos:
                await update.message.reply_text("❌ ما استلمت صور. أرسل صور الجهاز ثم Done.")
                return

            await update.message.reply_text("⏳ جاري تنزيل الصور وتحليلها...")

            images_b64: List[str] = []
            for fid in photos[:OPENAI_MAX_IMAGES]:
                f = await context.bot.get_file(fid)
                b = await f.download_as_bytearray()
                images_b64.append(base64.b64encode(bytes(b)).decode("utf-8"))

            # المرحلة 1: استخراج fields بدون فلترة قوية (نعطي L3 عامة)
            base_l3 = list(CLASSIFICATIONS.keys())[:40] if CLASSIFICATIONS else []
            r1 = call_vision_extract(images_b64, base_l3)
            fields = (r1.get("fields") or {})

            # hint للتصنيف من الوصف (أهم من الغرفة)
            hint = f"{fields.get('DESCRIPTION_AR','')} {fields.get('DESCRIPTION_EN','')} {fields.get('MANUFACTURER_NAME','')}"
            hint = hint.strip() or "جهاز طبي"

            l3_short = shortlist_l3(hint, 35)
            r2 = call_vision_extract(images_b64, l3_short)

            fields = (r2.get("fields") or {})
            # تنظيف الحقول
            fields_norm = {
                "TAG_NUMBER": str(fields.get("TAG_NUMBER","") or "").strip(),
                "DESCRIPTION_AR": str(fields.get("DESCRIPTION_AR","") or "").strip(),
                "DESCRIPTION_EN": str(fields.get("DESCRIPTION_EN","") or "").strip(),
                "MANUFACTURER_NAME": str(fields.get("MANUFACTURER_NAME","") or "").strip(),
                "MODEL_NUMBER": str(fields.get("MODEL_NUMBER","") or "").strip(),
                "SERIAL_NUMBER": str(fields.get("SERIAL_NUMBER","") or "").strip(),
            }

            # choices من AI
            raw_choices = r2.get("choices") or []
            clean_choices: List[str] = []
            seen = set()
            for c in raw_choices:
                l3 = _norm_ar(str(c))
                if not l3 or l3 in seen:
                    continue
                if l3 in CLASSIFICATIONS:
                    seen.add(l3)
                    clean_choices.append(l3)

            if not clean_choices:
                clean_choices = [l3 for l3 in l3_short[:3] if l3 in CLASSIFICATIONS] or l3_short[:3]

            options: List[Dict[str,str]] = []
            for c in clean_choices[:4]:
                h = CLASSIFICATIONS.get(c, {})
                options.append({
                    "L1": h.get("L1",""),
                    "L2": h.get("L2",""),
                    "L3": h.get("L3", c),
                })

            place = USER_PLACE[key]
            data = dict(
                DEPARTMENT=place["DEPARTMENT"],
                SECTION=place["SECTION"],
                ROOM_NAME=place["ROOM_NAME"],
                ROOM_ID=place["ROOM_ID"],

                TAG_NUMBER=fields_norm["TAG_NUMBER"],
                DESCRIPTION_AR=fields_norm["DESCRIPTION_AR"],
                DESCRIPTION_EN=fields_norm["DESCRIPTION_EN"],
                MANUFACTURER_NAME=fields_norm["MANUFACTURER_NAME"],
                MODEL_NUMBER=fields_norm["MODEL_NUMBER"],
                SERIAL_NUMBER=fields_norm["SERIAL_NUMBER"],

                DESCRIPTION_L1="",
                DESCRIPTION_L2="",
                DESCRIPTION_L3="",
                DESCRIPTION_L4="",
            )

            # دائماً نطلب اختيار (لا حفظ مباشر)
            first = options[0] if options else {"L1":"","L2":"","L3":""}
            preview = format_model(place, fields_norm, first["L1"], first["L2"], first["L3"])

            PENDING_CHOICES[key] = dict(data=data, options=options)

            txt = "🧾 نموذج:\n" + preview + "\nاختر رقم:\n"
            for i, o in enumerate(options, 1):
                txt += f"{i}) {o['L1']} → {o['L2']} → {o['L3']}\n"
            txt += "\n(إذا تبغى تلغي: /cancel)"
            await update.message.reply_text(txt)

        except Exception as e:
            # هذا أهم شيء لمنع “السكوت”
            await update.message.reply_text(f"❌ صار خطأ بعد Done:\n{type(e).__name__}: {e}")
            # لا نمسح الصور هنا عشان تقدر تعيد Done بعد التصحيح
        return

    # غير ذلك تجاهل
    return


# =========================
# HANDLE PHOTO / DOC
# =========================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    key = (update.effective_chat.id, update.effective_user.id)

    if key not in USER_PLACE:
        await update.message.reply_text("ارسل المكان أولاً: مستشفى=قسم=غرفة=رقم")
        return

    if key in PENDING_CHOICES:
        await update.message.reply_text("عندك اختيار معلّق. اختر رقم أو /cancel")
        return

    fid = update.message.photo[-1].file_id
    buf = ALBUM_BUFFER.setdefault(key, {"photos": [], "ts": time.time()})
    buf["photos"].append(fid)
    buf["ts"] = time.time()

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()
    if not (mime.startswith("image/") or fname.endswith((".jpg",".jpeg",".png",".webp",".heic"))):
        return

    key = (update.effective_chat.id, update.effective_user.id)

    if key not in USER_PLACE:
        await update.message.reply_text("ارسل المكان أولاً: مستشفى=قسم=غرفة=رقم")
        return

    if key in PENDING_CHOICES:
        await update.message.reply_text("عندك اختيار معلّق. اختر رقم أو /cancel")
        return

    buf = ALBUM_BUFFER.setdefault(key, {"photos": [], "ts": time.time()})
    buf["photos"].append(doc.file_id)
    buf["ts"] = time.time()


# =========================
# MAIN
# =========================
def main():
    if not PUBLIC_URL:
        raise RuntimeError("Missing PUBLIC_URL / RENDER_EXTERNAL_URL")

    load_classifications()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("place", cmd_place))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{PUBLIC_URL}/webhook",
    )

if __name__ == "__main__":
    main()
