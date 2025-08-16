import os
import re
import logging
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------- окружение ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()

# Часовой пояс пользователя (по умолчанию КАЛИНИНГРАД)
# Важно: строка должна быть в формате IANA: "Europe/Kaliningrad"
TZ_NAME = os.getenv("TZ", "Europe/Kaliningrad")
LOCAL_TZ = ZoneInfo(TZ_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger("bot")

# ---------- утилиты времени ----------
def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(timezone.utc)

# ---------- handlers ----------
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• напомни сегодня в 16:00 купить молоко\n"
        "• напомни завтра в 9:15 встреча с Андреем\n"
        "• напомни в 22:30 позвонить маме\n"
        "• напомни через 5 минут попить воды\n"
        "• напомни каждый день в 09:30 зарядка\n"
        f"(часовой пояс: {TZ_NAME})"
    )

async def help_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def remind_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = context.job.chat_id
    text = data.get("text", "🔔 Напоминание")
    await context.bot.send_message(chat_id=chat_id, text=f"🔔 {text}")

# ---------- парсер на русском ----------
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_message(msg: str):
    """
    Возвращает dict с одним из ключей:
      - {'once_at': datetime, 'text': str}
      - {'after': timedelta, 'text': str}
      - {'daily_at': time(tzinfo=LOCAL_TZ), 'text': str}
    Если не распознано — None.
    """
    s = (msg or "").strip().lower()
    # убираем ведущие "напомни"/"напомнить"
    s = re.sub(r"^(напомни(ть)?\s+)", "", s)

    # каждый день в HH:MM <текст>
    m = re.match(rf"каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", s)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        text = m.group("text").strip()
        return {"daily_at": time(hour=hh, minute=mm, tzinfo=LOCAL_TZ), "text": text}

    # через X минут/час(ов) <текст>
    m = re.match(
        r"через\s+(?P<n>\d+)\s*(?P<unit>минут(?:у|ы)?|мин|ч(?:ас(?:а|ов)?)?)\s+(?P<text>.+)$",
        s
    )
    if m:
        n = int(m.group("n")); unit = m.group("unit"); text = m.group("text").strip()
        if unit.startswith("мин"):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)
        return {"after": delta, "text": text}

    # сегодня в HH:MM <текст>
    m = re.match(rf"сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$", s)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m")); text = m.group("text").strip()
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_local():
            target += timedelta(days=1)
        return {"once_at": target, "text": text}

    # завтра в HH:MM <текст>
    m = re.match(rf"завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", s)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m")); text = m.group("text").strip()
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": text}

    # в HH:MM <текст> (если прошло — на завтра)
    m = re.match(rf"в\s+{RE_TIME}\s+(?P<text>.+)$", s)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m")); text = m.group("text").strip()
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_local():
            target += timedelta(days=1)
                return {"once_at": target, "text": text}

    return None

async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_message(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "Не понял формат. Примеры:\n"
            "• сегодня в 16:00 купить молоко\n"
            "• завтра в 9:15 встреча с Андреем\n"
            "• в 22:30 позвонить маме\n"
            "• через 5 минут попить воды\n"
            "• каждый день в 09:30 зарядка"
        )
        return

    if "after" in parsed:
        when_local = now_local() + parsed["after"]
        when_utc = to_utc(when_local)
        context.application.job_queue.run_once(
            remind_callback, when=when_utc,
            chat_id=update.effective_chat.id,
            data={"text": parsed["text"]},
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {when_local.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: {TZ_NAME})"
        )
        return

    if "once_at" in parsed:
        when_local = parsed["once_at"]
        when_utc = to_utc(when_local)
        context.application.job_queue.run_once(
            remind_callback, when=when_utc,
            chat_id=update.effective_chat.id,
            data={"text": parsed["text"]},
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {when_local.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: {TZ_NAME})"
        )
        return

    if "daily_at" in parsed:
        context.application.job_queue.run_daily(
            remind_callback,
            time=parsed["daily_at"],
            chat_id=update.effective_chat.id,
            data={"text": parsed["text"]},
            name=f"daily-{update.effective_chat.id}-{parsed['daily_at'].strftime('%H%M')}",
        )
        await update.message.reply_text(
            f"✅ Ежедневное напоминание в {parsed['daily_at'].strftime('%H:%M')} — «{parsed['text']}». (TZ: {TZ_NAME})"
        )
        return

# ---------- приложение / вебхук ----------
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в переменных окружения")

    app = build_app()
    url_path = BOT_TOKEN
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/{url_path}" if PUBLIC_URL else None
    if webhook_url:
        log.info(f"Запускаю с вебхуком: {webhook_url}")
    else:
        log.warning("RENDER_EXTERNAL_URL пуст — сервер стартует без вебхука, сделай повторный Deploy после первого билда.")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,   # может быть None на самом первом запуске
        close_loop=False,
    )
