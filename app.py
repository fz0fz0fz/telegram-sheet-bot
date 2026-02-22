# =========================
# IMPORTS
# =========================
import os
import json
import base64
import re
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
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")  # نص فقط (خفيف)
OPENAI_SHORTLIST_K = int(os.environ.get("OPENAI_SHORTLIST_K", "40"))  # كم L3 نرسلها للموديل
OPENAI_MAX_CHOICES = int(os.environ.get("OPENAI_MAX_CHOICES", "5"))   # الحد الأعلى للخيارات
OPENAI_MIN_CHOICES = int(os.environ.get("OPENAI_MIN_CHOICES", "3"))   # الحد الأدنى للخيارات

oa_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# CLASSIFICATIONS (L3 -> L1/L2/L3)
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
        ws = sh.add_worksheet(title=title, rows=2000, cols=25)
        ws.append_row(COLUMNS, value_input_option="RAW")

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

    ws_title = (data.get("DEPARTMENT") or "").strip() or DEFAULT_SHEET_NAME
    ws = get_or_create_worksheet(sh, ws_title)

    ws.append_row(build_row_by_header(ws, data), value_input_option="RAW")


# =========================
# PARSING USER MODEL (KEY: VALUE)
# =========================
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

    "ROOM_NAME": "ROOM_NAME",
    "اسم الغرفة": "ROOM_NAME",
    "اسم الغرفه": "ROOM_NAME",

    "ROOM_ID": "ROOM_ID",
    "رقم الغرفة": "ROOM_ID",
    "رقم الغرفه": "ROOM_ID",

    "TAG_NUMBER": "TAG_NUMBER",
    "رقم التاق": "TAG_NUMBER",
    "التاق": "TAG_NUMBER",
    "تاق": "TAG_NUMBER",
    "TAG": "TAG_NUMBER",
    "TAG NUMBER": "TAG_NUMBER",

    "DESCRIPTION_AR": "DESCRIPTION_AR",
    "الوصف عربي": "DESCRIPTION_AR",
    "وصف عربي": "DESCRIPTION_AR",

    "DESCRIPTION_EN": "DESCRIPTION_EN",
    "الوصف انجليزي": "DESCRIPTION_EN",
    "الوصف إنجليزي": "DESCRIPTION_EN",
    "وصف انجليزي": "DESCRIPTION_EN",

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

def looks_like_model(text: str, data: Dict[str, str]) -> bool:
    # يكفي وجود "المركز:" أو "الوصف عربي:" أو "رقم التاق:" إلخ
    if "المركز" in text or "الوصف عربي" in text or "رقم التاق" in text:
        return True
    for k in ("DEPARTMENT","DESCRIPTION_AR","TAG_NUMBER","ROOM_NAME","ROOM_ID"):
        if k in data:
            return True
    return False


# =========================
# SHORTLIST (local scoring)
# =========================
def _tokenize(s: str) -> List[str]:
    s = _norm_ar(s).lower()
    s = re.sub(r"[^\w\u0600-\u06FF]+", " ", s)
    return [t for t in s.split() if len(t) >= 2]

def shortlist_l3_local(query: str, k: int = 40) -> List[str]:
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

        # bonus substring
        l3_low = l3.lower()
        if any(t in l3_low for t in qt):
            score += 0.05

        scored.append((score, l3))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [l3 for score, l3 in scored[:k] if score > 0]
    return top or [l3 for _, l3 in scored[:k]]


# =========================
# OPENAI (TEXT ONLY) to rank L3 shortlist
# =========================
def _safe_json_loads(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))

def call_openai_pick_l3(device_text: str, l3_candidates: List[str]) -> Dict[str, Any]:
    """
    returns JSON:
    {
      "choices": [
        {"l3":"...", "confidence":0.92, "reason":"..."},
        ...
      ]
    }
    """
    if oa_client is None:
        raise RuntimeError("OPENAI_API_KEY missing")

    l3_candidates = l3_candidates[:OPENAI_SHORTLIST_K]

    system = (
        "أنت مساعد تصنيف أصول/أجهزة في المستشفيات.\n"
        "ستستلم وصف جهاز (نص) وقائمة L3 رسمية.\n"
        "مهمتك اختيار أقرب L3 من القائمة فقط.\n"
        "ممنوع اختراع L3 خارج القائمة.\n"
        "أخرج JSON فقط."
    )

    # نطلب 3-5 حسب الثقة (لو واثق خفف الخيارات)
    user = (
        "وصف الجهاز (قد يحتوي عربي/انجليزي):\n"
        f"{device_text}\n\n"
        "قائمة L3 الرسمية (اختر منها فقط):\n"
        f"{l3_candidates}\n\n"
        "أعد JSON بهذا الشكل فقط:\n"
        "{\n"
        '  "choices": [\n'
        '    {"l3":"", "confidence":0.0, "reason":""}\n'
        "  ]\n"
        "}\n\n"
        "قواعد مهمة:\n"
        f"- عدد الخيارات بين {OPENAI_MIN_CHOICES} و {OPENAI_MAX_CHOICES}.\n"
        "- إذا الثقة عالية جدًا، قلل الخيارات (مثلاً 3).\n"
        "- إذا فيه التباس، زد الخيارات (حتى 5).\n"
        "- confidence رقم بين 0 و 1.\n"
        "- reason مختصر جدًا (سطر واحد).\n"
        "- كل l3 لازم يكون مطابق حرفيًا لأحد عناصر القائمة.\n"
    )

    try:
        r = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception:
        r = oa_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

    t = r.choices[0].message.content or "{}"
    return _safe_json_loads(t)


# =========================
# STATE
# =========================
# Pending choice: user must pick number before save
# key=(chat_id,user_id) -> {"data":..., "options":[{L1,L2,L3,confidence,reason}...]}
PENDING_CHOICES: Dict[Tuple[int, int], Dict[str, Any]] = {}


# =========================
# HELPERS
# =========================
def build_device_text_for_classification(data: Dict[str, str]) -> str:
    parts = []
    for k in ("DESCRIPTION_AR", "DESCRIPTION_EN", "MANUFACTURER_NAME", "MODEL_NUMBER", "SERIAL_NUMBER", "TAG_NUMBER"):
        v = (data.get(k) or "").strip()
        if v:
            parts.append(f"{k}: {v}")
    # fallback minimal
    return "\n".join(parts) if parts else "جهاز (الوصف غير متوفر)"

def build_options_from_l3(ai_choices: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    seen = set()
    for item in ai_choices:
        l3 = _norm_ar(str(item.get("l3", "") or ""))
        if not l3 or l3 in seen:
            continue
        if l3 not in CLASSIFICATIONS:
            continue
        seen.add(l3)
        hit = CLASSIFICATIONS.get(l3, {})
        conf = item.get("confidence", "")
        reason = str(item.get("reason", "") or "").strip()
        options.append({
            "L1": hit.get("L1", ""),
            "L2": hit.get("L2", ""),
            "L3": hit.get("L3", l3),
            "CONF": str(conf),
            "REASON": reason,
        })
        if len(options) >= OPENAI_MAX_CHOICES:
            break
    return options


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ جاهز للتصنيف والحفظ\n\n"
        "الطريقة:\n"
        "1) ألصق نموذج الجهاز كامل (KEY: VALUE) مثل:\n"
        "المركز: ...\nالقسم: ...\nاسم الغرفة: ...\nرقم الغرفة: ...\nالوصف عربي: ...\n...\n\n"
        "2) سأعطيك 3 إلى 5 تصنيفات (L1→L2→L3)\n"
        "3) أرسل رقم الخيار فقط (مثال: 1)\n\n"
        "أوامر:\n"
        "/cancel يلغي الاختيار المعلّق\n"
        "/id يعرض Chat ID"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"CHAT_ID: {update.effective_chat.id}")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = (update.effective_chat.id, update.effective_user.id)
    if key in PENDING_CHOICES:
        PENDING_CHOICES.pop(key, None)
        await update.message.reply_text("✅ تم الإلغاء.")
    else:
        await update.message.reply_text("ما فيه اختيار معلّق.")


# =========================
# MAIN TEXT HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    # Optional restriction
    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
            if chat_id != allowed:
                return
        except ValueError:
            pass

    text = update.message.text.strip()

    # 1) pending selection
    if key in PENDING_CHOICES:
        if not re.fullmatch(r"\s*\d{1,2}\s*", text):
            await update.message.reply_text("ارسل رقم الخيار فقط (مثال: 1) أو /cancel")
            return

        idx = int(text.strip()) - 1
        pending = PENDING_CHOICES[key]
        options = pending.get("options", [])
        if idx < 0 or idx >= len(options):
            await update.message.reply_text("رقم غير صحيح. اختر رقم من القائمة أو /cancel")
            return

        chosen = options[idx]
        data = pending["data"]
        data["DESCRIPTION_L1"] = chosen.get("L1", "")
        data["DESCRIPTION_L2"] = chosen.get("L2", "")
        data["DESCRIPTION_L3"] = chosen.get("L3", "")

        try:
            await write_to_sheet(data)
            await update.message.reply_text("✅ تم الحفظ في الشيت.")
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ أثناء الحفظ: {type(e).__name__}: {e}")

        PENDING_CHOICES.pop(key, None)
        return

    # 2) parse model
    data = parse_kv(text)
    if not looks_like_model(text, data):
        return  # keep chat clean

    # ensure worksheet fallback
    if not (data.get("DEPARTMENT") or "").strip():
        data["DEPARTMENT"] = DEFAULT_SHEET_NAME

    # clear classification fields (always overwritten by choice)
    data["DESCRIPTION_L1"] = data.get("DESCRIPTION_L1", "")
    data["DESCRIPTION_L2"] = data.get("DESCRIPTION_L2", "")
    data["DESCRIPTION_L3"] = data.get("DESCRIPTION_L3", "")
    data["DESCRIPTION_L4"] = data.get("DESCRIPTION_L4", "")

    # Build text for classification
    device_text = build_device_text_for_classification(data)

    # Local shortlist first (saves tokens)
    l3_candidates = shortlist_l3_local(device_text, k=OPENAI_SHORTLIST_K if OPENAI_SHORTLIST_K > 0 else 40)

    if not l3_candidates:
        await update.message.reply_text("❌ ملف التصنيفات غير محمّل أو فارغ. تأكد من classifications.json.")
        return

    await update.message.reply_text("⏳ جاري تحديد أقرب التصنيفات...")

    try:
        ai = call_openai_pick_l3(device_text=device_text, l3_candidates=l3_candidates)
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ من OpenAI: {type(e).__name__}: {e}")
        return

    ai_choices = ai.get("choices", []) or []
    options = build_options_from_l3(ai_choices)

    # fallback إذا خرجت خيارات غير صالحة
    if not options:
        # fallback: أول 5 من shortlist
        options = []
        for l3 in l3_candidates[:OPENAI_MAX_CHOICES]:
            hit = CLASSIFICATIONS.get(l3, {})
            options.append({
                "L1": hit.get("L1",""),
                "L2": hit.get("L2",""),
                "L3": hit.get("L3", l3),
                "CONF": "",
                "REASON": "fallback",
            })

    # store pending
    PENDING_CHOICES[key] = {"data": data, "options": options}

    # reply
    lines = []
    lines.append("اختر رقم التصنيف فقط:")
    for i, opt in enumerate(options, start=1):
        conf = opt.get("CONF", "")
        reason = opt.get("REASON", "")
        conf_txt = f" (ثقة: {conf})" if conf not in ("", None) else ""
        reason_txt = f" — {reason}" if reason else ""
        lines.append(f"{i}) {opt.get('L1','')} → {opt.get('L2','')} → {opt.get('L3','')}{conf_txt}{reason_txt}")
    lines.append("\nمثال: ارسل 1")
    lines.append("(للإلغاء: /cancel)")
    await update.message.reply_text("\n".join(lines))


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
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{PUBLIC_URL}/webhook",
    )

if __name__ == "__main__":
    main()
