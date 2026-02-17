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

# Render usually provides this automatically for Web Services
PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

GOOGLE_SA_JSON_B64 = os.environ["GOOGLE_SA_JSON_B64"]

COLUMNS = [
    "DEPARTMENT",
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

# Keep it minimal (very tolerant)
REQUIRED_FIELDS = ["TAG_NUMBER"]

# -----------------------
# Very tolerant parsing
# -----------------------
SEP_CHARS = r":：﹕"  # ":" + fullwidth + common variants
LINE_RE = re.compile(rf"^\s*([^ {SEP_CHARS}]+(?:\s+[^ {SEP_CHARS}]+)*)\s*[{SEP_CHARS}]\s*(.*)\s*$")

KEY_ALIASES = {
    # Department
    "DEPARTMENT": "DEPARTMENT",
    "القسم": "DEPARTMENT",

    # Room
    "ROOM_ID": "ROOM_ID",
    "ROOM ID": "ROOM_ID",
    "رقم الغرفة": "ROOM_ID",

    "ROOM_NAME": "ROOM_NAME",
    "ROOM NAME": "ROOM_NAME",
    "اسم الغرفة": "ROOM_NAME",

    # Tag
    "TAG_NUMBER": "TAG_NUMBER",
    "TAG NUMBER": "TAG_NUMBER",
    "TAG": "TAG_NUMBER",
    "TAG NO": "TAG_NUMBER",
    "TAG#": "TAG_NUMBER",
    "رقم التاق": "TAG_NUMBER",
    "التاق": "TAG_NUMBER",
    "تاق": "TAG_NUMBER",

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
    "وصف انجليزي": "DESCRIPTION_EN",

    # Levels (many forms)
    "DESCRIPTION_L1": "DESCRIPTION_L1",
    "L1": "DESCRIPTION_L1",
    "LEVEL1": "DESCRIPTION_L1",
    "LEVEL 1": "DESCRIPTION_L1",
    "المستوى الاول": "DESCRIPTION_L1",
    "المستوى الأول": "DESCRIPTION_L1",
    "وصف المستوى الاول": "DESCRIPTION_L1",
    "وصف المستوى الأول": "DESCRIPTION_L1",

    "DESCRIPTION_L2": "DESCRIPTION_L2",
    "L2": "DESCRIPTION_L2",
    "LEVEL2": "DESCRIPTION_L2",
    "LEVEL 2": "DESCRIPTION_L2",
    "المستوى الثاني": "DESCRIPTION_L2",
    "وصف المستوى الثاني": "DESCRIPTION_L2",

    "DESCRIPTION_L3": "DESCRIPTION_L3",
    "L3": "DESCRIPTION_L3",
    "LEVEL3": "DESCRIPTION_L3",
    "LEVEL 3": "DESCRIPTION_L3",
    "المستوى الثالث": "DESCRIPTION_L3",
    "وصف المستوى الثالث": "DESCRIPTION_L3",

    "DESCRIPTION_L4": "DESCRIPTION_L4",
    "L4": "DESCRIPTION_L4",
    "LEVEL4": "DESCRIPTION_L4",
    "LEVEL 4": "DESCRIPTION_L4",
    "المستوى الرابع": "DESCRIPTION_L4",
    "وصف المستوى الرابع": "DESCRIPTION_L4",

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

    # Try direct Arabic/original match first, then uppercase match
    if k in KEY_ALIASES:
        return KEY_ALIASES[k]
    if k_up in KEY_ALIASES:
        return KEY_ALIASES[k_up]

    # Fallback: make it uppercase with underscores
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
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(COLUMNS) + 5)
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

def build_row(data: Dict[str, str]) -> List[str]:
    if not data.get("DEPARTMENT"):
        data["DEPARTMENT"] = DEFAULT_SHEET_NAME
    return [data.get(col, "") for col in COLUMNS]

# -----------------------
# Telegram handlers
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "جاهز ✅\n"
        "ألصق نموذج الجهاز بصيغة KEY: VALUE داخل هذا القروب.\n"
        "تقدر تستخدم عربي/انجليزي، و(:) أو (：).\n"
        "لإظهار Chat ID اكتب /id"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"CHAT_ID: {update.effective_chat.id}")

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

    data = parse_kv(update.message.text)

    # Only process if it looks like our template (now very loose)
    if "TAG_NUMBER" not in data and "التاق" not in update.message.text and "رقم التاق" not in update.message.text:
        return

    missing = missing_required(data)
    if missing:
        await update.message.reply_text("❌ حقول ناقصة:\n- " + "\n- ".join(missing))
        return

    dept = (data.get("DEPARTMENT") or "").strip() or DEFAULT_SHEET_NAME

    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = get_or_create_worksheet(sh, dept)
        ensure_header(ws)

        ws.append_row(build_row(data), value_input_option="RAW")
        await update.message.reply_text(f"✅ تمت الإضافة بنجاح إلى ورقة: {dept}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ أثناء الإضافة: {e}")

def main():
    if not PUBLIC_URL:
        raise RuntimeError("Missing PUBLIC_URL/RENDER_EXTERNAL_URL environment variable")

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
