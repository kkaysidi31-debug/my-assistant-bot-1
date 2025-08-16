import logging
import re
import random
from datetime import datetime, timedelta
from pytz import timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

# Логирование
logging.basicConfig(level=logging.INFO)

# Часовой пояс
TIMEZONE = timezone("Europe/Kaliningrad")

# =================== Ключи доступа ===================
def generate_keys(n=100):
    keys = {}
    for _ in range(n):
        key = f"VIP{random.randint(100, 999)}"
        keys[key] = None
    return keys

ACCESS_KEYS = generate_keys(100)   # 100 ключей
ALLOWED_USERS = set()

# =================== Планировщик ===================
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# =================== Функции ===================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

    if user_id in ALLOWED_USERS:
        await update.message.reply_text("✅ У вас уже есть доступ!")
        return

    if args and args[0] in ACCESS_KEYS:
        if ACCESS_KEYS[args[0]] is None:
            ACCESS_KEYS[args[0]] = user_id
            ALLOWED_USERS.add(user_id)
            await update.message.reply_text("🔑 Доступ предоставлен!")
        else:
            await update.message.reply_text("⛔ Этот ключ уже использован.")
    else:
        await update.message.reply_text("⛔ У вас нет доступа.")

async def remind(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"⏰ Напоминание: {job.data}")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ У вас нет доступа. Введите ключ при /start.")
        return

    text = update.message.text.lower()

    # --- через X минут ---
    match = re.match(r"через (\d+) минут (.+)", text)
    if match:
        minutes, task = int(match.group(1)), match.group(2)
        run_time = datetime.now(TIMEZONE) + timedelta(minutes=minutes)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                          id=str(run_time)+task, kwargs={"data": task}, replace_existing=True)
        await update.message.reply_text(f"✅ Напоминание через {minutes} минут: {task}")
        return

    # --- сегодня в HH:MM ---
    match = re.match(r"сегодня в (\d{1,2}):(\d{2}) (.+)", text)
    if match:
        hour, minute, task = int(match.group(1)), int(match.group(2)), match.group(3)
        run_time = datetime.now(TIMEZONE).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_time < datetime.now(TIMEZONE):
            await update.message.reply_text("⛔ Это время уже прошло сегодня.")
            return
        scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                          id=str(run_time)+task, kwargs={"data": task}, replace_existing=True)
        await update.message.reply_text(f"✅ Напоминание сегодня в {hour:02d}:{minute:02d}: {task}")
        return

    # --- завтра в HH:MM ---
    match = re.match(r"завтра в (\d{1,2}):(\d{2}) (.+)", text)
    if match:
        hour, minute, task = int(match.group(1)), int(match.group(2)), match.group(3)
        run_time = datetime.now(TIMEZONE).replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                          id=str(run_time)+task, kwargs={"data": task}, replace_existing=True)
        await update.message.reply_text(f"✅ Напоминание завтра в {hour:02d}:{minute:02d}: {task}")
        return

    # --- каждый день в HH:MM ---
    match = re.match(r"каждый день в (\d{1,2}):(\d{2}) (.+)", text)
    if match:
        hour, minute, task = int(match.group(1)), int(match.group(2)), match.group(3)
        scheduler.add_job(remind, "cron", hour=hour, minute=minute, args=[context],id=task, kwargs={"data": task}, replace_existing=True)
        await update.message.reply_text(f"✅ Ежедневное напоминание в {hour:02d}:{minute:02d}: {task}")
        return

    # --- конкретная дата (ДД месяц) ---
    match = re.match(r"(\d{1,2}) (\w+) (.+)", text)
    if match:
        day, month_name, task = match.group(1), match.group(2), match.group(3)
        months = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
            "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
        }
        if month_name in months:
            month = months[month_name]
            year = datetime.now(TIMEZONE).year
            run_time = datetime(year, month, int(day), 9, 0, tzinfo=TIMEZONE)  # по умолчанию 09:00
            scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                              id=str(run_time)+task, kwargs={"data": task}, replace_existing=True)
            await update.message.reply_text(f"✅ Напоминание {day} {month_name}: {task}")
            return

    await update.message.reply_text("❓ Не понял формат. Используй:\n"
                                    "- через 5 минут ...\n"
                                    "- сегодня в HH:MM ...\n"
                                    "- завтра в HH:MM ...\n"
                                    "- каждый день в HH:MM ...\n"
                                    "- 30 августа ...")

# =================== Запуск ===================
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    app.run_polling()

if __name__ == "__main__":
    main()
