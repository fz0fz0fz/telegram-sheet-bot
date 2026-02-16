import os
import json
import base64
import re
from typing import Dict, List

from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import gspread
from google.oauth2.service_account import Credentials

# -----------------------
# Config
# -----------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]  # from the Google Sheet URL
DEFAULT_SHEET_NAME = os.environ.get("DEFAULT_SHEET_NAME", "تجربة")

# Optional: lock to one group only
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # e.g. -1001234567890

# Service account JSON can be stored as base64 in env
GOOGLE_SA_JSON_B64 = os.environ["GOOGLE_SA_JSON_B64"]

# Columns order (must match header row in sheet)
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

# Minimum required fields to accept a message
REQUIRED_FIELDS = ["TAG_NUMBER", "DESCRIPTION_AR", "DESCRIPTION_L1", "DESCRIPTION_L2", "DESCRIPTION_L3"]

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
        # Write header row
        ws.append_row(COLUMNS, value_input_option="RAW")
        return ws

def ensure_header(ws):
    # Ensure first row matches our columns; if empty, write it
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(COLUMNS, value_input_option="RAW")
        return
    # If header differs, we won't overwrite; just proceed.
    # You can enforce strict matching if you want.

# -----------------------
# Parsing
# -----------------------
LINE_RE = re.compile(r"^\s*([A-Z_]+)\s*:\s*(.*)\s*$")

def parse_kv(text: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1).strip().upper()
        val = m.group(2).strip()
        data[key] = val
    return data

def missing_required(data: Dict[str, str]) -> List[str]:
    missing = []
    for k in REQUIRED_FIELDS:
        if not data.get(k):
            missing.append(k)
    return missing

def build_row(data: Dict[str, str]) -> List[str]:
    # If DEPARTMENT missing, use default sheet name
    if not data.get("DEPARTMENT"):
        data["DEPARTMENT"] = DEFAULT_SHEET_NAME
    row = [data.get(col, "") for col in COLUMNS]
    return row

# -----------------------
# Telegram handlers
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "جاهز ✅\n"
        "ألصق نموذج الجهاز بصيغة KEY: VALUE داخل هذا القروب.\n"
        "لإظهار Chat ID اكتب /id"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"CHAT_ID: {chat_id}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id

    # Lock to one group if configured
    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
        except ValueError:
            allowed = None
        if allowed is not None and chat_id != allowed:
            return  # ignore other chats

    text = update.message.text
    data = parse_kv(text)

    # Only process if it looks like our template
    if "TAG_NUMBER" not in data and "DESCRIPTION_AR" not in data:
        return

    missing = missing_required(data)
    if missing:
        await update.message.reply_text(
            "❌ ما أضفتها للشيت لأن فيه حقول ناقصة:\n- " + "\n- ".join(missing)
        )
        return

    # Decide which worksheet to write to
    dept = data.get("DEPARTMENT", "").strip() or DEFAULT_SHEET_NAME

    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = get_or_create_worksheet(sh, dept)
        ensure_header(ws)

        row = build_row(data)
        ws.append_row(row, value_input_option="RAW")

        await update.message.reply_text(f"✅ تمت الإضافة بنجاح إلى ورقة: {dept}")
    except Exception as e:
        await update.message.reply_text(f"❌ حصل خطأ أثناء الإضافة: {e}")

# -----------------------
# Flask webhook
# -----------------------
app = Flask(__name__)
telegram_app: Application = None

@app.get("/")
def home():
    return "OK"

@app.post("/webhook")
def webhook():
    if telegram_app is None:
        return "App not ready", 500
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "OK"

def main():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("id", cmd_id))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    telegram_app.initialize()
    telegram_app.start()

    # Flask server (Render provides PORT)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
