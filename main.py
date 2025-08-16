# -*- coding: utf-8 -*-
import os
import re
import random
import logging
import threading
from datetime import datetime, timedelta, time

from flask import Flask, Response  # маленький HTTP-сервер, чтобы Render видел открытый порт
from pytz import timezone

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from apscheduler.schedulers.background import BackgroundScheduler

# ---------------- ЛОГИ ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

# ---------------- ТАЙМЗОНА ----------------
TIMEZONE = timezone("Europe/Kaliningrad")

# ---------------- ОДНОРАЗОВЫЕ КЛЮЧИ ----------------
def generate_keys(n=100):
    keys = {}
    for i in range(1, n + 1):
        key = f"VIP{i:03d}"
        keys[key] = None  # None -> не использован; иначе будет user_id
    return keys

ACCESS_KEYS = generate_keys(100)
ALLOWED_USERS = set()

# ---------------- МЕСЯЦЫ (род. падеж) ----------------
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

RE_TIME = r"(?P<h>\d{1,2})[:\.](?P<m>\d{2})"

# ---------------- ВСПОМОГАТЕЛЬНОЕ ----------------
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def parse_text(text: str):
    """
    Возвращает одно из:
    {"after": timedelta, "text": "..."}
    {"once_at": datetime, "text": "..."}
    {"daily_at": time, "text": "..."}
    {"date": (year, month, day, hh, mm), "text": "..."}
    либо None.
    """
    t = text.lower().strip()

    # 1) "через N минут/часов <текст>"
    m = re.match(r"^через\s+(?P<n>\d+)\s*(минут(?:ы)?|мин|час(?:а|ов)?)\s+(?P<text>.+)$", t)
    if m:
        n = int(m.group("n"))
        unit = m.group(2)
        if unit.startswith("мин"):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        return {"after": delta, "text": m.group("text").strip()}

    # 2) "сегодня в HH:MM <текст>"
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        # если время уже прошло — на завтра
        if target <= now_local():
            target += timedelta(days=1)
        return {"once_at": target, "text": m.group("text").strip()}

    # 3) "завтра в HH:MM <текст>"
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": m.group("text").strip()}

    # 4) "каждый день в HH:MM <текст>"
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        return {"daily_at": time(hh, mm, tzinfo=TIMEZONE), "text": m.group("text").strip()}

    # 5) "30 августа [в 09:30] <текст>"  (если время не указано — 09:00)
    m = re.match(
        rf"^(?P<d>\d{{1,2}})\s+(?P<month>{'|'.join(MONTHS.keys())})(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$",
        t
    )
    if m:
        d = int(m.group("d"))
        month_name = m.group("month")
        month = MONTHS[month_name]
        year = now_local().year
        if m.group("h"):
            hh = int(m.group("h")); mm = int(m.group("m"))
        else:
            hh, mm = 9, 0
        return {"date": (year, month, d, hh, mm), "text": m.group("text").strip()}

    return None

# ---------------- SCHEDULER ----------------
scheduler = BackgroundScheduler(timezone=TIMEZONE)

async def remind(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.kwargs.get("data", {})
    chat_id = data.get("chat_id")
    text = data.get("text")
    if chat_id and text:
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ Напоминание: {text}")
        
# ---------------- ДОСТУП ПО КЛЮЧУ ----------------
def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USERS

def try_activate_key(user_id: int, candidate: str) -> bool:
    candidate = candidate.strip().upper()
    if candidate in ACCESS_KEYS and ACCESS_KEYS[candidate] is None:
        ACCESS_KEYS[candidate] = user_id
        ALLOWED_USERS.add(user_id)
        return True
    return False

WELCOME_EXAMPLES = (
    "Примеры:\n"
    "• напомни сегодня в 16:00 купить молоко\n"
    "• напомни завтра в 9:15 встреча с Андреем\n"
    "• напомни в 22:30 позвонить маме\n"
    "• напомни через 5 минут попить воды\n"
    "• напомни каждый день в 09:30 зарядка\n"
    "• напомни 30 августа в 09:00 заплатить за кредит\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text(
            "Бот приватный. Введите ваш одноразовый ключ доступа (формат VIP001…VIP100)."
        )
        return
    await update.message.reply_text("Бот запущен ✅\n\n" + WELCOME_EXAMPLES)

# ---------------- ОБРАБОТЧИК ТЕКСТА ----------------
async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # 1) если не авторизован — пытаемся принять ключ
    if not is_allowed(user_id):
        if try_activate_key(user_id, text):
            await update.message.reply_text("Ключ принят ✅\n\n" + WELCOME_EXAMPLES)
        else:
            await update.message.reply_text("Неверный или уже использованный ключ. Попробуйте снова.")
        return

    # 2) парсим команду-напоминание
    parsed = parse_text(text)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов ...\n"
            "— сегодня в HH:MM ...\n"
            "— завтра в HH:MM ...\n"
            "— каждый день в HH:MM ...\n"
            "— 30 августа [в 09:00] ..."
        )
        return

    # 2.1 через N…
    if "after" in parsed:
        run_time = now_local() + parsed["after"]
        scheduler.add_job(
            remind, "date", run_date=run_time,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"after_{chat_id}_{int(run_time.timestamp())}_{random.randint(1000,9999)}",
            replace_existing=True,
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {run_time.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». "
            "(TZ: Europe/Kaliningrad)"
        )
        return

    # 2.2 сегодня/завтра в HH:MM (once_at)
    if "once_at" in parsed:
        run_time = parsed["once_at"]
        scheduler.add_job(
            remind, "date", run_date=run_time,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"once_{chat_id}_{int(run_time.timestamp())}_{random.randint(1000,9999)}",
            replace_existing=True,
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {run_time.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». "
            "(TZ: Europe/Kaliningrad)"
        )
        return

    # 2.3 каждый день в HH:MM
    if "daily_at" in parsed:
        hh = parsed["daily_at"].hour
        mm = parsed["daily_at"].minute
        scheduler.add_job(
            remind, "cron", hour=hh, minute=mm,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"daily_{chat_id}_{hh:02d}{mm:02d}",
            replace_existing=True,
        )
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02d}:{mm:02d} — «{parsed['text']}». "
            "(TZ: Europe/Kaliningrad)"
        )
        return

    # 2.4 конкретная дата: DD месяц [в HH:MM]
    if "date" in parsed:
        y, mth, d, hh, mm = parsed["date"]
        run_time = datetime(y, mth, d, hh, mm, tzinfo=TIMEZONE)
        scheduler.add_job(
            remind, "date", run_date=run_time,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"date_{chat_id}_{y}{mth:02d}{d:02d}{hh:02d}{mm:02d}_{random.randint(1000,9999)}",
            replace_existing=True,
        )
        # красивое имя месяца
        month_name = next((name for name, num in MONTHS.items() if num == mth), f"{mth}")
        await update.message.reply_text(
            f"✅ Напоминание {d} {month_name} в {hh:02d}:{mm:02d} — «{parsed['text']}». "
            "(TZ: Europe/Kaliningrad)"
        )
        return

# ---------------- МИНИ-FLASK ДЛЯ RENDER ----------------
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return Response("OK", 200)

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ---------------- ЗАПУСК ----------------
def main():
    # токен только из переменной окружения
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # стартуем HTTP-сервер для Render в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()

    # планировщик
    scheduler.start()

    # телеграм-бот
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    log.info("Starting bot with polling...")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
