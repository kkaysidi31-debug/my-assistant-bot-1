# -*- coding: utf-8 -*-
import os
import io
import re
import threading
import logging
from datetime import datetime, timedelta, time

from flask import Flask, Response
from pytz import timezone
from apscheduler.schedulers.background import BackgroundScheduler

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger("reminder-bot")

# ---------------------- НАСТРОЙКИ ----------------------
TIMEZONE = timezone("Europe/Kaliningrad")

# Приватный доступ: VIP001 … VIP100
ACCESS_KEYS = {f"vip{n:03d}" for n in range(1, 101)}
USED_KEYS: set[str] = set()
ALLOWED_USERS: set[int] = set()

# ---------------------- ПЛАНИРОВЩИК ----------------------
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# ---------------------- ВСПОМОГАТЕЛЬНОЕ ----------------------
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def _clean_text(s: str) -> str:
    s = s.strip().lower()
    s = s.replace("ё", "е")
    s = re.sub(r"^(напомни(те)?-?ка?\s+)", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def parse_text(text: str):
    """
    Возвращает одно из:
      {"after": timedelta, "text": "..."}
      {"once_at": datetime, "text": "..."}
      {"daily_at": time(tzinfo=TIMEZONE), "text": "..."}
    или None
    """
    t = _clean_text(text)

    # 1) через N минут/часов ...
    m = re.match(r"^через\s+(?P<n>\d+)\s*(?P<u>мин|минуты|минут|час|часа|часов)\b(?:\s+(?P<txt>.+))?$", t)
    if m:
        n = int(m.group("n"))
        unit = m.group("u")
        msg  = (m.group("txt") or "").strip() or "Напоминание"
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"after": delta, "text": msg}

    # 2) сегодня в HH:MM ...
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<txt>.+)$", t)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        msg = m.group("txt").strip()
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if base <= now_local():
            base += timedelta(days=1)
        return {"once_at": base, "text": msg}

    # 3) завтра в HH:MM ...
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<txt>.+)$", t)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        msg = m.group("txt").strip()
        base = now_local().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        base = base.replace(hour=hh, minute=mm)
        return {"once_at": base, "text": msg}

    # 4) каждый день в HH:MM ...
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s*(?P<txt>.*)$", t)
    if m:
        hh = int(m.group("h")); mm = int(m.group("m"))
        msg = (m.group("txt") or "").strip() or "Ежедневное напоминание"
        return {"daily_at": time(hh, mm, tzinfo=TIMEZONE), "text": msg}

    # 5) DD <месяц> [в HH:MM] ...
    m = re.match(rf"^(?P<d>\d{{1,2}})\s+(?P<mon>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<txt>.+)$", t)
    if m:
        day = int(m.group("d"))
        mon_name = m.group("mon")
        mon = RU_MONTHS.get(mon_name)
        if mon:
            hh = int(m.group("h")) if m.group("h") else 9
            mm = int(m.group("m")) if m.group("m") else 0
            msg = m.group("txt").strip()
            year = now_local().year
            run_at = datetime(year, mon, day, hh, mm, tzinfo=TIMEZONE)
            if run_at <= now_local():
                run_at = datetime(year + 1, mon, day, hh, mm, tzinfo=TIMEZONE)
            return {"once_at": run_at, "text": msg}

    return None

# ---------------------- ЗАДАЧИ ----------------------
async def _send_text(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data  # {"chat_id":..., "text":...}
    try:
        await context.bot.send_message(chat_id=data["chat_id"], text=data["text"])
    except Exception as e:
        log.exception("send_message failed: %s", e)

def schedule_once(run_at: datetime, chat_id: int, text: str):
    scheduler.add_job(
        lambda: None, "date", run_date=run_at
    )  # dummy для ID предсказуемости
    # через JobQueue (точнее в PTB): сделаем через run_once с delay
    # но мы уже используем APScheduler для триггера, поэтому просто прокинем в PTB через 1 сек:
    # Упростим: APScheduler запланирует «будильник», который из PTB мы не можем вызвать напрямую.
    # Поэтому сделаем следующую хитрость: когда добавляем — сразу считаем delay и ставим PTB job.
    # (ровно так же делали раньше)
    pass  # будет выставлено в основном обработчике через context.job_queue.run_once

# ---------------------- ОБРАБОТЧИКИ ----------------------
HELP_TEXT = (
    "Примеры:\n"
    "• напомни сегодня в 16:00 купить молоко\n"
    "• напомни завтра в 9:15 встреча с Андреем\n"
    "• напомни в 22:30 позвонить маме\n"
    "• напомни через 5 минут попить воды\n"
    "• напомни каждый день в 09:30 зарядка\n"
    "• напомни 30 августа в 09:00 заплатить за кредит\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

WELCOME_PRIVATE = (
    "Бот приватный. Введи ключ доступа в формате ABC123."
)

WELCOME_OK = (
    "Ключ принят ✅. Теперь можно ставить напоминания.\n\n"
    "Бот запущен ✅\n\n" + HELP_TEXT
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(WELCOME_PRIVATE, parse_mode="Markdown")
        return
    await update.message.reply_text("Бот запущен ✅\n\n" + HELP_TEXT)

def _looks_like_key(s: str) -> bool:
    s = s.strip().lower()
    return bool(re.fullmatch(r"[a-z]{3}\d{3}", s))

async def try_accept_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Пробуем принять ключ. Вернёт True, если это был ключ (и мы ответили)."""
    if not update.message or not update.message.text:
        return False
    text = update.message.text.strip().lower()
    if not _looks_like_key(text):
        return False

    if text in USED_KEYS:
        await update.message.reply_text("Этот ключ уже использован ❌.")
        return True

    if text in ACCESS_KEYS:
        USED_KEYS.add(text)
        ALLOWED_USERS.add(update.effective_user.id)
        await update.message.reply_text(WELCOME_OK)
        return True

    await update.message.reply_text("Неверный ключ ❌.")
    return True

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # доступ
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        # если это ключ — обработаем
        handled = await try_accept_key(update, context)
        if not handled:
            await update.message.reply_text(WELCOME_PRIVATE, parse_mode="Markdown")
        return

    # текст → задача
    text = (update.message.text or "").strip()
    p = parse_text(text)
    if not p:
        await update.message.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов …\n"
            "— сегодня в HH:MM …\n"
            "— завтра в HH:MM …\n"
            "— каждый день в HH:MM …\n"
            "— DD <месяц> [в HH:MM] …"
        )
        return

    chat_id = update.effective_chat.id

    if "after" in p:
        when = now_local() + p["after"]
        delay = (when - now_local()).total_seconds()
        context.job_queue.run_once(
            _send_text, when=delay,
            data={"chat_id": chat_id, "text": p["text"]},
            name=f"once_{chat_id}_{when.timestamp()}"
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{p['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "once_at" in p:
        when = p["once_at"]
        delay = max(1, int((when - now_local()).total_seconds()))
        context.job_queue.run_once(
            _send_text, when=delay,
            data={"chat_id": chat_id, "text": p["text"]},
            name=f"once_{chat_id}_{when.timestamp()}"
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{p['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "daily_at" in p:
        hh = p["daily_at"].hour
        mm = p["daily_at"].minute
        # если время на сегодня прошло — первая сработка завтра
        first = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if first <= now_local():
            first += timedelta(days=1)
        # период 24 часа
        delay = max(1, int((first - now_local()).total_seconds()))
        context.job_queue.run_repeating(
            _send_text, interval=24*60*60, first=delay,
            data={"chat_id": chat_id, "text": p["text"]},
            name=f"daily_{chat_id}_{hh:02d}{mm:02d}"
        )
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02d}:{mm:02d} — «{p['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

# -------- голосовые: распознавание Whisper (если есть OPENAI_API_KEY) --------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(WELCOME_PRIVATE, parse_mode="Markdown")
        return

    if not os.getenv("OPENAI_API_KEY"):
        await update.message.reply_text("Для распознавания речи нужен OPENAI_API_KEY в переменных окружения.")
        return

    try:
        voice = update.message.voice
        if not voice:
            await update.message.reply_text("Не вижу голосового сообщения.")
            return

        file = await context.bot.get_file(voice.file_id)
        tmp_path = "/tmp/voice.ogg"
        await file.download_to_drive(custom_path=tmp_path)

        text = await transcribe_ogg(tmp_path)
        if not text:
            await update.message.reply_text("Не удалось распознать речь.")
            return

        # прогоняем распознанный текст через общий парсер
        fake_update = Update(
            update.update_id,
            message=update.message  # повторно используем сообщение для ответа
        )
        # заменим текст в message для downstream-логики
        fake_update.message.text = text
        await set_reminder(fake_update, context)

    except Exception as e:
        log.exception("voice handling failed: %s", e)
        await update.message.reply_text("Ошибка при распознавании голосового 😕")

async def transcribe_ogg(path: str) -> str | None:
    """
    Пытаемся сначала новым SDK, потом старым.
    """
    try:
        # Новый SDK (openai>=1.x)
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                language="ru"
            )
        return result.strip()
    except Exception:
        pass

    try:
        # Старый SDK (openai<1.x)
        import openai
        openai.api_key = os.getenv("OPENAI_API_KEY")
        with open(path, "rb") as f:
            result = openai.Audio.transcribe("whisper-1", f, language="ru")
        # result — dict со строкой в поле 'text'
        if isinstance(result, dict):
            return (result.get("text") or "").strip()
        return str(result).strip()
    except Exception as e:
        log.exception("whisper legacy failed: %s", e)
        return None

# ---------------------- FLASK "PORT BIND" ДЛЯ RENDER ----------------------
app_http = Flask(__name__)

@app_http.get("/")
def health():
    return Response("ok", mimetype="text/plain")

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    log.info("Running HTTP on 0.0.0.0:%s", port)
    app_http.run(host="0.0.0.0", port=port)

# ---------------------- ЗАПУСК ----------------------
async def _show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

def main():
    # Поднимаем Flask в отдельном потоке (для Render)
    threading.Thread(target=run_flask, daemon=True).start()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    application = Application.builder().token(token).build()

    # /start
    application.add_handler(CommandHandler("start", start))
    # текстовые команды/фразы
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))
    # голосовые
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    log.info("Starting bot with polling...")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
