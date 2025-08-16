import logging
import re
import os
from datetime import datetime, timedelta
from pytz import timezone
from flask import Flask, request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

# Логирование
logging.basicConfig(level=logging.INFO)

# Часовой пояс
TIMEZONE = timezone("Europe/Kaliningrad")

# Генерация ключей доступа
def generate_keys(n=100):
    keys = {}
    for i in range(n):
        key = f"VIP{str(i+1).zfill(3)}"  # VIP001 ... VIP100
        keys[key] = None
    return keys

ACCESS_KEYS = generate_keys(100)
ALLOWED_USERS = set()

# Планировщик
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# ================== Команды ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(
            "🔒 Этот бот приватный.\n\n"
            "Введи ключ доступа в формате: ABC123"
        )
        return
    await update.message.reply_text("✅ Добро пожаловать! Напиши напоминание в формате:\n"
                                    "— через 5 минут ...\n"
                                    "— сегодня в HH:MM ...\n"
                                    "— завтра в HH:MM ...\n"
                                    "— каждый день в HH:MM ...\n"
                                    "— 30 августа ...")

# ================== Проверка ключей ==================

async def check_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if user_id in ALLOWED_USERS:
        return

    if text in ACCESS_KEYS and ACCESS_KEYS[text] is None:
        ACCESS_KEYS[text] = user_id
        ALLOWED_USERS.add(user_id)
        await update.message.reply_text("✅ Ключ принят! Доступ открыт.")
    else:
        await update.message.reply_text("❌ Неверный ключ. Попробуй еще раз.")

# ================== Напоминания ==================

async def remind(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.kwargs.get("data")
    chat_id = job.chat_id
    await context.bot.send_message(chat_id, f"⏰ Напоминание: {data}")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ Нет доступа. Сначала введи ключ.")
        return

    text = update.message.text.strip()
    task = text

    # 1) через X минут
    m = re.match(r"через (\d+) минут", text)
    if m:
        minutes = int(m.group(1))
        run_time = datetime.now(TIMEZONE) + timedelta(minutes=minutes)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                          kwargs={"data": task}, id=f"{user_id}_{run_time}")
        await update.message.reply_text(f"✅ Напоминание через {minutes} минут установлено!")
        return

    # 2) сегодня в HH:MM
    m = re.match(r"сегодня в (\d{1,2}):(\d{2})", text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        run_time = datetime.now(TIMEZONE).replace(hour=hh, minute=mm, second=0, microsecond=0)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                          kwargs={"data": task}, id=f"{user_id}_{run_time}")
        await update.message.reply_text(f"✅ Напоминание сегодня в {hh:02d}:{mm:02d} установлено!")
        return

    # 3) завтра в HH:MM
    m = re.match(r"завтра в (\d{1,2}):(\d{2})", text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        run_time = (datetime.now(TIMEZONE) + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                          kwargs={"data": task}, id=f"{user_id}_{run_time}")
        await update.message.reply_text(f"✅ Напоминание завтра в {hh:02d}:{mm:02d} установлено!")
        return

    # 4) каждый день в HH:MM
    m = re.match(r"каждый день в (\d{1,2}):(\d{2})", text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        scheduler.add_job(remind, "cron", hour=hh, minute=mm, args=[context],
                          kwargs={"data": task}, id=f"{user_id}_daily_{hh}{mm}")
        await update.message.reply_text(f"✅ Ежедневное напоминание в {hh:02d}:{mm:02d} установлено!")
        return

    # 5) конкретная дата: 30 августа
    m = re.match(r"(\d{1,2}) ([а-яА-Я]+)", text)
    if m:
        day, month_name = m.groups()
        months = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
            "мая": 5, "июня": 6, "июля": 7, "августа": 8,
            "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
        }
        if month_name in months:
            month = months[month_name]
            year = datetime.now(TIMEZONE).year
            run_time = datetime(year, month, int(day), 9, 0, tzinfo=TIMEZONE)  # 09:00 по умолчанию
            scheduler.add_job(remind, "date", run_date=run_time, args=[context],
                              kwargs={"data": task}, id=f"{user_id}_{run_time}")
            await update.message.reply_text(f"✅ Напоминание {day} {month_name} установлено на 09:00!")
            return

    await update.message.reply_text("❌ Не понял формат. Используй:\n"
                                    "— через 5 минут ...\n"
                                    "— сегодня в HH:MM ...\n"
                                    "— завтра в HH:MM ...\n"
                                    "— каждый день в HH:MM ...\n"
                                    "— 30 августа ...")

# ================== Запуск ==================

def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_key))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    app.run_polling()

if __name__ == "__main__":
    main()
