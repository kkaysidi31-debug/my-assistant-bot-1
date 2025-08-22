# -*- coding: utf-8 -*-
import os
import re
import logging
import threading
import tempfile
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

# Приватный доступ: VIP001 … VIP100 (регистр не важен)
ACCESS_KEYS = {f"vip{n:03d}" for n in range(1, 101)}
USED_KEYS: set[str] = set()
ALLOWED_USERS: set[int] = set()

# ---------------------- KEEP-ALIVE ДЛЯ RENDER ----------------------
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return Response("ok", mimetype="text/plain")

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    log.info("HTTP keep-alive on 0.0.0.0:%s", port)
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ---------------------- ПЛАНИРОВЩИК ----------------------
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# ---------------------- ВСПОМОГАТЕЛЬНОЕ ----------------------
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

RU_MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
    # допускаем именительный на всякий случай
    "январь":1,"февраль":2,"март":3,"апрель":4,"май":5,"июнь":6,"июль":7,
    "август":8,"сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12
}
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def _clean_text(s: str) -> str:
    s = s.strip().lower().replace("ё", "е")
    # убираем «напомни / напомните / напомни-ка …»
    s = re.sub(r"^(напомни(те)?-?ка?\s+)", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def parse_text(text: str):
    """
    Возвращает:
      {"after": timedelta, "text": "..."}            — через N минут/часов …
      {"once_at": datetime, "text": "..."}           — сегодня/завтра/дата
      {"daily_at": time(tzinfo=TIMEZONE), "text": "..."}  — каждый день в HH:MM
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

# ---------------------- ОТПРАВКА СООБЩЕНИЙ ----------------------
async def _send_text(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data  # {"chat_id":..., "text":...}
    try:
        await context.bot.send_message(chat_id=data["chat_id"], text=data["text"])
    except Exception as e:
        log.exception("send_message failed: %s", e)

# ---------------------- ДОСТУП / КЛЮЧИ ----------------------
WELCOME_PRIVATE = "Бот приватный. Введи ключ доступа в формате ABC123."
HELP_TEXT = (
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

def _looks_like_key(s: str) -> bool:
    s = s.strip().lower()
    return bool(re.fullmatch(r"[a-z]{3}\d{3}", s))

async def try_accept_key(update: Update) -> bool:
    """Пробуем принять ключ. True — если это ключ и мы ответили."""
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
        await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.\n\n" + HELP_TEXT)
        return True

    await update.message.reply_text("Неверный ключ ❌.")
    return True

# ---------------------- ХЭНДЛЕРЫ ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(WELCOME_PRIVATE, parse_mode="Markdown")
        return
    await update.message.reply_text(HELP_TEXT)

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        handled = await try_accept_key(update)
        if not handled:
            await update.message.reply_text(WELCOME_PRIVATE, parse_mode="Markdown")
        return

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

    if "once_at" in p:
        when = p["once_at"]
        delay = max(1, int((when - now_local()).total_seconds()))
        context.job_queue.run_once(
            _send_text, when=delay,data={"chat_id": chat_id, "text": p["text"]},
            name=f"once_{chat_id}_{when.timestamp()}"
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{p['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "daily_at" in p:
        hh = p["daily_at"].hour
        mm = p["daily_at"].minute
        first = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if first <= now_local():
            first += timedelta(days=1)
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
        update.message.text = text
        await set_reminder(update, context)

    except Exception as e:
        log.exception("voice handling failed: %s", e)
        await update.message.reply_text("Ошибка при распознавании голосового 😕")

async def transcribe_ogg(path: str) -> str | None:
    """Распознаём через OpenAI Whisper. Поддержаны openai>=1.x и старый SDK."""
    # Попытка новым SDK
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                language="ru",
            )
        return (result or "").strip()
    except Exception:
        pass
    # Попытка старым SDK
    try:
        import openai
        openai.api_key = os.getenv("OPENAI_API_KEY")
        with open(path, "rb") as f:
            res = openai.Audio.transcribe("whisper-1", f, language="ru")
        if isinstance(res, dict):
            return (res.get("text") or "").strip()
        return str(res).strip()
    except Exception as e:
        log.exception("Whisper failed: %s", e)
        return None

# ---------------------- ПОСЛЕ-ИНИЦИАЛИЗАЦИИ ----------------------
async def _post_init(app: Application):
    """Критично: перед polling удаляем вебхук и чистим очередь апдейтов, чтобы не было Conflict."""
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        me = await app.bot.get_me()
        log.info("Webhook removed. Polling as @%s", me.username)
    except Exception as e:
        log.exception("post_init failed: %s", e)

# ---------------------- ЗАПУСК ----------------------
def main():
    # поднимем keep-alive HTTP (для Render)
    threading.Thread(target=run_flask, daemon=True).start()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    application = Application.builder().token(token).build()
    application.post_init = _post_init  # ← ставим хук удаления вебхука

    # хэндлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    log.info("Starting bot with polling…")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )

if __name__ == "__main__":
    main()
