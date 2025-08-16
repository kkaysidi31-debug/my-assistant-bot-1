# -*- coding: utf-8 -*-
import os
import logging
import re
from datetime import datetime, timedelta
from pytz import timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from apscheduler.schedulers.background import BackgroundScheduler

# -------------------- базовая настройка --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

TIMEZONE = timezone("Europe/Kaliningrad")

def now_local() -> datetime:
    return datetime.now(TIMEZONE)

# -------------------- ключи доступа ------------------------
# Генерим VIP001..VIP100
ACCESS_KEYS = {f"VIP{n:03d}": None for n in range(1, 101)}  # значение = user_id, который активировал
ALLOWED_USERS: set[int] = set()

async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Активация ключа: /key VIP0xx"""
    if not context.args:
        await update.message.reply_text("Пришли ключ так: /key VIP001")
        return

    user_id = update.effective_user.id
    key = context.args[0].strip().upper()

    if user_id in ALLOWED_USERS:
        await update.message.reply_text("✅ У тебя уже есть доступ.")
        return

    if key not in ACCESS_KEYS:
        await update.message.reply_text("❌ Неверный ключ.")
        return

    if ACCESS_KEYS[key] is not None and ACCESS_KEYS[key] != user_id:
        await update.message.reply_text("❌ Этот ключ уже активирован другим пользователем.")
        return

    # активируем
    ACCESS_KEYS[key] = user_id
    ALLOWED_USERS.add(user_id)
    await update.message.reply_text("✅ Доступ выдан! Напиши /start")

def check_access(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ALLOWED_USERS

# -------------------- парсер фраз --------------------------
RE_TIME = r'(?P<h>\d{1,2}):(?P<m>\d{2})'

MONTHS = {
    # родительный + именительный
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}

def parse_command(text: str):
    """
    Возвращает одну из форм:
    - {"after": timedelta, "text": str}
    - {"once_at": datetime, "text": str}
    - {"daily_at": (hour, minute), "text": str}
    - {"date": (year, month, day, hour, minute), "text": str}
    """
    t = text.strip().lower()

    # 1) "через 5 минут <текст>" или "через 2 часа <текст>"
    m = re.match(r'^через\s+(?P<n>\d+)\s*(мин(ут[уы])?|м|час(а|ов)?|ч)\s+(?P<text>.+)$', t)
    if m:
        n = int(m.group('n'))
        word = m.group(2)  # мин..., м, час..., ч
        if word.startswith('мин') or word == 'м':
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        return {"after": delta, "text": m.group('text').strip()}

    # 2) "сегодня в HH:MM <текст>"
    m = re.match(rf'^сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$', t)
    if m:
        hh, mm = int(m.group('h')), int(m.group('m'))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"once_at": target, "text": m.group('text').strip()}

    # 3) "завтра в HH:MM <текст>"
    m = re.match(rf'^завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$', t)
    if m:
        hh, mm = int(m.group('h')), int(m.group('m'))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": m.group('text').strip()}

    # 4) "каждый день в HH:MM <текст>"
    m = re.match(rf'^каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$', t)
    if m:
        hh, mm = int(m.group('h')), int(m.group('m'))
        return {"daily_at": (hh, mm), "text": m.group('text').strip()}

    # 5) "30 августа <текст>" или "30 августа в HH:MM <текст>"
    m = re.match(rf'^(?P<d>\d{{1,2}})\s+(?P<month>[а-яё]+)(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$',
        t
    )
    if m and m.group('month') in MONTHS:
        day = int(m.group('d'))
        month = MONTHS[m.group('month')]
        if m.group('h') and m.group('m'):
            hh, mm = int(m.group('h')), int(m.group('m'))
        else:
            hh, mm = 9, 0  # по умолчанию 09:00
        year = now_local().year
        return {"date": (year, month, day, hh, mm), "text": m.group('text').strip()}

    return None

# -------------------- планировщик --------------------------
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

async def remind(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.kwargs["data"]
    chat_id = data["chat_id"]
    text = data["text"]
    await context.application.bot.send_message(chat_id=chat_id, text=f"⏰ Напоминание: {text}")

# -------------------- хендлеры -----------------------------
HELP = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• напомни через 5 минут попить воды\n"
    "• напомни сегодня в 16:00 купить молоко\n"
    "• напомни завтра в 9:15 встреча с Андреем\n"
    "• напомни каждый день в 09:30 зарядка\n"
    "• напомни 30 августа в 10:00 заплатить кредит\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update):
        await update.message.reply_text("🔒 Приватный бот. Пришли ключ: /key VIP001")
        return
    await update.message.reply_text(HELP)

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update):
        await update.message.reply_text("🔒 Нет доступа. Пришли ключ: /key VIP001")
        return

    text = update.message.text.strip()
    # допускаем начальные слова "добавь" / "напомни"
    if text.lower().startswith("добавь "):
        text = "напомни " + text[7:].strip()

    if text.lower().startswith("напомни "):
        text = text[len("напомни "):]

    parsed = parse_command(text)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Примеры:\n"
            "• через 5 минут ...\n"
            "• сегодня в HH:MM ...\n"
            "• завтра в HH:MM ...\n"
            "• каждый день в HH:MM ...\n"
            "• 30 августа [в HH:MM] ..."
        )
        return

    chat_id = update.effective_chat.id

    if "after" in parsed:
        run_time = now_local() + parsed["after"]
        scheduler.add_job(
            remind, "date", run_date=run_time,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"once_{chat_id}_{run_time.timestamp()}_{hash(parsed['text'])}",
            replace_existing=True,
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {run_time.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "once_at" in parsed:
        run_time = parsed["once_at"]
        scheduler.add_job(
            remind, "date", run_date=run_time,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"once_{chat_id}_{run_time.timestamp()}_{hash(parsed['text'])}",
            replace_existing=True,
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {run_time.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "daily_at" in parsed:
        hh, mm = parsed["daily_at"]
        scheduler.add_job(
            remind, "cron", hour=hh, minute=mm,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"daily_{chat_id}_{hh:02d}{mm:02d}_{hash(parsed['text'])}",
            replace_existing=True,
        )
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02d}:{mm:02d} — «{parsed['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "date" in parsed:
        y, mth, d, hh, mm = parsed["date"]run_time = datetime(y, mth, d, hh, mm, tzinfo=TIMEZONE)
        scheduler.add_job(
            remind, "date", run_date=run_time,
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            id=f"date_{chat_id}_{y}{mth:02d}{d:02d}{hh:02d}{mm:02d}_{hash(parsed['text'])}",
            replace_existing=True,
        )
        mon_names = {v: k for k, v in MONTHS.items() if k.endswith('а') or k in ("май",)}
        month_name = mon_names.get(mth, f"{mth}")
        await update.message.reply_text(
            f"✅ Напоминание {d} {month_name} в {hh:02d}:{mm:02d} — «{parsed['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

# -------------------- запуск -------------------------------
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("key", cmd_key))  # активация ключа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    log.info("Starting bot with polling...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
