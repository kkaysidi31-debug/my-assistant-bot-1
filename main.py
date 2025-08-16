# -*- coding: utf-8 -*-
import logging
import re
from datetime import datetime, timedelta, time as dtime

import pytz
from flask import Flask
import threading

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackContext,
    filters,
)

# ------------------------ ЛОГИ ------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("reminder-bot")

# ------------------------ ЧАСОВОЙ ПОЯС ------------------------
TZ = pytz.timezone("Europe/Kaliningrad")


# ------------------------ HEALTH-CHECK (Flask) ------------------------
# Нужен, чтобы Render видел открытый порт, а UptimeRobot мог "будить" сервис.
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Bot is running!", 200

def run_flask():
    # Render/uptimerobot будут стучаться сюда
    flask_app.run(host="0.0.0.0", port=8080)


# ------------------------ ПАРСЕР ФРАЗ ------------------------
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_reminder(text: str):
    """
    Возвращает dict:
      {"once_at": datetime, "text": "..."}  — одноразовое
      {"daily": (hh, mm),  "text": "..."}  — ежедневное
      или None, если не распознал.
    """
    t = text.strip()
    now_local = datetime.now(TZ)

    # "напомни через N минут/часов <текст>"
    m = re.match(r"напомни\s+через\s+(\d+)\s*(минут[уы]?|час[аов]?)\s+(.+)$", t, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        what = m.group(3).strip()
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"once_at": now_local + delta, "text": what}

    # "напомни сегодня в HH:MM <текст>"
    m = re.match(rf"напомни\s+сегодня\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local:
            target += timedelta(days=1)
        return {"once_at": target, "text": what}

    # "напомни завтра в HH:MM <текст>"
    m = re.match(rf"напомни\s+завтра\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        base = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": what}

    # "напомни в HH:MM <текст>"  (сегодня, если время не прошло; иначе завтра)
    m = re.match(rf"напомни\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local:
            target += timedelta(days=1)
        return {"once_at": target, "text": what}

    # "напомни каждый день в HH:MM <текст>"
    m = re.match(rf"напомни\s+каждый\s+день\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        return {"daily": (hh, mm), "text": what}

    return None


# ------------------------ CALLBACK-и ДЛЯ JOBQUEUE ------------------------
async def job_once(ctx: CallbackContext) -> None:
    data = ctx.job.data or {}
    text = data.get("text", "Напоминание")
    await ctx.bot.send_message(ctx.job.chat_id, f"🔔 {text}")

async def job_daily(ctx: CallbackContext) -> None:
    data = ctx.job.data or {}
    text = data.get("text", "Ежедневное напоминание")
    await ctx.bot.send_message(ctx.job.chat_id, f"🔔 {text}")


# ------------------------ ОБРАБОТЧИКИ БОТА ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• напомни сегодня в 16:00 купить молоко\n"
        "• напомни завтра в 9:15 встреча с Андреем\n"
        "• напомни в 22:30 позвонить маме\n"
        "• напомни через 5 минут попить воды\n"
        "• напомни каждый день в 09:30 зарядка\n"
        f"(часовой пояс: {TZ.zone})"
    )
    await update.message.reply_text(msg)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    parsed = parse_reminder(update.message.text)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял формат. Пришли, например: «напомни завтра в 9:00 пробежка».")
        return

    chat_id = update.message.chat_id

    # одноразовое
    if "once_at" in parsed:
        target = parsed["once_at"]
        # В PTB21 можно передать chat_id и data в job_queue
        context.job_queue.run_once(
            job_once,
            when=target.astimezone(TZ),
            chat_id=chat_id,
            name=f"once-{chat_id}-{int(target.timestamp())}",
            data={"text": parsed["text"]},
            tzinfo=TZ,
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {target.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: {TZ.zone})"
        )
        return

    # ежедневное
    if "daily" in parsed:
        hh, mm = parsed["daily"]
        context.job_queue.run_daily(
            job_daily,
            time=dtime(hour=hh, minute=mm, tzinfo=TZ),
            chat_id=chat_id,
            name=f"daily-{chat_id}-{hh:02}{mm:02}",
            data={"text": parsed["text"]},
        )
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02}:{mm:02} — «{parsed['text']}». (TZ: {TZ.zone})"
        )


# ------------------------ ЗАПУСК ------------------------
def main():
    # ВАЖНО: токен читать из переменной окружения BOT_TOKEN на Render
    # (Settings → Environment → BOT_TOKEN)
    import os
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # Поднимаем Flask в отдельном потоке (порт 8080)
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Используем polling — надёжно и просто, вебхук не нужен.
    # Flask даёт нам открытый порт для Render/uptimerobot.
    log.info("Starting bot with polling...")
    application.run_polling(close_loop=False)  # не закрываем loop, чтобы Flask спокойно жил


if __name__ == "__main__":
    main()
