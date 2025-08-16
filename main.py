import logging
import os
import re
from datetime import datetime, timedelta
from pytz import timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from apscheduler.schedulers.background import BackgroundScheduler

# Логирование
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Часовой пояс
TIMEZONE = timezone("Europe/Kaliningrad")

# ================= Ключи доступа =================
ACCESS_KEYS = [f"VIP{str(i).zfill(3)}" for i in range(1, 101)]
USED_KEYS = set()
ALLOWED_USERS = set()

# Планировщик
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# ================= Напоминания =================
async def remind(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"🔔 Напоминание: {job.data}")

# ================= Авторизация =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ALLOWED_USERS:
        await update.message.reply_text("✅ Ты уже авторизован! Можешь создавать напоминания.")
        return

    await update.message.reply_text("🔑 Введи свой ключ доступа:")

async def check_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id in ALLOWED_USERS:
        await set_reminder(update, context)
        return

    if text in ACCESS_KEYS and text not in USED_KEYS:
        ALLOWED_USERS.add(user_id)
        USED_KEYS.add(text)
        await update.message.reply_text("✅ Ключ принят! Теперь ты можешь создавать напоминания.")
    else:
        await update.message.reply_text("❌ Неверный или уже использованный ключ.")

# ================= Обработка напоминаний =================
async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("🚫 У тебя нет доступа. Сначала введи ключ.")
        return

    text = update.message.text.lower()
    now = datetime.now(TIMEZONE)

    # через N минут
    m = re.match(r"через (\d+) минут", text)
    if m:
        minutes = int(m.group(1))
        run_time = now + timedelta(minutes=minutes)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context], kwargs={"data": text}, id=str(run_time))
        await update.message.reply_text(f"⏰ Напоминание через {minutes} минут.")
        return

    # сегодня в HH:MM
    m = re.match(r"сегодня в (\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = map(int, m.groups())
        run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context], kwargs={"data": text}, id=str(run_time))
        await update.message.reply_text(f"📅 Напоминание сегодня в {hour:02d}:{minute:02d}.")
        return

    # завтра в HH:MM
    m = re.match(r"завтра в (\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = map(int, m.groups())
        run_time = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context], kwargs={"data": text}, id=str(run_time))
        await update.message.reply_text(f"📅 Напоминание завтра в {hour:02d}:{minute:02d}.")
        return

    # каждый день в HH:MM
    m = re.match(r"каждый день в (\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = map(int, m.groups())
        scheduler.add_job(remind, "cron", hour=hour, minute=minute, args=[context], kwargs={"data": text}, id=f"daily-{hour}-{minute}-{user_id}")
        await update.message.reply_text(f"🔁 Напоминание каждый день в {hour:02d}:{minute:02d}.")
        return

    # конкретная дата (30 августа)
    m = re.match(r"(\d{1,2}) (\w+)", text)
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,"сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
    }
    if m and m.group(2) in months:
        day, month = int(m.group(1)), months[m.group(2)]
        run_time = datetime(now.year, month, day, 9, 0, tzinfo=TIMEZONE)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context], kwargs={"data": text}, id=str(run_time))
        await update.message.reply_text(f"📅 Напоминание {day} {m.group(2)} в 09:00.")
        return

    await update.message.reply_text("❓ Не понял формат. Примеры:\n"
                                    "• через 5 минут\n"
                                    "• сегодня в 09:00\n"
                                    "• завтра в 10:30\n"
                                    "• каждый день в 08:00\n"
                                    "• 30 августа")

# ================= Запуск =================
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise SystemExit("❌ Нет BOT_TOKEN в переменных окружения!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_key))
    app.run_polling()

if __name__ == "__main__":
    main()
