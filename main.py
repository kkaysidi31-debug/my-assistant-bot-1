import os
import re
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import tempfile

from flask import Flask, Response
from openai import OpenAI

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ----------------------------- ЛОГИ -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

# ------------------------- НАСТРОЙКИ ---------------------------
TIMEZONE = ZoneInfo("Europe/Kaliningrad")

# Одноразовые ключи VIP001..VIP100
ACCESS_KEYS = {f"VIP{n:03d}": None for n in range(1, 101)}
ALLOWED_USERS: set[int] = set()

# OpenAI для голосовых (можно не задавать — тогда голосовые отключатся)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Мини-Flask, чтобы Render видел открытый порт (web service не засыпал)
app_http = Flask(__name__)

@app_http.route("/")
def _health():
    return Response("OK", 200)

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    log.info(f"HTTP keep-alive on 0.0.0.0:{port}")
    app_http.run(host="0.0.0.0", port=port, debug=False)

# ------------------------ ВСПОМОГАТЕЛЬНОЕ ----------------------
RU_MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12
}
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")

# ---------------------- ПРИВАТНЫЙ ДОСТУП -----------------------
async def ensure_access(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id in ALLOWED_USERS:
        return True

    txt = (update.message.text or "").strip().upper() if update.message else ""
    if txt in ACCESS_KEYS and ACCESS_KEYS[txt] is None:
        ACCESS_KEYS[txt] = user_id
        ALLOWED_USERS.add(user_id)
        await update.message.reply_text("✅ Ключ принят. Добро пожаловать!")
        return True

    await update.message.reply_text(
        "🔒 Бот приватный. Введите ключ доступа в формате ABC123.\n"
        "Если у вас нет ключа — обратитесь к владельцу бота.",
        parse_mode="Markdown"
    )
    return False

# ---------------------- ОБРАБОТЧИКИ КОМАНД ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update):
        return

    examples = (
        "Бот запущен ✅\n\nПримеры:\n"
        "• напомни сегодня в 16:00 купить молоко\n"
        "• напомни завтра в 9:15 встреча с Андреем\n"
        "• напомни в 22:30 позвонить маме\n"
        "• напомни через 5 минут попить воды\n"
        "• напомни каждый день в 09:30 зарядка\n"
        "• напомни 30 августа в 09:00 заплатить за кредит\n"
        "(часовой пояс: Europe/Kaliningrad)"
    )
    await update.message.reply_text(examples)

# --------------------- ПАРСЕР НАПОМИНАНИЙ ----------------------
def parse_request(text: str):
    t = text.strip().lower()

    m = re.match(r"^напомни\s+через\s+(?P<n>\d+)\s*(?P<u>минут[уы]?|мин|час[аов]?)\s+(?P<text>.+)$", t)
    if m:
        n = int(m.group("n")); u = m.group("u")
        delta = timedelta(minutes=n) if u.startswith("мин") else timedelta(hours=n)
        return {"after": delta, "text": m.group("text").strip()}

    m = re.match(rf"^напомни\s+сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_local():
            target += timedelta(days=1)
        return {"once_at": target, "text": m.group("text").strip()}

    m = re.match(rf"^напомни\s+завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"once_at": base + timedelta(days=1), "text": m.group("text").strip()}

    m = re.match(rf"^напомни\s+каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        return {"daily_at": (int(m.group("h")), int(m.group("m"))), "text": m.group("text").strip()}

    m = re.match(rf"^напомни\s+(?P<day>\d{{1,2}})\s+(?P<month>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$", t)
    if m and m.group("month") in RU_MONTHS:
        day = int(m.group("day")); month = RU_MONTHS[m.group("month")]
        year = now_local().year
        hh = int(m.group("h")) if m.group("h") else 9
        mm = int(m.group("m")) if m.group("m") else 0
        target = datetime(year, month, day, hh, mm, tzinfo=TIMEZONE)
        return {"once_at": target, "text": m.group("text").strip()}

    return None

# ----------------------- JOB CALLBACKS -------------------------
async def job_once(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(context.job.chat_id, f"⏰ {context.job.data.get('text', 'Напоминание')}")

async def job_daily(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(context.job.chat_id, f"📅 {context.job.data.get('text', 'Напоминание')}")

# -------------------- ТЕКСТОВЫЕ СООБЩЕНИЯ ----------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update):
        return
    parsed = parse_request(update.message.text or "")
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов …\n"
            "— сегодня в HH:MM …\n"
            "— завтра в HH:MM …\n"
            "— каждый день в HH:MM …\n"
            "— 30 августа [в 09:00] …"
        )
        return

    jq = context.job_queue
    chat_id = update.effective_chat.id

    if "after" in parsed:
        run_at = now_local() + parsed["after"]
        jq.run_once(job_once, when=parsed["after"], chat_id=chat_id, name=str(run_at),
                    data={"text": parsed["text"]})
        await update.message.reply_text(f"✅ Ок, напомню {fmt(run_at)} — «{parsed['text']}».")
        return

    if "once_at" in parsed:
        target = parsed["once_at"]
        delay = max(0, (target - now_local()).total_seconds())
        jq.run_once(job_once, when=delay, chat_id=chat_id, name=str(target),
                    data={"text": parsed["text"]})
        await update.message.reply_text(f"✅ Ок, напомню {fmt(target)} — «{parsed['text']}».")
        return

    if "daily_at" in parsed:
        hh, mm = parsed["daily_at"]
        first = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if first <= now_local():
            first += timedelta(days=1)
        jq.run_daily(job_daily, time=first.timetz(), chat_id=chat_id,
                     name=f"daily-{hh:02d}:{mm:02d}", data={"text": parsed["text"]})
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02d}:{mm:02d} — «{parsed['text']}».")
        return

# ------------------- ГОЛОСОВЫЕ СООБЩЕНИЯ ----------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update):
        return
    if not openai_client:
        await update.message.reply_text("🎙️ Речь-распознавание не настроено (нет OPENAI_API_KEY).")
        return

    voice = update.message.voice
    if not voice:
        await update.message.reply_text("Не нашёл голосовое в сообщении 🤔")
        return

    tg_file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        with open(tmp_path, "rb") as f:
            tr = openai_client.audio.transcriptions.create(model="whisper-1", file=f, response_format="text")
        text = tr.strip() if isinstance(tr, str) else str(tr).strip()
        if not text:
            await update.message.reply_text("Не смог распознать речь. Попробуй ещё раз 🙏")
            returnupdate.message.text = text
        await handle_text(update, context)
    finally:
        try: os.remove(tmp_path)
        except Exception: pass

# ----------------------------- MAIN ----------------------------
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # HTTP keep-alive для Render
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Starting bot with polling…")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
