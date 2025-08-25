# -*- coding: utf-8 -*-
import os
import re
import logging
import threading
import tempfile
from datetime import datetime, timedelta, time
from typing import List, Dict, Any

from flask import Flask, Response
from pytz import timezone

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ───────────────────────── ЛОГИ ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger("reminder-bot")

# ─────────────────── ОБЩИЕ НАСТРОЙКИ ───────────────────
TIMEZONE = timezone("Europe/Kaliningrad")

def now_local() -> datetime:
    return datetime.now(TIMEZONE)

# Приватный доступ: реальные ключи VIP001..VIP100 (регистр неважен)
ACCESS_KEYS = {f"VIP{n:03d}" for n in range(1, 101)}
USED_KEYS: set[str] = set()
ALLOWED_USERS: set[int] = set()

# ───────────── Heartbeat HTTP для Render/UptimeRobot ───────────
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return Response("✅ Bot is running", mimetype="text/plain", status=200)

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    log.info("HTTP keep-alive on 0.0.0.0:%s", port)
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ───────────────────── ХРАНИЛИЩЕ ДЕЛ ───────────────────
# user_id -> список элементов:
# { "kind": "once"|"daily", "when": datetime | None, "hh": int|None, "mm": int|None,
#   "text": str, "job_name": str }
SCHEDULES: Dict[int, List[Dict[str, Any]]] = {}

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")

# ──────────────── ПОДСКАЗКИ/ТЕКСТЫ ─────────────────────
WELCOME_PRIVATE = "Бот приватный. Введите ключ доступа в формате ABC123."
HELP_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "(часовой пояс: Europe/Kaliningrad)\n\n"
    "Команды:\n"
    "• /affairs — показать список дел\n"
    "• /affairs delete N — удалить дело №N\n"
)

# ──────────────────── НОРМАЛИЗАЦИЯ ТЕКСТА ───────────────────
RU_MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
    # допустим именительный тоже
    "январь":1,"февраль":2,"март":3,"апрель":4,"май":5,"июнь":6,"июль":7,
    "август":8,"сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12
}
RE_TIME = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"

def _clean_text(s: str) -> str:
    s = (s or "").strip().lower().replace("ё", "е")
    # убираем «напомни / напомните / напомни-ка …» если пользователь добавил
    s = re.sub(r"^(напомни(те)?-?ка?\s+)", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def parse_text(text: str):
    """
    Возвращает:
      {"after": timedelta, "text": "..."}                      — через N минут/часов
      {"once_at": datetime, "text": "..."}                     — сегодня/завтра/дата
      {"daily_at": time(tzinfo=TIMEZONE), "text": "..."}       — каждый день в HH:MM
      или None
    """
    t = _clean_text(text)

    # 1) через N минут/часов ...
    m = re.match(r"^через\s+(?P<n>\d+)\s*(?P<u>мин|минуты|минут|м|час|часа|часов|ч)\b(?:\s+(?P<txt>.+))?$", t)
    if m:
        n = int(m.group("n"))
        unit = m.group("u")
        msg  = (m.group("txt") or "").strip() or "Напоминание"
        delta = timedelta(minutes=n) if unit.startswith(("м","мин")) else timedelta(hours=n)
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

# ─────────────────── ДОСТУП / КЛЮЧИ ────────────────────
async def request_key(update: Update):
    await update.message.reply_text(WELCOME_PRIVATE, parse_mode="Markdown")

def looks_like_key(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]{3}\d{3}", (s or "").strip()))

async def try_accept_key(update: Update) -> bool:
    """Пробуем принять ключ. True — если обработали как ключ (успех/ошибка)."""
    if not update.message or not update.message.text:
        return False
    candidate = update.message.text.strip().upper()
    if not looks_like_key(candidate):
        return False
    if candidate in USED_KEYS:
        await update.message.reply_text("Этот ключ уже использован ❌.")
        return True
    if candidate in ACCESS_KEYS:
        USED_KEYS.add(candidate)
        ALLOWED_USERS.add(update.effective_user.id)
        await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.\n\n" + HELP_TEXT)
        return True
    await update.message.reply_text("Неверный ключ ❌.")
    return True

# ──────────────────── ХЭНДЛЕРЫ КОМАНД ──────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await request_key(update)
        return
    await update.message.reply_text(HELP_TEXT)

# Служебное: отправка сообщения из JobQueue
async def _send_text(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(chat_id=context.job.chat_id, text=context.job.data)
    except Exception as e:
        log.exception("send_message failed: %s", e)

# Добавление дела в память
def _remember(uid: int, item: Dict[str, Any]):
    lst = SCHEDULES.setdefault(uid, [])
    lst.append(item)

# Пересчёт «ближайшего времени» для сортировки (ежедневные — следующее срабатывание)
def _next_time_for(item: Dict[str, Any]) -> datetime:
    if item["kind"] == "once":
        return item["when"]
    hh, mm = item["hh"], item["mm"]
    first = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
    if first <= now_local():
        first += timedelta(days=1)
    return first

# /affairs — показать список; /affairs delete N — удалить №N
async def list_or_delete_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await request_key(update)
        return

    args = context.args or []
    # Удаление: "/affairs delete N"
    if len(args) >= 1 and args[0].lower() == "delete":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Использование: /affairs delete N (номер из списка /affairs)")
            return
        index = int(args[1])
        items = SCHEDULES.get(uid, [])
        if not items:
            await update.message.reply_text("Список дел пуст.")
            return
        ordered = sorted(items, key=_next_time_for)
        if index < 1 or index > len(ordered):
            await update.message.reply_text(f"Нет пункта №{index}.")
            return
        to_del = ordered[index - 1]
        # удаляем job из JobQueue
        job_name = to_del.get("job_name")
        deleted = False
        if job_name:
            jobs = context.job_queue.get_jobs_by_name(job_name)
            for j in jobs:
                j.schedule_removal()
                deleted = True
        # удаляем из памяти
        items.remove(to_del)
        await update.message.reply_text(
            f"🗑 Удалено: {_next_time_for(to_del).strftime('%d.%m.%Y %H:%M')} — {to_del['text']}"
            + ("" if deleted else " (заметка удалена, но задача могла уже выполниться)")
        )
        return

    # Показ списка
    items = SCHEDULES.get(uid, [])
    future_items = []
    for it in items:
        if it["kind"] == "once":
            if it["when"] >= now_local():
                future_items.append(it)
        else:
            future_items.append(it)  # daily показываем всегда

    if not future_items:
        await update.message.reply_text("У вас пока нет активных дел ✅")
        return

    ordered = sorted(future_items, key=_next_time_for)
    lines = []
    for i, it in enumerate(ordered, start=1):
        if it["kind"] == "once":
            lines.append(f"{i}. {fmt_dt(it['when'])} — {it['text']}")
        else:
            lines.append(f"{i}. {it['hh']:02d}:{it['mm']:02d} — {it['text']} (ежедневно)")
    await update.message.reply_text("Ваши ближайшие дела:\n" + "\n".join(lines))

# Синоним: /affairs_delete N
async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /affairs_delete N")
        return
    context.args = ["delete", context.args[0]]
    await list_or_delete_affairs(update, context)

# ───────────────── ОБРАБОТКА ТЕКСТА/ГОЛОСА ─────────────
async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        handled = await try_accept_key(update)
        if not handled:
            await request_key(update)
        return

    text = (update.message.text or "").strip()
    p = parse_text(text)
    if not p:
        await update.message.reply_text(
            "❓ Не понял формат. Примеры:\n"
            "— сегодня в 16:00 купить молоко\n"
            "— завтра в 9:15 встреча\n"
            "— в 22:30 позвонить маме\n"
            "— через 5 минут попить воды\n"
            "— каждый день в 09:30 зарядка\n"
            "— 30 августа в 09:00 заплатить за кредит"
        )
        return

    chat_id = update.effective_chat.id

    if "after" in p:
        when = now_local() + p["after"]
        delay = max(1, int((when - now_local()).total_seconds()))
        job_name = f"{uid}:once:{int(when.timestamp())}:{abs(hash(p['text']))%100000}"
        context.job_queue.run_once(
            _send_text, when=delay,
            chat_id=chat_id, data=p["text"], name=job_name
        )
        # запомним
        lst = SCHEDULES.setdefault(uid, [])
        lst.append({"kind":"once","when":when,"hh":None,"mm":None,"text":p["text"],"job_name":job_name})
        await update.message.reply_text(
            f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{p['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

    if "once_at" in p:
        when = p["once_at"]
        delay = max(1, int((when - now_local()).total_seconds()))
        job_name = f"{uid}:once:{int(when.timestamp())}:{abs(hash(p['text']))%100000}"
        context.job_queue.run_once(
            _send_text, when=delay,chat_id=chat_id, data=p["text"], name=job_name
        )
        lst = SCHEDULES.setdefault(uid, [])
        lst.append({"kind":"once","when":when,"hh":None,"mm":None,"text":p["text"],"job_name":job_name})
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
        job_name = f"{uid}:daily:{hh:02d}{mm:02d}:{abs(hash(p['text']))%100000}"
        delay = max(1, int((first - now_local()).total_seconds()))
        context.job_queue.run_repeating(
            _send_text, interval=24*60*60, first=delay,
            chat_id=chat_id, data=p["text"], name=job_name
        )
        lst = SCHEDULES.setdefault(uid, [])
        lst.append({"kind":"daily","when":None,"hh":hh,"mm":mm,"text":p["text"],"job_name":job_name})
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hh:02d}:{mm:02d} — «{p['text']}». (TZ: Europe/Kaliningrad)"
        )
        return

# Голосовые → Whisper → в set_reminder
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await request_key(update)
        return

    if not os.getenv("OPENAI_API_KEY"):
        await update.message.reply_text("Для распознавания речи нужен OPENAI_API_KEY в переменных окружения.")
        return

    voice = update.message.voice
    if not voice:
        await update.message.reply_text("Не нашёл голосовое сообщение.")
        return

    tg_file = await context.bot.get_file(voice.file_id)
    tmp_path = "/tmp/voice.ogg"
    await tg_file.download_to_drive(tmp_path)

    try:
        text = await transcribe_ogg(tmp_path)
        if not text:
            await update.message.reply_text("Не удалось распознать речь 😕")
            return
        update.message.text = text
        await set_reminder(update, context)
    except Exception as e:
        log.exception("voice handling failed: %s", e)
        await update.message.reply_text("Ошибка при распознавании голосового 😕")
    finally:
        try: os.remove(tmp_path)
        except Exception: pass

async def transcribe_ogg(path: str) -> str | None:
    # Новый SDK (openai>=1.x)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(path, "rb") as f:
            res = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text", language="ru"
            )
        return (res or "").strip()
    except Exception:
        pass
    # Старый SDK (openai<1.x)
    try:
        import openai
        openai.api_key = os.getenv("OPENAI_API_KEY")
        with open(path, "rb") as f:
            res = openai.Audio.transcribe("whisper-1", f, language="ru")
        if isinstance(res, dict):
            return (res.get("text") or "").strip()
        return str(res).strip()
    except Exception as e:
        log.exception("whisper legacy failed: %s", e)
        return None

# ───────────── ПОСЛЕ-ИНИЦИАЛИЗАЦИИ (anti-conflict + меню) ────────────
async def _post_init(app: Application):
    try:
        # Удаляем вебхук и чистим очередь — чтобы polling не конфликтовал
        await app.bot.delete_webhook(drop_pending_updates=True)
        # Команды для меню в Telegram (кнопка /)
        await app.bot.set_my_commands([
            BotCommand("start", "помощь и примеры"),
            BotCommand("affairs", "список дел / удалить: /affairs delete N"),
            BotCommand("affairs_delete", "удалить дело по номеру"),
        ])
        me = await app.bot.get_me()
        log.info("Webhook removed, commands set. Polling as @%s", me.username)
    except Exception as e:
        log.exception("post_init failed: %s", e)
        
# ───────────────────────── ЗАПУСК ───────────────────────
def main():
    # поднимем heartbeat веб-сервер
    threading.Thread(target=run_flask, daemon=True).start()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    app = Application.builder().token(token).build()
    app.post_init = _post_init

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", list_or_delete_affairs))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete))

    # сообщения
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder))

    log.info("Starting bot with polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()
