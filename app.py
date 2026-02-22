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
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")   # ← أفضل رؤية حالياً
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
    return re.sub(r"\s+", " ", s)

def load_classifications():
    global CLASSIFICATIONS
    try:
        with open("classifications.json","r",encoding="utf-8") as f:
            obj=json.load(f)
        CLASSIFICATIONS={_norm_ar(k):v for k,v in obj.get("by_L3",{}).items()}
        print("loaded classifications:",len(CLASSIFICATIONS))
    except Exception as e:
        print("classification load fail:",e)


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
    sa=json.loads(base64.b64decode(GOOGLE_SA_JSON_B64).decode())
    creds=Credentials.from_service_account_info(sa,scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)

def get_or_create_worksheet(sh,title):
    try:
        return sh.worksheet(title)
    except:
        ws=sh.add_worksheet(title=title,rows=2000,cols=20)
        ws.append_row(COLUMNS)
        return ws

def build_row_by_header(ws,data):
    header=ws.row_values(1) or COLUMNS
    return [data.get(h,"") for h in header]

async def write_to_sheet(data):
    gc=get_gspread_client()
    sh=gc.open_by_key(SPREADSHEET_ID)
    ws=get_or_create_worksheet(sh,data.get("DEPARTMENT") or DEFAULT_SHEET_NAME)
    ws.append_row(build_row_by_header(ws,data))


# =========================
# STATE
# =========================
USER_PLACE={}
ALBUM_BUFFER={}
PENDING_CHOICES={}


# =========================
# PARSE PLACE
# =========================
def parse_place_line(text):
    if "=" not in text:
        return None
    p=[x.strip() for x in text.split("=") if x.strip()]
    if len(p)!=4:
        return None
    return dict(DEPARTMENT=p[0],SECTION=p[1],ROOM_NAME=p[2],ROOM_ID=p[3])


# =========================
# SHORTLIST
# =========================
def _tokenize(s):
    s=_norm_ar(s).lower()
    s=re.sub(r"[^\w\u0600-\u06FF]+"," ",s)
    return [t for t in s.split() if len(t)>=2]

def shortlist_l3(query,k=30):
    if not CLASSIFICATIONS:
        return []
    qt=set(_tokenize(query))
    scored=[]
    for l3 in CLASSIFICATIONS:
        lt=set(_tokenize(l3))
        if not lt: continue
        inter=len(qt&lt)
        union=len(qt|lt) or 1
        score=inter/union
        if any(t in l3 for t in qt): score+=0.05
        scored.append((score,l3))
    scored.sort(reverse=True)
    return [l3 for _,l3 in scored[:k]] or list(CLASSIFICATIONS.keys())[:k]


# =========================
# OPENAI VISION
# =========================
def call_vision_extract(images,l3_candidates):
    if oa_client is None:
        raise RuntimeError("NO OPENAI KEY")

    content=[{"type":"text","text":
"استخرج بيانات الجهاز من الصور وارجع JSON فقط.\n"
"fields: TAG_NUMBER, DESCRIPTION_AR, DESCRIPTION_EN, MANUFACTURER_NAME, MODEL_NUMBER, SERIAL_NUMBER\n"
"choices: اقرب L3 من القائمة فقط\n"
f"{l3_candidates}"
}]
    for b in images:
        content.append({"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b}"}})

    r=oa_client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.1,
        messages=[{"role":"user","content":content}]
    )
    t=r.choices[0].message.content or "{}"
    try: return json.loads(t)
    except:
        m=re.search(r"\{.*\}",t,re.S)
        return json.loads(m.group(0))


def format_model(place,fields,l1,l2,l3):
    return(
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
async def cmd_start(update,context):
    await update.message.reply_text(
"جاهز ✅\n"
"أرسل المكان:\nمستشفى=قسم=غرفة=رقم\n"
"ثم صور الجهاز ثم Done\n"
"واكتب جاهز لمعرفة الحالة"
)

async def cmd_place(update,context):
    key=(update.effective_chat.id,update.effective_user.id)
    p=USER_PLACE.get(key)
    await update.message.reply_text(str(p) if p else "مافي مكان محفوظ")

async def cmd_cancel(update,context):
    key=(update.effective_chat.id,update.effective_user.id)
    ALBUM_BUFFER.pop(key,None)
    PENDING_CHOICES.pop(key,None)
    await update.message.reply_text("تم إلغاء الجهاز الحالي")

async def cmd_reset(update,context):
    key=(update.effective_chat.id,update.effective_user.id)
    USER_PLACE.pop(key,None)
    ALBUM_BUFFER.pop(key,None)
    PENDING_CHOICES.pop(key,None)
    await update.message.reply_text("تم مسح كل الحالة")


# =========================
# HANDLE TEXT
# =========================
async def handle_text(update,context):

    key=(update.effective_chat.id,update.effective_user.id)
    text=update.message.text.strip()

    # جاهز
    if text.lower()=="جاهز":
        p=USER_PLACE.get(key)
        if not p:
            await update.message.reply_text("ارسل المكان اول")
        else:
            await update.message.reply_text(
f"انا جاهز\n{p['DEPARTMENT']} / {p['SECTION']} / {p['ROOM_NAME']} / {p['ROOM_ID']}\nارسل صور"
)
        return

    # اختيار تصنيف
    if key in PENDING_CHOICES:
        if text.isdigit():
            idx=int(text)-1
            pending=PENDING_CHOICES[key]
            opt=pending["options"][idx]
            data=pending["data"]
            data["DESCRIPTION_L1"]=opt["L1"]
            data["DESCRIPTION_L2"]=opt["L2"]
            data["DESCRIPTION_L3"]=opt["L3"]
            await write_to_sheet(data)
            await update.message.reply_text("تم الحفظ")
            PENDING_CHOICES.pop(key,None)
            ALBUM_BUFFER.pop(key,None)
        return

    # مكان
    place=parse_place_line(text)
    if place:
        USER_PLACE[key]=place
        await update.message.reply_text("تم حفظ المكان")
        return

    # Done
    if text.lower()=="done":

        if key not in USER_PLACE:
            await update.message.reply_text("ارسل المكان اول")
            return

        buf=ALBUM_BUFFER.get(key)
        if not buf:
            await update.message.reply_text("مافي صور")
            return

        images=[]
        for fid in buf["photos"][:OPENAI_MAX_IMAGES]:
            f=await context.bot.get_file(fid)
            b=await f.download_as_bytearray()
            images.append(base64.b64encode(bytes(b)).decode())

        # قراءة أولية
        r=call_vision_extract(images,list(CLASSIFICATIONS.keys())[:40])
        fields=r.get("fields",{})

        # shortlist ذكية
        hint=f"{fields.get('DESCRIPTION_AR','')} {fields.get('DESCRIPTION_EN','')}"
        l3=shortlist_l3(hint or "جهاز طبي",35)

        r=call_vision_extract(images,l3)
        fields=r.get("fields",{})
        choices=[c for c in r.get("choices",[]) if c in CLASSIFICATIONS][:4]
        if not choices: choices=l3[:3]

        options=[]
        for c in choices:
            h=CLASSIFICATIONS.get(c,{})
            options.append(dict(L1=h.get("L1",""),L2=h.get("L2",""),L3=h.get("L3",c)))

        place=USER_PLACE[key]

        data=dict(
DEPARTMENT=place["DEPARTMENT"],
SECTION=place["SECTION"],
ROOM_NAME=place["ROOM_NAME"],
ROOM_ID=place["ROOM_ID"],
TAG_NUMBER=fields.get("TAG_NUMBER",""),
DESCRIPTION_AR=fields.get("DESCRIPTION_AR",""),
DESCRIPTION_EN=fields.get("DESCRIPTION_EN",""),
MANUFACTURER_NAME=fields.get("MANUFACTURER_NAME",""),
MODEL_NUMBER=fields.get("MODEL_NUMBER",""),
SERIAL_NUMBER=fields.get("SERIAL_NUMBER",""),
DESCRIPTION_L1="",
DESCRIPTION_L2="",
DESCRIPTION_L3="",
DESCRIPTION_L4=""
)

        preview=format_model(place,fields,
options[0]["L1"],options[0]["L2"],options[0]["L3"])

        PENDING_CHOICES[key]=dict(data=data,options=options)

        txt="🧾 نموذج:\n"+preview+"\nاختر رقم:\n"
        for i,o in enumerate(options,1):
            txt+=f"{i}) {o['L1']} → {o['L2']} → {o['L3']}\n"

        await update.message.reply_text(txt)
        return


# =========================
# HANDLE PHOTO
# =========================
async def handle_photo(update,context):

    key=(update.effective_chat.id,update.effective_user.id)

    if key not in USER_PLACE:
        await update.message.reply_text("ارسل المكان اول")
        return

    fid=update.message.photo[-1].file_id
    buf=ALBUM_BUFFER.setdefault(key,{"photos":[]})
    buf["photos"].append(fid)


async def handle_doc(update,context):

    doc=update.message.document
    if not doc or not (doc.mime_type or "").startswith("image"):
        return

    key=(update.effective_chat.id,update.effective_user.id)

    if key not in USER_PLACE:
        await update.message.reply_text("ارسل المكان اول")
        return

    buf=ALBUM_BUFFER.setdefault(key,{"photos":[]})
    buf["photos"].append(doc.file_id)


# =========================
# MAIN
# =========================
def main():

    load_classifications()

    app=Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("cancel",cmd_cancel))
    app.add_handler(CommandHandler("reset",cmd_reset))
    app.add_handler(CommandHandler("place",cmd_place))

    app.add_handler(MessageHandler(filters.PHOTO,handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL,handle_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))

    app.run_webhook(
listen="0.0.0.0",
port=PORT,
url_path="webhook",
webhook_url=f"{PUBLIC_URL}/webhook"
)

if __name__=="__main__":
    main()
