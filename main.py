import logging
import re
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from flask import Flask
import threading

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Часовой пояс
TZ = pytz.timezone("Europe/Kaliningrad")

# ====================== ПАРСЕР ВРЕМЕНИ ======================
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_reminder(text: str):
    now_local = datetime.now(TZ)

    # через N минут / часов
    m = re.match(r"напомни через (\d+)\s*(минут[уы]?|час[аов]?)\s+(.+)", text, re.I)
    if m:
        n = int(m.group(1))
        what = m.group(3).strip()
        if "мин" in m.group(2):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        return {"once_at": now_local + delta, "text": what}

    # сегодня в HH:MM
    m = re.match(rf"напомни сегодня в {RE_TIME}\s+(.+)", text, re.I)
    if m:
        hh, mm, what = int(m.group("h")), int(m.group("m")), m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local:
            target += timedelta(days=1)
        return {"once_at": target, "text": what}

    # завтра в HH:MM
    m = re.match(rf"напомни завтра в {RE_TIME}\s+(.+)", text, re.I)
    if m:
        hh, mm, what = int(m.group("h")), int(m.group("m")), m.group(4).strip()
        base = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": what}

    # просто "в HH:MM" (на сегодня или завтра)
    m = re.match(rf"напомни в {RE_TIME}\s+(.+)", text, re.I)
    if m:
        hh, mm, what = int(m.group("h")), int(m.group("m")), m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local:
            target += timedelta(days=1)
        return {"once_at": target, "text": what}

    # каждый день в HH:MM
    m = re.match(rf"напомни каждый день в {RE_TIME}\s+(.+)", text, re.I)
    if m:
        hh, mm, what = int(m.group("h")), int(m.group("m")), m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"daily": (hh, mm), "text": what}

    return None

# ====================== ОБРАБОТЧИКИ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен ✅\n\nПримеры:\n"
        "• напомни сегодня в 16:00 купить молоко\n"
        "• напомни завтра в 9:15 встреча с Андреем\n"
        "• напомни в 22:30 позвонить маме\n"
        "• напомни через 5 минут попить воды\n"
        "• напомни каждый день в 09:30 зарядка\n"
        f"(часовой пояс: {TZ})"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parsed = parse_reminder(text)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял формат напоминания.")
        return

    chat_id = update.message.chat_id

    if "once_at" in parsed:
        target = parsed["once_at"]
        delta = (target - datetime.now(TZ)).total_seconds()
        context.job_queue.run_once(
            lambda ctx: ctx.bot.send_message(chat_id, f"⏰ Напоминаю: {parsed['text']}"),
            when=delta,
            chat_id=chat_id,
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {target.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: {TZ})"
        )

    elif "daily" in parsed:
        hh, mm = parsed["daily"]
        context.job_queue.run_daily(
            lambda ctx: ctx.bot.send_message(chat_id, f"⏰ Ежедневное напоминание: {parsed['text']}"),
            time=datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0).timetz(),
            chat_id=chat_id,
        )await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02}:{mm:02} — «{parsed['text']}». (TZ: {TZ})"
        )

# ====================== HEALTH-CHECK ======================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Bot is running!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

# ====================== ЗАПУСК ======================
def main():
    application = Application.builder().token("YOUR_TELEGRAM_BOT_TOKEN").build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Запускаем Flask health-check в отдельном потоке
    threading.Thread(target=run_flask).start()

    application.run_polling()

if __name__ == "__main__":
    main()
