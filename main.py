import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from uuid import uuid4

from pytz import timezone
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ================== НАСТРОЙКИ ==================
TZ = timezone("Europe/Kaliningrad")
ADMIN_ID = 963586834  # <- твой ID
DB_FILE = "db.json"

# Команды в меню
BOT_COMMANDS = [
    BotCommand("start", "Помощь и примеры"),
    BotCommand("affairs", "Список дел"),
    BotCommand("affairs_delete", "Удалить дело по номеру: /affairs_delete 3"),
]

# Месяцы
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("assistant")

# ================== ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ ==================
DEFAULT_DB = {
    "allowed_users": [],
    "keys": {f"VIP{i:03d}": None for i in range(1, 101)},
    "tasks": {},
    "maintenance": False,
    "pending_chats": []
}

def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DB, f, ensure_ascii=False, indent=2)
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

DB = load_db()

ALLOWED_USERS = set(DB["allowed_users"])
ACCESS_KEYS = DB["keys"]
USER_TASKS = DB["tasks"]
MAINTENANCE = DB["maintenance"]
PENDING_CHATS = set(DB["pending_chats"])

# ================== ВСПОМОГАТЕЛЬНОЕ ==================
def now_local() -> datetime:
    return datetime.now(TZ)

def make_job_name(chat_id: int) -> str:
    return f"{chat_id}:{uuid4().hex}"

def add_user_task(chat_id: int, job_name: str, when: datetime, kind: str, text: str):
    tasks = USER_TASKS.setdefault(str(chat_id), [])
    tasks.append({
        "job_name": job_name,
        "when": when.isoformat(),
        "kind": kind,
        "text": text
    })
    save_db(DB)

def remove_user_task_by_jobname(chat_id: int, job_name: str) -> None:
    tasks = USER_TASKS.get(str(chat_id), [])
    USER_TASKS[str(chat_id)] = [t for t in tasks if t["job_name"] != job_name]
    save_db(DB)

def human_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")

def examples_text() -> str:
    return (
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча с Андреем\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• Сегодня в 14:00 (сигнал) напоминаю, встреча в 15:00 (само напоминание в 14:00)\n"
        f"(часовой пояс: {TZ.zone})"
    )

# ================== ПАРСЕР ==================
RE_TIME = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"
RE_INT = r"(?P<n>\d{1,3})"

def parse_text_to_schedule(text: str):
    t = text.strip().lower()

    m = re.match(rf"^через\s+{RE_INT}\s+(минут[уы]?|мин)\s+(?P<msg>.+)$", t)
    if m:
        n = int(m.group("n"))
        when = now_local() + timedelta(minutes=n)
        return ("once", when, m.group("msg"))

    m = re.match(rf"^через\s+{RE_INT}\s+(час(а|ов)?)\s+(?P<msg>.+)$", t)
    if m:
        n = int(m.group("n"))
        when = now_local() + timedelta(hours=n)
        return ("once", when, m.group("msg"))

    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<msg>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return ("once", base, m.group("msg"))

    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<msg>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return ("once", base, m.group("msg"))

    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s+(?P<msg>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        when = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now_local():
            when += timedelta(days=1)
        return ("daily", when, m.group("msg"))

    day_month = rf"(?P<day>\d{{1,2}})\s+(?P<mon>{'|'.join(MONTHS.keys())})"
    m = re.match(rf"^{day_month}(?:\s+в\s+{RE_TIME})?\s+(?P<msg>.+)$", t)
    if m:
        day = int(m.group("day"))
        mon = MONTHS[m.group("mon")]
        hh = int(m.group("h")) if m.groupdict().get("h") else 9
        mm = int(m.group("m")) if m.groupdict().get("m") else 0
        year = now_local().year
        dt = TZ.localize(datetime(year, mon, day, hh, mm))
        if dt <= now_local():
            dt = TZ.localize(datetime(year + 1, mon, day, hh, mm))
        return ("once", dt, m.group("msg"))

    return None

# ================== ОБРАБОТЧИКИ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await context.bot.set_my_commands(BOT_COMMANDS)
    await update.message.reply_text(examples_text())

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    DB["maintenance"] = True
    save_db(DB)
    await update.message.reply_text("⚠️ Режим техработ включен")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    DB["maintenance"] = False
    save_db(DB)
    await update.message.reply_text("✅ Бот снова работает")
    for chat_id in list(PENDING_CHATS):
        await context.bot.send_message(chat_id, "✅ Бот снова работает")
    PENDING_CHATS.clear()

async def affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = USER_TASKS.get(str(update.effective_chat.id), [])
    if not tasks:
        await update.message.reply_text("Список дел пуст.")
        return
    lines = [f"{i+1}. {human_dt(datetime.fromisoformat(t['when']))} — {t['text']}" for i, t in enumerate(tasks)]
    await update.message.reply_text("Ваши дела:\n" + "\n".join(lines))

async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажите номер задачи: /affairs_delete 2")
        return
    idx = int(args[0]) - 1
    tasks = USER_TASKS.get(str(update.effective_chat.id), [])
    if 0 <= idx < len(tasks):
        removed = tasks.pop(idx)
        save_db(DB)
        await update.message.reply_text(f"Удалено: {removed['text']}")
    else:
        await update.message.reply_text("Нет такого номера.")

async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # Ключ
    if uid not in ALLOWED_USERS:
        if msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
            ACCESS_KEYS[msg] = uid
            ALLOWED_USERS.add(uid)
            DB["allowed_users"] = list(ALLOWED_USERS)
            save_db(DB)
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # Техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        DB["pending_chats"] = list(PENDING_CHATS)
        save_db(DB)
        await update.message.reply_text("⚠️ Уважаемый пользователь, бот на техработах.")
        return

    # Парсим задачу
    parsed = parse_text_to_schedule(msg)
    if not parsed:
        await update.message.reply_text("❓ Не понял формат.")
        return

    kind, when, text = parsed
    job_name = make_job_name(chat_id)

    if kind == "once":
        context.job_queue.run_once(remind, when.astimezone(TZ).replace(tzinfo=None), name=job_name, data={"chat_id": chat_id, "text": text})
    elif kind == "daily":
        context.job_queue.run_daily(remind, when.time(), name=job_name, days=(0,1,2,3,4,5,6), data={"chat_id": chat_id, "text": text})

    add_user_task(chat_id, job_name, when, kind, text)
    await update.message.reply_text(f"✅ Ок, напомню {human_dt(when)} — «{text}».")

async def remind(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    chat_id, text = data["chat_id"], data["text"]
    await context.bot.send_message(chat_id, f"⏰ Напоминание: {text}")
    if job.name and USER_TASKS.get(str(chat_id)):
        remove_user_task_by_jobname(chat_id, job.name)

# ================== MAIN ==================
async def main():
    token = os.getenv("BOT_TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
