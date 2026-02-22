import os
import json
import base64
import re
from typing import Dict, List

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
DEFAULT_SHEET_NAME = os.environ.get("DEFAULT_SHEET_NAME", "تجربة")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # optional

PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

GOOGLE_SA_JSON_B64 = os.environ["GOOGLE_SA_JSON_B64"]

# -----------------------
# Classifications (L3 -> L2/L1) from local JSON
# -----------------------
CLASSIFICATIONS: Dict[str, Dict[str, str]] = {}

def _norm_ar(s: str) -> str:
    """Normalize Arabic safely (no guessing): trims + removes tatweel + normalizes spaces."""
    if s is None:
        return ""
    s = str(s).strip()
    s = s.replace("ـ", "")              # tatweel
    s = re.sub(r"\s+", " ", s)          # collapse spaces
    return s

def load_classifications():
    """
    Expects classifications.json in project root.
    Format:
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

# ✅ الأعمدة
COLUMNS = [
    "DEPARTMENT",          # اسم الورقة (المركز/قسم المستشفى الكبير)
    "SECTION",             # القسم داخل الورقة (الإدارة/أسنان/تطعيمات...)
    "ROOM_ID",
    "ROOM_NAME",
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

# ✅ التاق لم يعد شرطًا نهائيًا
REQUIRED_FIELDS: List[str] = []

# -----------------------
# Very tolerant parsing
# -----------------------
SEP_CHARS = r":：﹕"  # ":" + fullwidth + common variants
LINE_RE = re.compile(rf"^\s*([^ {SEP_CHARS}]+(?:\s+[^ {SEP_CHARS}]+)*)\s*[{SEP_CHARS}]\s*(.*)\s*$")

KEY_ALIASES = {
    # Worksheet name (facility / main department)
    "DEPARTMENT": "DEPARTMENT",
    "المركز": "DEPARTMENT",
    "المنشأة": "DEPARTMENT",
    "اسم المركز": "DEPARTMENT",
    "اسم المنشأة": "DEPARTMENT",

    # Section (داخل الورقة)
    "SECTION": "SECTION",
    "القسم": "SECTION",
    "قسم": "SECTION",
    "الادارة": "SECTION",
    "الإدارة": "SECTION",

    # Room
    "ROOM_ID": "ROOM_ID",
    "ROOM ID": "ROOM_ID",
    "رقم الغرفة": "ROOM_ID",
    "رقم الغرفه": "ROOM_ID",

    "ROOM_NAME": "ROOM_NAME",
    "ROOM NAME": "ROOM_NAME",
    "اسم الغرفة": "ROOM_NAME",
    "اسم الغرفه": "ROOM_NAME",

    # Tag
    "TAG_NUMBER": "TAG_NUMBER",
    "TAG NUMBER": "TAG_NUMBER",
    "TAG": "TAG_NUMBER",
    "TAG NO": "TAG_NUMBER",
    "TAG#": "TAG_NUMBER",
    "رقم التاق": "TAG_NUMBER",
    "التاق": "TAG_NUMBER",
    "تاق": "TAG_NUMBER",
    "رقم التاق نمبر": "TAG_NUMBER",
    "تاق نمبر": "TAG_NUMBER",

    # Descriptions
    "DESCRIPTION_AR": "DESCRIPTION_AR",
    "DESCRIPTION AR": "DESCRIPTION_AR",
    "DESC AR": "DESCRIPTION_AR",
    "الوصف عربي": "DESCRIPTION_AR",
    "وصف عربي": "DESCRIPTION_AR",

    "DESCRIPTION_EN": "DESCRIPTION_EN",
    "DESCRIPTION EN": "DESCRIPTION_EN",
    "DESC EN": "DESCRIPTION_EN",
    "الوصف انجليزي": "DESCRIPTION_EN",
    "الوصف إنجليزي": "DESCRIPTION_EN",
    "وصف انجليزي": "DESCRIPTION_EN",

    # Levels
    "DESCRIPTION_L1": "DESCRIPTION_L1",
    "L1": "DESCRIPTION_L1",
    "LEVEL1": "DESCRIPTION_L1",
    "LEVEL 1": "DESCRIPTION_L1",
    "المستوى الاول": "DESCRIPTION_L1",
    "المستوى الأول": "DESCRIPTION_L1",

    "DESCRIPTION_L2": "DESCRIPTION_L2",
    "L2": "DESCRIPTION_L2",
    "LEVEL2": "DESCRIPTION_L2",
    "LEVEL 2": "DESCRIPTION_L2",
    "المستوى الثاني": "DESCRIPTION_L2",
    "المستوى الثاني ": "DESCRIPTION_L2",

    "DESCRIPTION_L3": "DESCRIPTION_L3",
    "L3": "DESCRIPTION_L3",
    "LEVEL3": "DESCRIPTION_L3",
    "LEVEL 3": "DESCRIPTION_L3",
    "المستوى الثالث": "DESCRIPTION_L3",

    "DESCRIPTION_L4": "DESCRIPTION_L4",
    "L4": "DESCRIPTION_L4",
    "LEVEL4": "DESCRIPTION_L4",
    "LEVEL 4": "DESCRIPTION_L4",
    "المستوى الرابع": "DESCRIPTION_L4",

    # Manufacturer / Serial / Model
    "MANUFACTURER_NAME": "MANUFACTURER_NAME",
    "MANUFACTURER NAME": "MANUFACTURER_NAME",
    "MANUFACTURER": "MANUFACTURER_NAME",
    "الشركة المصنعة": "MANUFACTURER_NAME",
    "المصنع": "MANUFACTURER_NAME",

    "SERIAL_NUMBER": "SERIAL_NUMBER",
    "SERIAL NUMBER": "SERIAL_NUMBER",
    "SERIAL": "SERIAL_NUMBER",
    "SERIAL NO": "SERIAL_NUMBER",
    "الرقم التسلسلي": "SERIAL_NUMBER",
    "السيريال": "SERIAL_NUMBER",

    "MODEL_NUMBER": "MODEL_NUMBER",
    "MODEL NUMBER": "MODEL_NUMBER",
    "MODEL": "MODEL_NUMBER",
    "MODEL NO": "MODEL_NUMBER",
    "الموديل": "MODEL_NUMBER",
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

# -----------------------
# Google Sheets client
# -----------------------
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

def looks_like_device_message(text: str, data: Dict[str, str]) -> bool:
    """
    ✅ بدل الاعتماد على التاق:
    نعتبرها رسالة جهاز إذا فيها على الأقل (2) حقول من حقول النموذج المعروفة
    أو إذا فيها كلمة من عناوين النموذج.
    """
    # وجود أي من هذه الكلمات غالبًا يعني نموذج
    hints_in_text = [
        "المركز", "القسم", "اسم الغرفة", "رقم الغرفة",
        "رقم التاق", "الوصف عربي", "الوصف انجليزي",
        "المصنع", "الموديل", "السيريال",
        "المستوى الأول", "المستوى الثاني", "المستوى الثالث",
        "Tag Number", "Serial", "Model", "Manufacturer",
    ]
    if any(h in text for h in hints_in_text):
        return True

    # أو وجود مفاتيح parsed من النموذج (بدون اشتراط التاق)
    model_keys = {
        "DEPARTMENT", "SECTION", "ROOM_ID", "ROOM_NAME",
        "TAG_NUMBER", "DESCRIPTION_AR", "DESCRIPTION_EN",
        "MANUFACTURER_NAME", "MODEL_NUMBER", "SERIAL_NUMBER",
        "DESCRIPTION_L1", "DESCRIPTION_L2", "DESCRIPTION_L3", "DESCRIPTION_L4",
    }
    present = sum(1 for k in model_keys if (data.get(k) is not None and str(data.get(k)).strip() != ""))
    return present >= 2

def build_row_by_header(ws, data: Dict[str, str]) -> List[str]:
    """
    نكتب حسب هيدر الورقة الفعلي حتى لو كانت الورقة القديمة ما فيها SECTION
    """
    header = ws.row_values(1)
    if not header:
        header = COLUMNS

    if not data.get("DEPARTMENT"):
        data["DEPARTMENT"] = DEFAULT_SHEET_NAME

    if not data.get("SECTION"):
        data["SECTION"] = ""

    # Auto-fill L1/L2 from L3 using classifications.json
    l3 = _norm_ar(data.get("DESCRIPTION_L3", ""))
    l1 = _norm_ar(data.get("DESCRIPTION_L1", ""))
    l2 = _norm_ar(data.get("DESCRIPTION_L2", ""))

    if l3 and (not l1 or not l2):
        hit = CLASSIFICATIONS.get(l3)
        if hit:
            data["DESCRIPTION_L1"] = hit.get("L1", data.get("DESCRIPTION_L1", ""))
            data["DESCRIPTION_L2"] = hit.get("L2", data.get("DESCRIPTION_L2", ""))
            data["DESCRIPTION_L3"] = hit.get("L3", data.get("DESCRIPTION_L3", ""))

    row = []
    for col in header:
        col_norm = col.strip()
        row.append(data.get(col_norm, ""))
    return row

# -----------------------
# Telegram handlers
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "جاهز ✅\n"
        "ألصق نموذج الجهاز داخل القروب بصيغة KEY: VALUE.\n"
        "✅ التاق اختياري (لو ما انقرأ أو غير موجود، يظل فاضي).\n"
        "يدعم عربي/انجليزي و(:) أو (：).\n"
        "لإظهار Chat ID اكتب /id"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"CHAT_ID: {update.effective_chat.id}")

async def _write_to_sheet(update: Update, data: Dict[str, str]):
    worksheet_name = (data.get("DEPARTMENT") or "").strip() or DEFAULT_SHEET_NAME
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = get_or_create_worksheet(sh, worksheet_name)
        ensure_header(ws)

        ws.append_row(build_row_by_header(ws, data), value_input_option="RAW")

        sec = (data.get("SECTION") or "").strip()
        if sec:
            await update.message.reply_text(f"✅ تمت الإضافة إلى ورقة: {worksheet_name}\n📌 القسم: {sec}")
        else:
            await update.message.reply_text(f"✅ تمت الإضافة إلى ورقة: {worksheet_name}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ أثناء الإضافة: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id

    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
            if chat_id != allowed:
                return
        except ValueError:
            pass

    text = update.message.text.strip()
    data = parse_kv(text)

    # ✅ ما عاد نعتمد على التاق لمعرفة الرسالة
    if not looks_like_device_message(text, data):
        return

    missing = missing_required(data)
    if missing:
        await update.message.reply_text("❌ حقول ناقصة:\n- " + "\n- ".join(missing))
        return

    await _write_to_sheet(update, data)

def main():
    if not PUBLIC_URL:
        raise RuntimeError("Missing PUBLIC_URL/RENDER_EXTERNAL_URL environment variable")

    load_classifications()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
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
