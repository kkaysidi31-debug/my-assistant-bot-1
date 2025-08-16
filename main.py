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
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Bot is running!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

# ------------------------ ВСПОМОГАТЕЛЬНОЕ ------------------------
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

# месяцы по-русски (любая падежная форма примется по префиксу)
MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "сент": 9,
    "окт": 10, "ноя": 11, "дек": 12,
}
def month_from_ru(name: str) -> int | None:
    s = name.strip().lower()
    # нормализуем общие окончания (августа -> авг, сентября -> сент)
    s = s.replace("ё", "е")
    candidates = [k for k in MONTHS if s.startswith(k)]
    return MONTHS[candidates[0]] if candidates else None

# ------------------------ ПАРСЕР ФРАЗ ------------------------
def parse_reminder(text: str):
    """
    Возвращает dict:
      {"once_at": datetime, "text": "..."}  — одноразовое
      {"daily": (hh, mm),  "text": "..."}  — ежедневное
      или None, если не распознал.
    """
    t = text.strip()
    now_local = datetime.now(TZ)

    # через N минут/часов
    m = re.match(r"напомни\s+через\s+(\d+)\s*(минут[уы]?|час[аов]?)\s+(.+)$", t, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        what = m.group(3).strip()
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"once_at": now_local + delta, "text": what}

    # сегодня в HH:MM
    m = re.match(rf"напомни\s+сегодня\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local:
            target += timedelta(days=1)
        return {"once_at": target, "text": what}

    # завтра в HH:MM
    m = re.match(rf"напомни\s+завтра\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        base = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": what}

    # в HH:MM (на сегодня/завтра)
    m = re.match(rf"напомни\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local:
            target += timedelta(days=1)
        return {"once_at": target, "text": what}

    # каждый день в HH:MM
    m = re.match(rf"напомни\s+каждый\s+день\s+в\s+{RE_TIME}\s+(.+)$", t, re.I)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        what = m.group(4).strip()
        return {"daily": (hh, mm), "text": what}

    # ---- НОВОЕ: «напомни 30 августа [2025] [в 16:00] <текст>» ----
    # год и время — опционально. Если времени нет — ставим 09:00.
    m = re.match(
        rf"напомни\s+(?P<d>\d{{1,2}})\s+(?P<mon>[А-Яа-яЁё]+)\s*(?P<y>\d{{4}})?(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$",
        t, re.I
    )
    if m:
        day = int(m.group("d"))
        mon = month_from_ru(m.group("mon") or "")
        if not mon:
            return None
        year = int(m.group("y")) if m.group("y") else now_local.year
        # время
        if m.group("h") and m.group("m"):
            hh, mm = int(m.group("h")), int(m.group("m"))
        else:
            hh, mm = 9, 0  # время по умолчанию 09:00
        what = (m.group("text") or "").strip()
        try:
            target = TZ.localize(datetime(year, mon, day, hh, mm, 0, 0))
            # если дата/время уже прошло в текущем году без явного года — переносим на следующий
            if not m.group("y") and target < now_local:
                target = TZ.localize(datetime(year + 1, mon, day, hh, mm, 0, 0))
            return {"once_at": target, "text": what}
        except ValueError:
            return None

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

# ------------------------ ОБРАБОТЧИКИ ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• напомни сегодня в 16:00 купить молоко\n"
        "• напомни завтра в 9:15 встреча с Андреем\n"
        "• напомни 30 августа в 10:00 заплатить за кредит\n"
        "• напомни 30 августа заплатить за кредит   (в 09:00 по умолчанию)\n"
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
        await update.message.reply_text(
            "⚠️ Не понял формат. Примеры: "
            "«напомни 30 августа в 16:00 оплатить ЖКХ», "
            "«напомни через 10 минут сделать перерыв», "
            "«напомни каждый день в 09:30 зарядка»."
        )
        return

    chat_id = update.message.chat_id

    if "once_at" in parsed:
        target = parsed["once_at"]
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
    import os
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("Starting bot with polling...")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
