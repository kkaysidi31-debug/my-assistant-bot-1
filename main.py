import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from pytz import timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

import aiohttp
import tempfile

# -------------------- НАСТРОЙКИ --------------------

TIMEZONE = timezone("Europe/Kaliningrad")

# Генерим одноразовые ключи VIP001..VIP100
ACCESS_KEYS: Dict[str, Optional[int]] = {f"VIP{i:03d}": None for i in range(1, 101)}
ALLOWED_USERS: set[int] = set()

# Месяцы по-русски → номер месяца
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

# Шаблон времени HH:MM
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# -------------------- УТИЛИТЫ --------------------


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def parse_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает один из вариантов:
      {"after": timedelta, "text": "..."}                       — через N минут/часов
      {"once_at": datetime, "text": "..."}                      — сегодня/завтра/дата
      {"daily_at": {"h": int, "m": int}, "text": "..."}         — каждый день в HH:MM
    Если не распознано — None.
    """
    t = text.strip().lower()

    # 1) через N минут/часов ...
    m = re.match(rf"через\s+(?P<n>\d+)\s+(минут|минуту|мин|час|часа|часов)\s+(?P<text>.+)$", t)
    if m:
        n = int(m.group("n"))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"after": delta, "text": m.group("text").strip()}

    # 2) сегодня в HH:MM ...
    m = re.match(rf"сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now_local():
            # сегодня время прошло — на завтра
            target += timedelta(days=1)
        return {"once_at": target, "text": m.group("text").strip()}

    # 3) завтра в HH:MM ...
    m = re.match(rf"завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local() + timedelta(days=1)
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"once_at": target, "text": m.group("text").strip()}

    # 4) каждый день в HH:MM ...
    m = re.match(rf"каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return {"daily_at": {"h": hh, "m": mm}, "text": m.group("text").strip()}

    # 5) «30 августа [в 09:00] ...»
    #    Если время не указано — по умолчанию 09:00
    m = re.match(
        rf"(?P<day>\d{{1,2}})\s+(?P<month>{"|".join(MONTHS.keys())})(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$",
        t
    )
    if m:
        day = int(m.group("day"))
        month = MONTHS[m.group("month")]
        year = now_local().year
        if m.group("h") and m.group("m"):
            hh, mm = int(m.group("h")), int(m.group("m"))
        else:
            hh, mm = 9, 0  # дефолт

        target = datetime(year, month, day, hh, mm, tzinfo=TIMEZONE)
        # если дата уже прошла в этом году — переносим на следующий
        if target < now_local():
            target = datetime(year + 1, month, day, hh, mm, tzinfo=TIMEZONE)
        return {"once_at": target, "text": m.group("text").strip()}

    return None


async def remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "Напоминание")
    try: await context.bot.send_message(chat_id=chat_id, text=f"⏰ {text}")
    except Exception as e:
        log.exception("Ошибка отправки напоминания: %s", e)


def schedule_parsed(
    parsed: Dict[str, Any], chat_id: int, job_id_prefix: str, scheduler_: AsyncIOScheduler
):
    if "after" in parsed:
        run_time = now_local() + parsed["after"]
        scheduler_.add_job(
            remind, DateTrigger(run_date=run_time),
            id=f"{job_id_prefix}:{run_time.isoformat()}",
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            replace_existing=True
        )
        return run_time

    if "once_at" in parsed:
        run_time = parsed["once_at"]
        scheduler_.add_job(
            remind, DateTrigger(run_date=run_time),
            id=f"{job_id_prefix}:{run_time.isoformat()}",
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            replace_existing=True
        )
        return run_time

    if "daily_at" in parsed:
        hh, mm = parsed["daily_at"]["h"], parsed["daily_at"]["m"]
        scheduler_.add_job(
            remind,
            CronTrigger(hour=hh, minute=mm),
            id=f"{job_id_prefix}:daily-{hh:02d}-{mm:02d}",
            kwargs={"data": {"chat_id": chat_id, "text": parsed["text"]}},
            replace_existing=True
        )
        # возвращаем ближайшее срабатывание просто как справку
        now = now_local()
        first = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if first <= now:
            first += timedelta(days=1)
        return first

    return None


async def transcribe_voice(file_path: str) -> Optional[str]:
    """
    Шлём файл в OpenAI Whisper и возвращаем текст.
    Нужен OPENAI_API_KEY в переменных окружения.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.warning("Нет OPENAI_API_KEY — распознавание отключено.")
        return None

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    form = aiohttp.FormData()
    form.add_field("model", "whisper-1")
    # язык не обязателен, но поможем распознаванию
    form.add_field("language", "ru")

    with open(file_path, "rb") as f:
        form.add_field("file", f, filename=os.path.basename(file_path), content_type="audio/ogg")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form) as resp:
            if resp.status != 200:
                txt = await resp.text()
                log.error("Whisper error %s: %s", resp.status, txt)
                return None
            js = await resp.json()
            return js.get("text")


# -------------------- ОБРАБОТЧИКИ --------------------


START_HELP = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• напомни сегодня в 16:00 купить молоко\n"
    "• напомни завтра в 9:15 встреча с Андреем\n"
    "• напомни в 22:30 позвонить маме\n"
    "• напомни через 5 минут попить воды\n"
    "• напомни каждый день в 09:30 зарядка\n"
    "• напомни 30 августа в 09:00 заплатить за кредит\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

ACCESS_PROMPT = (
    "Этот бот приватный. Введите ключ доступа в формате ABC123."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text(ACCESS_PROMPT, disable_web_page_preview=True)
        return
    await update.message.reply_text(START_HELP)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()

    # Если пользователь ещё не прошёл по ключу — пробуем принять ключ
    if uid not in ALLOWED_USERS:
        # принимаем ключи вида VIP001..VIP100, которые ещё не использованы
        if re.fullmatch(r"VIP\d{3}", txt) and txt in ACCESS_KEYS and ACCESS_KEYS[txt] is None:
            ACCESS_KEYS[txt] = uid
            ALLOWED_USERS.add(uid)
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.\n\n" + START_HELP)
        else:
            await update.message.reply_text(ACCESS_PROMPT, disable_web_page_preview=True)
        return

    parsed = parse_text(txt)
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

    when = schedule_parsed(parsed, update.effective_chat.id, f"u{uid}", scheduler)
    if "after" in parsed:
        await update.message.reply_text(f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}».")
    elif "once_at" in parsed:
        await update.message.reply_text(f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}».")
    else:
        # daily
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {parsed['daily_at']['h']:02d}:{parsed['daily_at']['m']:02d} — "
            f"«{parsed['text']}». Первый раз: {when.strftime('%Y-%m-%d %H:%M')}."
        )


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text(ACCESS_PROMPT, disable_web_page_preview=True)
        return

    voice = update.message.voice
    if not voice:
        return

    # Скачиваем .ogg
    with tempfile.TemporaryDirectory() as td:
        local_path = os.path.join(td, "voice.ogg")
        tg_file = await voice.get_file()
        await tg_file.download_to_drive(local_path)

        # Распознаём
        text = await transcribe_voice(local_path)

    if not text:
        await update.message.reply_text("Не удалось распознать голосовое 😕")
        return

    # Делаем видимым распознанный текст и ставим задачу
    await update.message.reply_text(f"🗣 Распознал: «{text}»")
    fake_update = Update(update.update_id, message=update.message)
    fake_update.message.text = text
    await text_handler(fake_update, context)


# -------------------- СТАРТ --------------------


async def on_startup(app: Application):
    # Стартуем планировщик
    if not scheduler.running:
        scheduler.start()

    # Готовим URL вебхука
    public_host = os.getenv("RENDER_EXTERNAL_HOSTNAME") or os.getenv("PUBLIC_URL", "").replace("https://", "").replace("http://", "")
    if not public_host:
        log.warning("PUBLIC URL не найден. Установи переменную окружения RENDER_EXTERNAL_HOSTNAME или PUBLIC_URL.")
        # Немного подождём, чтобы Render успел прописать URL, и упадём — следующая деплой-итерация подхватит
        await asyncio.sleep(5)
        raise SystemExit(1)

    token = os.getenv("BOT_TOKEN")
    if not token:
        log.error("Нет переменной окружения BOT_TOKEN")
        raise SystemExit(1)

    webhook_url = f"https://{public_host}/{token}"
    log.info("Ставлю вебхук: %s", webhook_url)
    await app.bot.set_webhook(url=webhook_url, allowed_updates=["message"])


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    application = Application.builder().token(token).build()

    # Команды/хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))

    # На старте — установим вебхук
    application.post_init = on_startup

    # Запуск только WEBHOOK-сервера (без polling!)
    port = int(os.environ.get("PORT", "8000"))
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=token,  # путь = токен
        webhook_url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{token}"
        if os.getenv("RENDER_EXTERNAL_HOSTNAME")
        else None,
        close_loop=False,  # чтобы не ломать event loop на Render
    )
    
    
if __name__ == "__main__":
    main()
