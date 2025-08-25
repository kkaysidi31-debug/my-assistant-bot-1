# main.py
import os
import re
import json
import threading
import logging
from io import BytesIO
from datetime import datetime, timedelta, time, timezone as dt_timezone
from zoneinfo import ZoneInfo

from flask import Flask, Response

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    TypeHandler, filters, ApplicationHandlerStop
)

# =============== ЛОГИ =================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

# =============== НАСТРОЙКИ ===============
TIMEZONE = ZoneInfo("Europe/Kaliningrad")
PORT = int(os.getenv("PORT", "10000"))

# ADMIN IDS (через запятую в ENV), иначе список пуст
ADMIN_IDS = set()
if os.getenv("ADMIN_IDS"):
    try:
        ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip()}
    except Exception:
        pass

# =============== ПРИВАТНЫЕ КЛЮЧИ =================
VALID_KEYS = {f"VIP{str(i).zfill(3)}" for i in range(1, 101)}  # VIP001..VIP100
USED_KEYS_FILE = "used_keys.json"
ALLOWED_FILE = "allowed_users.json"

def load_json_set(path: str) -> set:
    try:
        return set(json.load(open(path, "r", encoding="utf-8")))
    except Exception:
        return set()

def save_json_set(path: str, s: set):
    try:
        json.dump(list(s), open(path, "w", encoding="utf-8"))
    except Exception:
        pass

USED_KEYS = load_json_set(USED_KEYS_FILE)
ALLOWED_USERS = load_json_set(ALLOWED_FILE)

def allow_user(uid: int, key: str):
    ALLOWED_USERS.add(uid)
    USED_KEYS.add(key)
    save_json_set(ALLOWED_FILE, ALLOWED_USERS)
    save_json_set(USED_KEYS_FILE, USED_KEYS)

# =============== ТЕХРАБОТЫ ===============
MAINTENANCE = False
PENDING_CHATS_FILE = "pending_chats.json"
def load_pending() -> set:
    return load_json_set(PENDING_CHATS_FILE)
def save_pending(s: set):
    save_json_set(PENDING_CHATS_FILE, s)
PENDING_CHATS = load_pending()

# =============== ХРАНЕНИЕ ДЕЛ ===============
TASKS_FILE = "tasks.json"  # {str(user_id): [ taskdict, ... ]}
def load_tasks() -> dict:
    try:
        return json.load(open(TASKS_FILE, "r", encoding="utf-8"))
    except Exception:
        return {}
def save_tasks(d: dict):
    try:
        json.dump(d, open(TASKS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass

TASKS = load_tasks()  # user_id -> list of tasks

def add_task(uid: int, task: dict):
    lst = TASKS.get(str(uid), [])
    lst.append(task)
    TASKS[str(uid)] = lst
    save_tasks(TASKS)

def delete_task_by_index(uid: int, idx: int) -> dict | None:
    lst = TASKS.get(str(uid), [])
    if 1 <= idx <= len(lst):
        task = lst.pop(idx - 1)
        TASKS[str(uid)] = lst
        save_tasks(TASKS)
        return task
    return None

def list_tasks(uid: int) -> list[dict]:
    lst = TASKS.get(str(uid), [])
    # сортируем: сначала однократные по времени, затем ежедневные по времени
    def keyer(t):
        if t.get("kind") == "daily":
            return (1, t.get("time", "00:00"))
        else:
            return (0, t.get("due_iso", "9999-12-31T23:59:59"))
    return sorted(lst, key=keyer)

# =============== МЕСЯЦЫ (ru) ===============
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

# === ВСПОМОГАТЕЛЬНЫЕ ===
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(dt_timezone.utc)

def normalize_minutes_hours(n: int, unit: str) -> timedelta:
    return timedelta(minutes=n) if "мин" in unit else timedelta(hours=n)

# =============== РАСПОЗНАВАНИЕ РЕЧИ (опционально) ===============
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # если нет, голос обрабатывать не будем
openai_client = None

if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client initialized for voice transcription.")
    except Exception as e:
        log.warning("Failed to init OpenAI client: %s", e)
        openai_client = None

async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Скачивает voice OGG и отправляет в OpenAI Whisper. Возвращает распознанный текст или None."""
    if not openai_client:
        await update.message.reply_text("⚠️ Голос к тексту недоступен (нет OPENAI_API_KEY).")
        return None
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        bio = BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        # OpenAI API: audio.transcriptions.create
        tr = openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.ogg", bio, "audio/ogg")
        )
        text = tr.text.strip()
        return text
    except Exception as e:
        log.exception("Transcribe error: %s", e)
        await update.message.reply_text("⚠️ Не удалось распознать голос.")
        return None

# =============== ПАРСИНГ РУССКИХ ФРАЗ ===============
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parsedate(text: str) -> dict | None:
    """
    Возвращает один из вариантов:
    {"after": timedelta, "text": "..."}
    {"once_at": datetime_local, "text": "..."}        # локальное aware
    {"daily_at": "HH:MM", "text": "..."}              # ежедневное
    """
    t = text.strip().lower()

    # через N минут/часов …
    m = re.match(r"^через\s+(?P<n>\d{1,3})\s*(?P<u>минут[уы]?|час(а|ов)?)\s+(?P<text>.+)$", t)
    if m:
        n = int(m.group("n"))
        delta = normalize_minutes_hours(n, m.group("u"))
        return {"after": delta, "text": m.group("text").strip()}

    # сегодня в HH:MM …
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt_local = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"once_at": dt_local, "text": m.group("text").strip()}

    # завтра в HH:MM …
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return {"once_at": base, "text": m.group("text").strip()}

    # каждый день в HH:MM …
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return {"daily_at": f"{hh:02d}:{mm:02d}", "text": m.group("text").strip()}

    # DD <месяц> [в HH:MM] …
    m = re.match(rf"^(?P<d>\d{{1,2}})\s+(?P<mon>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$", t)
    if m and m.group("mon") in MONTHS:
        d = int(m.group("d"))
        mon = MONTHS[m.group("mon")]
        year = now_local().year
        hh = int(m.group("h")) if m.group("h") else 9
        mm = int(m.group("m")) if m.group("m") else 0
        dt_local = datetime(year, mon, d, hh, mm, tzinfo=TIMEZONE)
        # если дата уже прошла в этом году — возьмём следующий год
        if dt_local < now_local():
            dt_local = datetime(year + 1, mon, d, hh, mm, tzinfo=TIMEZONE)
        return {"once_at": dt_local, "text": m.group("text").strip()}

    return None

# =============== ПЛАНИРОВАНИЕ ===============
async def remind_once(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    text = data.get("text", "Напоминание")
    chat_id = job.chat_id
    try:
        await context.bot.send_message(chat_id, f"⏰ {text}")
    except Exception as e:
        log.warning("send_message failed: %s", e)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id

    # приватный доступ
    if uid not in ALLOWED_USERS:
        key = update.message.text.strip()
        if re.fullmatch(r"VIP\d{3}", key) and (key in VALID_KEYS) and (key not in USED_KEYS):
            allow_user(uid, key)
            await update.message.reply_text("Ключ принят ✅.Теперь можно ставить напоминания.")
            await send_help(update, context)
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # обычный текст → парсим
    parsed = parsedate(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов ...\n"
            "— сегодня в HH:MM ...\n"
            "— завтра в HH:MM ...\n"
            "— каждый день в HH:MM ...\n"
            "— DD <месяц> [в HH:MM] ..."
        )
        return

    # планируем
    if "after" in parsed:
        delta: timedelta = parsed["after"]
        job = context.application.job_queue.run_once(
            remind_once, when=delta, chat_id=cid, data={"text": parsed["text"]}
        )
        add_task(uid, {
            "kind": "once",
            "due_iso": (now_local() + delta).astimezone(TIMEZONE).isoformat(),
            "text": parsed["text"],
            "job_name": job.name
        })
        await update.message.reply_text(f"✅ Ок, напомню через {delta} — «{parsed['text']}».")
        return

    if "once_at" in parsed:
        local_dt: datetime = parsed["once_at"]
        job = context.application.job_queue.run_once(
            remind_once, when=to_utc(local_dt), chat_id=cid, data={"text": parsed["text"]}
        )
        add_task(uid, {
            "kind": "once",
            "due_iso": local_dt.isoformat(),
            "text": parsed["text"],
            "job_name": job.name
        })
        await update.message.reply_text(f"✅ Ок, напомню {local_dt.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: Europe/Kaliningrad)")
        return

    if "daily_at" in parsed:
        hh, mm = map(int, parsed["daily_at"].split(":"))
        tm = time(hour=hh, minute=mm, tzinfo=TIMEZONE)
        job = context.application.job_queue.run_daily(
            remind_once, time=tm, chat_id=cid, data={"text": parsed["text"]}
        )
        add_task(uid, {
            "kind": "daily",
            "time": f"{hh:02d}:{mm:02d}",
            "text": parsed["text"],
            "job_name": job.name
        })
        await update.message.reply_text(f"✅ Ок, буду напоминать каждый день в {hh:02d}:{mm:02d} — «{parsed['text']}».")
        return

# =============== ГОЛОСОВЫЕ ===============
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = await transcribe_voice(update, context)
    if not txt:
        return
    # прогоняем как обычный текст
    update.message.text = txt
    await handle_text(update, context)

# =============== СПИСОК ДЕЛ / УДАЛЕНИЕ ===============
async def cmd_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    items = list_tasks(uid)
    if not items:
        await update.message.reply_text("Пока нет дел.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(items, 1):
        if t.get("kind") == "daily":
            lines.append(f"{i}. каждый день {t.get('time')} — {t.get('text')}")
        else:
            dt = datetime.fromisoformat(t.get("due_iso")).astimezone(TIMEZONE)
            lines.append(f"{i}. {dt.strftime('%d.%m.%Y %H:%M')} — {t.get('text')}")
    await update.message.reply_text("\n".join(lines))

async def cmd_affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    if not context.args:
        await update.message.reply_text("Укажите номер: /affairs_delete N")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть целым: /affairs_delete N")
        return
    # перед удалением попытаться снять Job
    items = list_tasks(uid)
    if 1 <= n <= len(items):
        job_name = items[n-1].get("job_name")
        # снять все джобы с таким именем (если есть)
        for j in context.application.job_queue.get_jobs_by_name(job_name or ""):
            j.schedule_removal()
    removed = delete_task_by_index(uid, n)
    if removed:
        await update.message.reply_text("✅ Удалил.")
    else:
        await update.message.reply_text("Не нашёл дело с таким номером.")

# =============== /start и меню ===============
HELP_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Напоминание за какое либо кол-во времени пишите так(Пример напоминания за 1 час): Сегодня в 14:00(Сигнал для бота - в какое время уведомить) напоминаю, встреча в 15:00(Это само напоминание которое бот отправит вам в указанное время - в данном случае в 14:00) Так можно делать с любой датой \n"    
    "(часовой пояс: Europe/Kaliningrad)"
)

async def send_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ALLOWED_USERS:
        await send_help(update, context)
    else:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")

# =============== ТЕХРАБОТЫ: ГЛОБАЛЬНЫЙ ШЛАГБАУМ ===============
async def maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if (not MAINTENANCE) or (uid in ADMIN_IDS):
        return
    cid = update.effective_chat.id if update.effective_chat else None
    if cid:
        if cid not in PENDING_CHATS:
            PENDING_CHATS.add(cid)
            save_pending(PENDING_CHATS)
        try:
            await context.bot.send_message(
                cid,
                "⚠️ Уважаемый пользователь! Сейчас ведутся технические работы. "
                "Как только бот снова заработает, мы сообщим вам здесь."
            )
        except Exception:
            pass
    raise ApplicationHandlerStop

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id not in ADMIN_IDS:
        return
    MAINTENANCE = True
    await update.message.reply_text("⚠️ Включил режим технических работ. Пользователей предупрежу.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE, PENDING_CHATS
    if update.effective_user.id not in ADMIN_IDS:
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Режим техработ выключен. Рассылаю уведомления…")
    to_notify = list(PENDING_CHATS)
    PENDING_CHATS.clear()
    save_pending(PENDING_CHATS)
    for cid in to_notify:
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# =============== МИНИ-HTTP для Render ===============
def run_flask():
    app = Flask(__name__)

    @app.get("/")
    def index():
        return Response("ok", mimetype="text/plain")

    app.run(host="0.0.0.0", port=PORT)

# =============== MAIN ===============
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # HTTP пин для Render
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()

    # Команды для меню
    application.bot.set_my_commands([
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
    ])

    # Глобальный «шлагбаум» техработ
    application.add_handler(TypeHandler(Update, maintenance_guard), group=-100)

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("affairs", cmd_affairs))
    application.add_handler(CommandHandler("affairs_delete", cmd_affairs_delete))
    application.add_handler(CommandHandler("maintenance_on", maintenance_on))
    application.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # Голос
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice))

    # Текст
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Starting bot with polling…")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
