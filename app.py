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
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # optional

PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # تقدر تغيّره
OPENAI_MAX_IMAGES = int(os.environ.get("OPENAI_MAX_IMAGES", "7"))

if not OPENAI_API_KEY:
    print("[WARN] OPENAI_API_KEY is missing. Vision features will not work.")


# =========================
# OpenAI client
# =========================
oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# Classifications from local JSON
# =========================
CLASSIFICATIONS: Dict[str, Dict[str, str]] = {}


def _norm_ar(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    s = s.replace("ـ", "")
    s = re.sub(r"\s+", " ", s)
    return s


def load_classifications():
    """
    classifications.json format:
      {
        "by_L3": { "L3": {"L1": "...", "L2": "...", "L3": "..."} },
        "_meta": {...}
      }
    """
    global CLASSIFICATIONS
    try:
        with open("classifications.json", "r", encoding="utf-8") as f:
            obj = json.load(f)
        by_l3 = obj.get("by_L3", {})
        CLASSIFICATIONS = {_norm_ar(k): v for k, v in by_l3.items()}
        print(f"[classifications] loaded: {len(CLASSIFICATIONS)}")
    except FileNotFoundError:
        CLASSIFICATIONS = {}
        print("[classifications] classifications.json not found (skip)")
    except Exception as e:
        CLASSIFICATIONS = {}
        print(f"[classifications] failed to load: {e}")


# =========================
# Google Sheets
# =========================
COLUMNS = [
    "DEPARTMENT",          # اسم الورقة (المركز/قسم المستشفى الكبير)
    "SECTION",             # القسم داخل الورقة (العمليات/الطوارئ/مختبر...)
    "ROOM_ID",             # رقم الغرفة
    "ROOM_NAME",           # اسم الغرفة
    "TAG_NUMBER",
    "DESCRIPTION_AR",
    "DESCRIPTION_EN",
    "DESCRIPTION_L1",
    "DESCRIPTION_L2",
    "DESCRIPTION_L3",
    "DESCRIPTION_L4",
    "MANUFACTURER_NAME",
    "SERIAL_NUMBER",
    "MODEL_NUMBER",
]

# ✅ لا يوجد أي حقل إلزامي (التاق اختياري)
REQUIRED_FIELDS: List[str] = []

SEP_CHARS = r":：﹕"
LINE_RE = re.compile(rf"^\s*([^ {SEP_CHARS}]+(?:\s+[^ {SEP_CHARS}]+)*)\s*[{SEP_CHARS}]\s*(.*)\s*$")

KEY_ALIASES = {
    "DEPARTMENT": "DEPARTMENT",
    "المركز": "DEPARTMENT",
    "المنشأة": "DEPARTMENT",
    "اسم المركز": "DEPARTMENT",
    "اسم المنشأة": "DEPARTMENT",

    "SECTION": "SECTION",
    "القسم": "SECTION",
    "قسم": "SECTION",

    "ROOM_ID": "ROOM_ID",
    "رقم الغرفة": "ROOM_ID",
    "رقم الغرفه": "ROOM_ID",

    "ROOM_NAME": "ROOM_NAME",
    "اسم الغرفة": "ROOM_NAME",
    "اسم الغرفه": "ROOM_NAME",

    "TAG_NUMBER": "TAG_NUMBER",
    "TAG": "TAG_NUMBER",
    "TAG NUMBER": "TAG_NUMBER",
    "رقم التاق": "TAG_NUMBER",
    "التاق": "TAG_NUMBER",
    "تاق": "TAG_NUMBER",
    "رقم التاق نمبر": "TAG_NUMBER",
    "تاق نمبر": "TAG_NUMBER",

    "DESCRIPTION_AR": "DESCRIPTION_AR",
    "الوصف عربي": "DESCRIPTION_AR",
    "وصف عربي": "DESCRIPTION_AR",

    "DESCRIPTION_EN": "DESCRIPTION_EN",
    "الوصف انجليزي": "DESCRIPTION_EN",
    "الوصف إنجليزي": "DESCRIPTION_EN",
    "وصف انجليزي": "DESCRIPTION_EN",

    "DESCRIPTION_L1": "DESCRIPTION_L1",
    "المستوى الأول": "DESCRIPTION_L1",
    "المستوى الاول": "DESCRIPTION_L1",
    "L1": "DESCRIPTION_L1",

    "DESCRIPTION_L2": "DESCRIPTION_L2",
    "المستوى الثاني": "DESCRIPTION_L2",
    "L2": "DESCRIPTION_L2",

    "DESCRIPTION_L3": "DESCRIPTION_L3",
    "المستوى الثالث": "DESCRIPTION_L3",
    "L3": "DESCRIPTION_L3",

    "DESCRIPTION_L4": "DESCRIPTION_L4",
    "المستوى الرابع": "DESCRIPTION_L4",
    "L4": "DESCRIPTION_L4",

    "MANUFACTURER_NAME": "MANUFACTURER_NAME",
    "المصنع": "MANUFACTURER_NAME",
    "الشركة المصنعة": "MANUFACTURER_NAME",

    "MODEL_NUMBER": "MODEL_NUMBER",
    "الموديل": "MODEL_NUMBER",

    "SERIAL_NUMBER": "SERIAL_NUMBER",
    "السيريال": "SERIAL_NUMBER",
    "الرقم التسلسلي": "SERIAL_NUMBER",
}


def normalize_key(raw_key: str) -> str:
    k = raw_key.strip()
    k = k.replace("_", " ").replace("-", " ")
    k = re.sub(r"\s+", " ", k)
    k_up = k.upper()

    if k in KEY_ALIASES:
        return KEY_ALIASES[k]
    if k_up in KEY_ALIASES:
        return KEY_ALIASES[k_up]

    return k_up.replace(" ", "_")


def get_gspread_client():
    sa_json = base64.b64decode(GOOGLE_SA_JSON_B64).decode("utf-8")
    sa_info = json.loads(sa_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet, title: str):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(COLUMNS) + 5)
        ws.append_row(COLUMNS, value_input_option="RAW")
        return ws


def ensure_header(ws):
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(COLUMNS, value_input_option="RAW")


def parse_kv(text: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        raw_key = m.group(1).strip()
        val = m.group(2).strip()
        key = normalize_key(raw_key)
        data[key] = val
    return data


def missing_required(data: Dict[str, str]) -> List[str]:
    return [k for k in REQUIRED_FIELDS if not data.get(k)]


def build_row_by_header(ws, data: Dict[str, str]) -> List[str]:
    header = ws.row_values(1) or COLUMNS
    row = []
    for col in header:
        col_norm = col.strip()
        row.append(data.get(col_norm, ""))
    return row


# =========================
# State (avoid mixing)
# =========================
# Place format from user: Hospital=Section=RoomName=RoomID
# Key = (chat_id, user_id)
USER_PLACE: Dict[Tuple[int, int], Dict[str, str]] = {}

# Album photos buffer
# Key = (chat_id, user_id)
# Value = {"photos":[file_id,...], "ts":..., "media_group_id": "..."}
ALBUM_BUFFER: Dict[Tuple[int, int], Dict[str, Any]] = {}

# Pending classification choice after AI
# Key = (chat_id, user_id)
# Value = {"data": {...}, "options": [{"L1":..,"L2":..,"L3":..}, ...], "preview": "..."}
PENDING_CHOICES: Dict[Tuple[int, int], Dict[str, Any]] = {}


def parse_place_line(text: str) -> Optional[Dict[str, str]]:
    """
    Accept: "مستشفى النبهانية=العمليات=ريكفري=C3-127-RM1234"
    """
    if "=" not in text:
        return None
    parts = [p.strip() for p in text.strip().split("=") if p.strip()]
    if len(parts) != 4:
        return None
    return {
        "DEPARTMENT": parts[0],
        "SECTION": parts[1],
        "ROOM_NAME": parts[2],
        "ROOM_ID": parts[3],
    }


# =========================
# L3 shortlist
# =========================
def _tokenize(s: str) -> List[str]:
    s = _norm_ar(s).lower()
    s = re.sub(r"[^\w\u0600-\u06FF]+", " ", s, flags=re.UNICODE)
    toks = [t for t in s.split() if len(t) >= 2]
    return toks


def shortlist_l3(query: str, k: int = 30) -> List[str]:
    if not CLASSIFICATIONS:
        return []

    qt = set(_tokenize(query))
    if not qt:
        return list(CLASSIFICATIONS.keys())[:k]

    scored: List[Tuple[float, str]] = []
    for l3 in CLASSIFICATIONS.keys():
        lt = set(_tokenize(l3))
        if not lt:
            continue
        inter = len(qt & lt)
        union = len(qt | lt) or 1
        score = inter / union
        if any(t in l3 for t in qt):
            score += 0.05
        scored.append((score, l3))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [l3 for score, l3 in scored[:k] if score > 0]
    if not top:
        top = [l3 for _, l3 in scored[:k]]
    return top


# =========================
# OpenAI Vision call
# =========================
def _clean_tag(tag: str) -> str:
    return (tag or "").strip()


def call_vision_extract(images_b64: List[str], l3_candidates: List[str]) -> Dict[str, Any]:
    """
    Returns dict:
      {
        "fields": {...},
        "choices": ["L3_1","L3_2","L3_3"]
      }
    """
    if oa_client is None:
        raise RuntimeError("OPENAI_API_KEY is missing")

    images_b64 = images_b64[:OPENAI_MAX_IMAGES]
    l3_candidates = l3_candidates[:40]

    system = (
        "أنت مساعد متخصص في جرد الأجهزة داخل المستشفيات.\n"
        "استخرج بيانات الجهاز من الصور.\n"
        "ثم اختر أقرب تصنيفات (L3) من القائمة المعطاة فقط.\n"
        "ممنوع اختراع L3 خارج القائمة.\n"
        "أخرج JSON فقط بدون أي كلام خارج JSON."
    )

    user_text = (
        "استخرج الحقول التالية قدر الإمكان (إذا غير واضح اكتب فارغ):\n"
        "- TAG_NUMBER (رقم التاق)\n"
        "- DESCRIPTION_AR (الوصف عربي: نوع الجهاز)\n"
        "- DESCRIPTION_EN (الوصف انجليزي)\n"
        "- MANUFACTURER_NAME (المصنع)\n"
        "- MODEL_NUMBER (الموديل)\n"
        "- SERIAL_NUMBER (السيريال)\n\n"
        "ثم اختر أفضل 1 إلى 4 خيارات L3 من هذه القائمة فقط:\n"
        f"{l3_candidates}\n\n"
        "أعد JSON بهذا الشكل:\n"
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
        "}\n"
        "ملاحظات:\n"
        "- choices لازم تكون من القائمة فقط.\n"
        "- إذا واثق جدًا ضع خيار واحد فقط.\n"
    )

    content = [{"type": "text", "text": user_text}]
    for b64 in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    resp = oa_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        temperature=0.2,
    )

    text = resp.choices[0].message.content or "{}"
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def format_device_model(place: Dict[str, str], fields: Dict[str, str], l1: str, l2: str, l3: str) -> str:
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


async def write_to_sheet(data: Dict[str, str]):
    worksheet_name = (data.get("DEPARTMENT") or "").strip() or DEFAULT_SHEET_NAME
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = get_or_create_worksheet(sh, worksheet_name)
    ensure_header(ws)
    ws.append_row(build_row_by_header(ws, data), value_input_option="RAW")


# =========================
# Telegram handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ جاهز\n\n"
        "1) أرسل سطر المكان بصيغة:\n"
        "مستشفى النبهانية=العمليات=ريكفري=C3-127-RM1234\n\n"
        "2) بعدها أرسل صور الجهاز (ألبوم أو صور متتالية)\n"
        "3) ارسل Done\n\n"
        "سأرجع لك نموذج + خيارات تصنيف (1/2/3...).\n"
        "✅ التاق غير إلزامي (لو ما انقرأ بيطلع فاضي).\n\n"
        "أوامر:\n"
        "/cancel إلغاء الجهاز الحالي (يحذف الصور/الاختيار فقط)\n"
        "/reset مسح المكان الحالي أيضًا\n"
        "/place عرض المكان الحالي\n"
        "/id عرض Chat ID"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"CHAT_ID: {update.effective_chat.id}")


async def cmd_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)
    place = USER_PLACE.get(key)
    if not place:
        await update.message.reply_text("ما تم تحديد مكان بعد. أرسل: مستشفى=قسم=غرفة=رقم")
        return
    await update.message.reply_text(
        "📌 المكان الحالي:\n"
        f"- المركز: {place.get('DEPARTMENT','')}\n"
        f"- القسم: {place.get('SECTION','')}\n"
        f"- الغرفة: {place.get('ROOM_NAME','')}\n"
        f"- رقم الغرفة: {place.get('ROOM_ID','')}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    # ✅ لا نمسح المكان (عشان تكمّل نفس الغرفة)
    ALBUM_BUFFER.pop(key, None)
    PENDING_CHOICES.pop(key, None)

    await update.message.reply_text("✅ تم الإلغاء للجهاز الحالي (الصور/الاختيار).")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    USER_PLACE.pop(key, None)
    ALBUM_BUFFER.pop(key, None)
    PENDING_CHOICES.pop(key, None)

    await update.message.reply_text("✅ تم مسح الحالة بالكامل (بما فيها المكان).")


async def _download_to_b64(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> str:
    file = await context.bot.get_file(file_id)
    b = await file.download_as_bytearray()
    return base64.b64encode(bytes(b)).decode("utf-8")


def _build_options_from_l3_list(l3_list: List[str]) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    for l3 in l3_list[:4]:
        hit = CLASSIFICATIONS.get(l3, {})
        options.append({
            "L1": hit.get("L1", ""),
            "L2": hit.get("L2", ""),
            "L3": hit.get("L3", l3),
        })
    return options


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
            if chat_id != allowed:
                return
        except ValueError:
            pass

    text = update.message.text.strip()

    # (A) Selection pending?
    if key in PENDING_CHOICES:
        m = re.fullmatch(r"\s*(\d{1,2})\s*", text)
        if not m:
            await update.message.reply_text("ارسل رقم الخيار فقط (مثال: 1) أو /cancel.")
            return
        idx = int(m.group(1))
        pending = PENDING_CHOICES[key]
        options = pending.get("options", [])
        if idx < 1 or idx > len(options):
            await update.message.reply_text("رقم غير صحيح. اختر رقم من القائمة أو /cancel.")
            return

        chosen = options[idx - 1]
        data = pending["data"]

        data["DESCRIPTION_L1"] = chosen.get("L1", "")
        data["DESCRIPTION_L2"] = chosen.get("L2", "")
        data["DESCRIPTION_L3"] = chosen.get("L3", "")

        try:
            await write_to_sheet(data)
            await update.message.reply_text("✅ تم الحفظ وكتابة الجهاز في الشيت.")
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ أثناء الكتابة: {e}")

        PENDING_CHOICES.pop(key, None)
        ALBUM_BUFFER.pop(key, None)
        return

    # (B) Place line?
    place = parse_place_line(text)
    if place:
        USER_PLACE[key] = place
        ALBUM_BUFFER.pop(key, None)  # لمنع خلط صور قديمة
        await update.message.reply_text(
            "✅ تم حفظ المكان.\n"
            f"المركز: {place['DEPARTMENT']}\n"
            f"القسم: {place['SECTION']}\n"
            f"الغرفة: {place['ROOM_NAME']}\n"
            f"رقم الغرفة: {place['ROOM_ID']}\n\n"
            "الآن أرسل صور الجهاز ثم Done."
        )
        return

    # (C) Done trigger
    if text.lower() == "done":
        if key not in USER_PLACE:
            await update.message.reply_text("❌ قبل Done لازم ترسل: مستشفى=قسم=غرفة=رقم")
            return

        buf = ALBUM_BUFFER.get(key)
        if not buf or not buf.get("photos"):
            await update.message.reply_text("❌ ما استلمت صور. أرسل صور الجهاز ثم Done.")
            return

        photos = buf["photos"][:OPENAI_MAX_IMAGES]
        try:
            images_b64 = [await _download_to_b64(context, fid) for fid in photos]
        except Exception as e:
            await update.message.reply_text(f"❌ فشل تنزيل صور: {e}")
            return

        place = USER_PLACE[key]

        query_hint = f"{place.get('SECTION','')} {place.get('ROOM_NAME','')} جهاز"
        l3_candidates = shortlist_l3(query_hint, k=35)

        await update.message.reply_text("⏳ جاري قراءة الصور واستخراج البيانات...")

        try:
            result = call_vision_extract(images_b64=images_b64, l3_candidates=l3_candidates)
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ من AI: {e}")
            return

        fields = result.get("fields", {}) or {}
        norm_fields = {
            "TAG_NUMBER": _clean_tag(str(fields.get("TAG_NUMBER", "") or "")),
            "DESCRIPTION_AR": str(fields.get("DESCRIPTION_AR", "") or "").strip(),
            "DESCRIPTION_EN": str(fields.get("DESCRIPTION_EN", "") or "").strip(),
            "MANUFACTURER_NAME": str(fields.get("MANUFACTURER_NAME", "") or "").strip(),
            "MODEL_NUMBER": str(fields.get("MODEL_NUMBER", "") or "").strip(),
            "SERIAL_NUMBER": str(fields.get("SERIAL_NUMBER", "") or "").strip(),
        }

        choices = result.get("choices", []) or []
        seen = set()
        clean_l3: List[str] = []
        for c in choices:
            l3 = _norm_ar(str(c))
            if not l3 or l3 in seen:
                continue
            if l3 in CLASSIFICATIONS:
                seen.add(l3)
                clean_l3.append(l3)

        if not clean_l3:
            clean_l3 = l3_candidates[:3]

        options = _build_options_from_l3_list(clean_l3)

        data_for_sheet = {
            "DEPARTMENT": place.get("DEPARTMENT", "") or DEFAULT_SHEET_NAME,
            "SECTION": place.get("SECTION", ""),
            "ROOM_NAME": place.get("ROOM_NAME", ""),
            "ROOM_ID": place.get("ROOM_ID", ""),

            "TAG_NUMBER": norm_fields["TAG_NUMBER"],
            "DESCRIPTION_AR": norm_fields["DESCRIPTION_AR"],
            "DESCRIPTION_EN": norm_fields["DESCRIPTION_EN"],
            "MANUFACTURER_NAME": norm_fields["MANUFACTURER_NAME"],
            "MODEL_NUMBER": norm_fields["MODEL_NUMBER"],
            "SERIAL_NUMBER": norm_fields["SERIAL_NUMBER"],

            "DESCRIPTION_L1": "",
            "DESCRIPTION_L2": "",
            "DESCRIPTION_L3": "",
            "DESCRIPTION_L4": "",
        }

        # ✅ إذا خيار واحد فقط: نحفظ مباشرة (بدون خطوة إضافية)
        if len(options) == 1:
            chosen = options[0]
            data_for_sheet["DESCRIPTION_L1"] = chosen.get("L1", "")
            data_for_sheet["DESCRIPTION_L2"] = chosen.get("L2", "")
            data_for_sheet["DESCRIPTION_L3"] = chosen.get("L3", "")

            preview = format_device_model(place, norm_fields,
                                         data_for_sheet["DESCRIPTION_L1"],
                                         data_for_sheet["DESCRIPTION_L2"],
                                         data_for_sheet["DESCRIPTION_L3"])
            try:
                await write_to_sheet(data_for_sheet)
                await update.message.reply_text("🧾 النموذج:\n" + preview + "\n✅ تم الحفظ وكتابة الجهاز في الشيت.")
            except Exception as e:
                await update.message.reply_text(f"❌ خطأ أثناء الكتابة: {e}")

            ALBUM_BUFFER.pop(key, None)
            return

        # ✅ أكثر من خيار: نخليها معلّقة وتختار رقم
        first = options[0] if options else {"L1": "", "L2": "", "L3": ""}
        preview = format_device_model(place, norm_fields, first["L1"], first["L2"], first["L3"])

        PENDING_CHOICES[key] = {
            "data": data_for_sheet,
            "options": options,
            "preview": preview,
        }

        lines = []
        lines.append("🧾 نموذج مقترح (مع أول خيار كتجربة):")
        lines.append(preview)
        lines.append("اختر التصنيف بإرسال رقم فقط:")
        for i, opt in enumerate(options, start=1):
            lines.append(f"{i}) {opt.get('L1','')} → {opt.get('L2','')} → {opt.get('L3','')}")
        lines.append("\nمثال: ارسل 1")
        lines.append("للإلغاء: /cancel")
        await update.message.reply_text("\n".join(lines))
        return

    # غير ذلك: تجاهل لتبقى المحادثة نظيفة
    return


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
            if chat_id != allowed:
                return
        except ValueError:
            pass

    # لازم مكان أولاً لمنع الخلط
    if key not in USER_PLACE:
        await update.message.reply_text("قبل الصور لازم ترسل: مستشفى=قسم=غرفة=رقم")
        return

    # إذا في اختيار معلّق لا نستقبل صور جديدة
    if key in PENDING_CHOICES:
        await update.message.reply_text("عندك اختيار تصنيف معلّق. اختر رقم أولاً أو /cancel.")
        return

    best = update.message.photo[-1]
    fid = best.file_id

    media_group_id = update.message.media_group_id  # can be None
    buf = ALBUM_BUFFER.get(key)
    if not buf:
        buf = {"photos": [], "ts": time.time(), "media_group_id": media_group_id}
        ALBUM_BUFFER[key] = buf

    # إذا تغير الألبوم نبدأ جديد لمنع خلط
    if buf.get("media_group_id") and media_group_id and buf.get("media_group_id") != media_group_id:
        buf = {"photos": [], "ts": time.time(), "media_group_id": media_group_id}
        ALBUM_BUFFER[key] = buf

    buf["photos"].append(fid)
    buf["ts"] = time.time()
    buf["media_group_id"] = media_group_id or buf.get("media_group_id")
    return


async def handle_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    بعض الأجهزة ترسل الصورة كـ document (jpg/png/heic...) وليس PHOTO
    """
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()

    is_image = mime.startswith("image/") or fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic"))
    if not is_image:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
            if chat_id != allowed:
                return
        except ValueError:
            pass

    if key not in USER_PLACE:
        await update.message.reply_text("قبل الصور لازم ترسل: مستشفى=قسم=غرفة=رقم")
        return

    if key in PENDING_CHOICES:
        await update.message.reply_text("عندك اختيار تصنيف معلّق. اختر رقم أولاً أو /cancel.")
        return

    fid = doc.file_id
    media_group_id = update.message.media_group_id

    buf = ALBUM_BUFFER.get(key)
    if not buf:
        buf = {"photos": [], "ts": time.time(), "media_group_id": media_group_id}
        ALBUM_BUFFER[key] = buf

    if buf.get("media_group_id") and media_group_id and buf.get("media_group_id") != media_group_id:
        buf = {"photos": [], "ts": time.time(), "media_group_id": media_group_id}
        ALBUM_BUFFER[key] = buf

    buf["photos"].append(fid)
    buf["ts"] = time.time()
    buf["media_group_id"] = media_group_id or buf.get("media_group_id")
    return


def main():
    if not PUBLIC_URL:
        raise RuntimeError("Missing PUBLIC_URL/RENDER_EXTERNAL_URL environment variable")

    load_classifications()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("place", cmd_place))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_image_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{PUBLIC_URL}/webhook",
    )


if __name__ == "__main__":
    main()
