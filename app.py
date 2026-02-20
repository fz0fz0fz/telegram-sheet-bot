import os
import json
import base64
import re
from typing import Dict, List, Any, Optional, Tuple

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

# -----------------------
# Synonyms (any keyword -> official L3) from local JSON
# -----------------------
SYNONYMS: Dict[str, Any] = {}  # value: str OR list[str]

# -----------------------
# Pending choice state (to avoid mixing devices)
# Key = (chat_id, user_id)
# Value = {"data": {...}, "options": [...], "worksheet_name": "...", "section": "..."}
# -----------------------
PENDING_CHOICES: Dict[Tuple[int, int], Dict[str, Any]] = {}

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

def load_synonyms():
    """
    Expects synonyms.json in project root.
    Format:
      {
        "by_synonym": {
          "شفط": ["شفاطات", "وحدات شفط الطبية"],
          "حاسب": "أجهزة الكمبيوتر المحمولة وأجهزة الكمبيوتر المكتبية"
        }
      }
    """
    global SYNONYMS
    try:
        with open("synonyms.json", "r", encoding="utf-8") as f:
            obj = json.load(f)
        by_syn = obj.get("by_synonym", {})
        # normalize synonym keys
        SYNONYMS = {_norm_ar(k): v for k, v in by_syn.items()}
        print(f"[synonyms] loaded: {len(SYNONYMS)}")
    except FileNotFoundError:
        SYNONYMS = {}
        print("[synonyms] synonyms.json not found (skip)")
    except Exception as e:
        SYNONYMS = {}
        print(f"[synonyms] failed to load: {e}")

# ✅ أضفنا SECTION هنا
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

# متسامح جدًا: يكفي التاق
REQUIRED_FIELDS = ["TAG_NUMBER"]

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

def _apply_synonym_to_l3(data: Dict[str, str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    If DESCRIPTION_L3 is a synonym keyword, replace it with official L3.
    Return (options, chosen_l3):
      - options: list of possible official L3s if ambiguous
      - chosen_l3: official L3 if unique, else None
    """
    raw_l3 = _norm_ar(data.get("DESCRIPTION_L3", ""))
    if not raw_l3:
        return None, None

    hit = SYNONYMS.get(raw_l3)
    if not hit:
        # no synonym mapping: leave as-is
        return None, raw_l3

    if isinstance(hit, list):
        # ambiguous
        options = [str(x).strip() for x in hit if str(x).strip()]
        if options:
            return options, None
        return None, raw_l3

    # unique mapping
    chosen = str(hit).strip()
    if chosen:
        data["DESCRIPTION_L3"] = chosen
        return None, chosen

    return None, raw_l3

def build_row_by_header(ws, data: Dict[str, str]) -> List[str]:
    """
    🔥 أهم نقطة: نكتب حسب هيدر الورقة الفعلي حتى لو كانت الورقة القديمة ما فيها SECTION
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
        "ألصق نموذج الجهاز بصيغة KEY: VALUE داخل هذا القروب.\n"
        "يدعم عربي/انجليزي و(:) أو (：).\n"
        "الحد الأدنى: رقم التاق فقط.\n\n"
        "ميزة المرادفات:\n"
        "- إذا كتبت L3 = كلمة مثل (شفط/حاسب) يحولها لـ L3 الرسمي.\n"
        "- إذا لها أكثر من خيار: يعطيك أرقام تختار منها.\n\n"
        "لإظهار Chat ID اكتب /id\n"
        "لإلغاء آخر اختيار معلّق اكتب /cancel"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"CHAT_ID: {update.effective_chat.id}")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)
    if key in PENDING_CHOICES:
        del PENDING_CHOICES[key]
        await update.message.reply_text("تم الإلغاء ✅")
    else:
        await update.message.reply_text("ما فيه شيء معلّق للإلغاء.")

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
    user_id = update.effective_user.id if update.effective_user else 0

    if ALLOWED_CHAT_ID is not None:
        try:
            allowed = int(ALLOWED_CHAT_ID)
            if chat_id != allowed:
                return
        except ValueError:
            pass

    text = update.message.text.strip()

    # 1) إذا المستخدم عنده اختيار معلّق وأرسل رقم
    key = (chat_id, user_id)
    if key in PENDING_CHOICES:
        m = re.fullmatch(r"\s*(\d{1,2})\s*", text)
        if not m:
            await update.message.reply_text("ارسل رقم الخيار فقط (مثال: 1) أو /cancel للإلغاء.")
            return

        idx = int(m.group(1))
        pending = PENDING_CHOICES[key]
        options = pending.get("options", [])
        if idx < 1 or idx > len(options):
            await update.message.reply_text("رقم غير صحيح. اختر رقم من القائمة أو /cancel.")
            return

        chosen_l3 = options[idx - 1]
        data = pending["data"]
        data["DESCRIPTION_L3"] = chosen_l3

        # حذف التعليق قبل الكتابة
        del PENDING_CHOICES[key]

        await _write_to_sheet(update, data)
        return

    # 2) غير كذا: نعالج كنموذج جهاز
    data = parse_kv(text)

    looks_like_device = ("TAG_NUMBER" in data) or ("رقم التاق" in text) or ("Tag Number" in text) or ("التاق" in text)
    if not looks_like_device:
        return

    missing = missing_required(data)
    if missing:
        await update.message.reply_text("❌ حقول ناقصة:\n- " + "\n- ".join(missing))
        return

    # ✅ تطبيق المرادفات على L3 إذا موجود
    options, chosen = _apply_synonym_to_l3(data)
    if options:
        # نخزن الطلب معلّق لنفس المستخدم/القروب فقط (أمان من الخلط)
        PENDING_CHOICES[key] = {"data": data, "options": options}

        msg_lines = ["اختر المستوى الثالث الصحيح بإرسال رقم فقط:"]
        for i, opt in enumerate(options, start=1):
            msg_lines.append(f"{i}) {opt}")
        msg_lines.append("\nمثال: ارسل 1")
        msg_lines.append("للإلغاء: /cancel")
        await update.message.reply_text("\n".join(msg_lines))
        return

    # 3) لو ما فيه تعارض: نكتب مباشرة
    await _write_to_sheet(update, data)

def main():
    if not PUBLIC_URL:
        raise RuntimeError("Missing PUBLIC_URL/RENDER_EXTERNAL_URL environment variable")

    load_classifications()
    load_synonyms()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{PUBLIC_URL}/webhook",
    )

if __name__ == "__main__":
    main()
