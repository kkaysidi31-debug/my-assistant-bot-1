# main.py
import os
import re
import logging
from datetime import datetime, timedelta, time
from typing import Optional, Tuple

import pytz
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ───────────────────────── ЛОГИ ─────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

# ───────────────────── ВРЕМЕННАЯ ЗОНА ───────────────────
TIMEZONE = pytz.timezone("Europe/Kaliningrad")

def now_local() -> datetime:
    return datetime.now(TIMEZONE)

# ─────────────── СЕКРЕТНЫЕ КЛЮЧИ ДОСТУПА ────────────────
# Одноразовые: VIP001 … VIP100
ACCESS_KEYS = {f"VIP{str(i).zfill(3)}": None for i in range(1, 101)}
ALLOWED_USERS: set[int] = set()

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USERS

async def request_key(update: Update) -> None:
    await update.message.reply_text(
        "🔒 Бот приватный. Введите ключ доступа в формате ABC123.",
        parse_mode="Markdown"
    )

async def try_consume_key(update: Update) -> bool:
    """Возвращает True, если сообщение было ключом и доступ выдан."""
    if not update.message or not update.message.text:
        return False
    txt = update.message.text.strip().upper()
    if re.fullmatch(r"[A-Z]{3}\d{3}", txt):
        if txt in ACCESS_KEYS and ACCESS_KEYS[txt] is None:
            ACCESS_KEYS[txt] = update.effective_user.id
            ALLOWED_USERS.add(update.effective_user.id)
            await update.message.reply_text(
                "Ключ принят ✅. Теперь можно ставить напоминания."
            )
            await send_help(update)
            return True
        else:
            await update.message.reply_text("⛔️ Неверный или уже использованный ключ.")
            return True
    return False

# ───────────────────── СООБЩЕНИЕ /start ─────────────────
async def send_help(update: Update) -> None:
    tzname = TIMEZONE.zone if hasattr(TIMEZONE, "zone") else "Europe/Kaliningrad"
    text = (
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• напомни сегодня в 16:00 купить молоко\n"
        "• напомни завтра в 9:15 встреча с Андреем\n"
        "• напомни в 22:30 позвонить маме\n"
        "• напомни через 5 минут попить воды\n"
        "• напомни каждый день в 09:30 зарядка\n"
        "• напомни 30 августа в 09:00 заплатить за кредит\n"
        f"(часовой пояс: {tzname})"
    )
    await update.message.reply_text(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_allowed(uid):
        await request_key(update)
        return
    await send_help(update)

# ──────────────── ПАРСЕР ЕСТЕСТВЕННОГО ТЕКСТА ───────────
RE_TIME = r"(?P<h>\d{1,2})[:.](?P<m>\d{1,2})"
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def parse_text_command(text: str) -> Optional[Tuple[str, dict]]:
    """
    Возвращает кортеж: (тип, параметры)
    тип ∈ {'after', 'once_at', 'tomorrow_at', 'daily_at', 'date'}
    """
    t = text.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = t.replace("ё", "е")

    # 1) через N минут/часов ...
    m = re.search(r"через\s+(?P<n>\d+)\s*(минут|мин|m)\b\s+(?P<task>.+)", t)
    if m:
        delta = timedelta(minutes=int(m.group("n")))
        return "after", {"delta": delta, "text": m.group("task").strip()}
    m = re.search(r"через\s+(?P<n>\d+)\s*(час(а|ов)?|ч)\b\s+(?P<task>.+)", t)
    if m:
        delta = timedelta(hours=int(m.group("n")))
        return "after", {"delta": delta, "text": m.group("task").strip()}

    # 2) сегодня в HH:MM ...
    m = re.search(rf"сегодня\s+в\s+{RE_TIME}\s+(?P<task>.+)", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_local():
            target += timedelta(days=1)  # на завтра, если время уже прошло
        return "once_at", {"dt": target, "text": m.group("task").strip()}

    # 3) завтра в HH:MM ...
    m = re.search(rf"завтра\s+в\s+{RE_TIME}\s+(?P<task>.+)", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return "once_at", {"dt": target, "text": m.group("task").strip()}

    # 4) каждый день в HH:MM ...
    m = re.search(rf"каждый\s+день\s+в\s+{RE_TIME}\s+(?P<task>.+)", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return "daily_at", {"tm": time(hh, mm, tzinfo=TIMEZONE), "text": m.group("task").strip()}

    # 5) DD <месяц> [в HH:MM] ...
    m = re.search(
        rf"(?P<d>\d{{1,2}})\s+(?P<month>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<task>.+)", t
    )
    if m:
        day = int(m.group("d"))
        month_name = m.group("month")
        if month_name in MONTHS:
            month = MONTHS[month_name]
            year = now_local().year
            hh = int(m.group("h")) if m.groupdict().get("h") else 9
            mm = int(m.group("m")) if m.groupdict().get("m") else 0
            dt = datetime(year, month, day, hh, mm, tzinfo=TIMEZONE)
            # если дата уже прошла в этом году — переносим на следующий
            if dt <= now_local():
                dt = datetime(year + 1, month, day, hh, mm, tzinfo=TIMEZONE)
            return "once_at", {"dt": dt, "text": m.group("task").strip()}

    # не распарсили
    return None

# ────────────────── ПОСТАНОВКА НАПОМИНАНИЙ ──────────────
async def remind_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    text = context.job.data  # сам текст напоминания
    await context.bot.send_message(chat_id, f"⏰ Напоминание: {text}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_allowed(uid):
        # сначала пытаемся принять ключ
        if await try_consume_key(update):
            return
        await request_key(update)
        return

    if not update.message or not update.message.text:
        return

    parsed = parse_text_command(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов …\n"
            "— сегодня в HH:MM …\n"
            "— завтра в HH:MM …\n"
            "— каждый день в HH:MM …\n"
            "— DD <месяц> [в HH:MM] …"
        )
        return

    kind, data = parsed
    jq = context.job_queue
    chat_id = update.effective_chat.id

    if kind == "after":
        when = now_local() + data["delta"]
        jq.run_once(remind_callback, when - now_local(), chat_id=chat_id, data=data["text"])
        await update.message.reply_text(
            f"✅ Ок, напомню в {when.strftime('%Y-%m-%d %H:%M')} — «{data['text']}». (TZ: {TIMEZONE.zone})"
        )
    elif kind == "once_at":
        dt = data["dt"]
        jq.run_once(remind_callback, dt - now_local(), chat_id=chat_id, data=data["text"])
        await update.message.reply_text(
            f"✅ Ок, напомню {dt.strftime('%Y-%m-%d %H:%M')} — «{data['text']}». (TZ: {TIMEZONE.zone})"
        )
    elif kind == "daily_at":
        tm = data["tm"]  # datetime.time с tzinfo
        jq.run_daily(remind_callback, tm, chat_id=chat_id, data=data["text"], name=f"daily:{chat_id}:{data['text']}")
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {tm.strftime('%H:%M')} — «{data['text']}». (TZ: {TIMEZONE.zone})"
        )

# ────────────── HEARTBEAT HTTP-СЕРВЕР (Flask) ───────────
from flask import Flask
import threading

hb = Flask(__name__)

@hb.get("/")
def _root():
    return "✅ Bot is running", 200

def run_heartbeat():
    port = int(os.getenv("PORT", "10000"))
    # без debug, чтобы не создавалось лишних потоков
    hb.run(host="0.0.0.0", port=port, debug=False)

# ───────────────────────── ЗАПУСК ───────────────────────
def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Поднимаем heartbeat-сервер для UptimeRobot/Render
    threading.Thread(target=run_heartbeat, daemon=True).start()

    log.info("Starting bot with polling…")
    app.run_polling(close_loop=False)  # close_loop=False, чтобы не падать на shutdown

if __name__ == "__main__":
    main()
