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
ADMIN_ID = 963586834                 # твой Telegram ID
DB_FILE = "db.json"

BOT_COMMANDS = [
    BotCommand("start", "Помощь и примеры"),
    BotCommand("affairs", "Список дел"),
    BotCommand("affairs_delete", "Удалить дело по номеру: /affairs_delete 3"),
    BotCommand("maintenance_on", "Включить техработы (админ)"),
    BotCommand("maintenance_off", "Выключить техработы (админ)"),
]

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
ALLOWED_USERS = set(DB.get("allowed_users", []))
ACCESS_KEYS = DB.get("keys", {})
USER_TASKS = DB.get("tasks", {})
MAINTENANCE = bool(DB.get("maintenance", False))
PENDING_CHATS = set(DB.get("pending_chats", []))

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
    DB["tasks"] = USER_TASKS
    save_db(DB)

def remove_user_task_by_jobname(chat_id: int, job_name: str) -> None:
    tasks = USER_TASKS.get(str(chat_id), [])
    USER_TASKS[str(chat_id)] = [t for t in tasks if t["job_name"] != job_name]
    DB["tasks"] = USER_TASKS
    save_db(DB)

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

def human_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")

# ================== ПАРСЕР ЕСТЕСТВЕННОГО ЯЗЫКА ==================
RE_TIME = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"
RE_INT = r"(?P<n>\d{1,3})"

def parse_text_to_schedule(text: str):
    t = (text or "").strip().lower()

    # через N минут ...
    m = re.match(rf"^через\s+{RE_INT}\s+(минут[уы]?|мин)\s+(?P<msg>.+)$", t)
    if m:
        n = int(m.group("n"))
        when = now_local() + timedelta(minutes=n)
        return ("once", when, m.group("msg"))

    # через N часов ...
    m = re.match(rf"^через\s+{RE_INT}\s+час(а|ов)?\s+(?P<msg>.+)$", t)
    if m:
        n = int(m.group("n"))
        when = now_local() + timedelta(hours=n)
        return ("once", when, m.group("msg"))

    # сегодня в HH:MM ...
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<msg>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        when = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return ("once", when, m.group("msg"))

    # завтра в HH:MM ...
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<msg>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        when = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return ("once", when, m.group("msg"))

    # каждый день в HH:MM ...
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s+(?P<msg>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        first = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if first <= now_local():
            first += timedelta(days=1)
        return ("daily", first, m.group("msg"))

    # 30 августа [в HH:MM] ...
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
    # уведомим всех, кто пытался писать в простое
    for chat_id in list(PENDING_CHATS):
        try:
            await context.bot.send_message(chat_id, "✅ Бот снова работает")
        except Exception:
            pass
    PENDING_CHATS.clear()
    DB["pending_chats"] = list(PENDING_CHATS)
    save_db(DB)

async def affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = USER_TASKS.get(str(update.effective_chat.id), [])
    if not tasks:
        await update.message.reply_text("Список дел пуст.")
        return
    lines = [
        f"{i+1}. {human_dt(datetime.fromisoformat(t['when']))} — {t['text']}"
        for i, t in enumerate(tasks)
    ]
    await update.message.reply_text("Ваши дела:\n" + "\n".join(lines))

async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажите номер: /affairs_delete 2")
        return
    idx = int(args[0]) - 1
    tasks = USER_TASKS.get(str(update.effective_chat.id), [])
    if 0 <= idx < len(tasks):
        removed = tasks.pop(idx)
        DB["tasks"] = USER_TASKS
        save_db(DB)
        await update.message.reply_text(f"Удалено: {removed['text']}")
    else:
        await update.message.reply_text("Нет такого номера.")

async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик: сначала ключ, потом парсинг фразы."""
    global MAINTENANCE
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1) Авторизация по ключу
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", msg) and msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
            ACCESS_KEYS[msg] = uid
            ALLOWED_USERS.add(uid)
            DB["allowed_users"] = list(ALLOWED_USERS)
            DB["keys"] = ACCESS_KEYS
            save_db(DB)
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # 2) Техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        DB["pending_chats"] = list(PENDING_CHATS)
        save_db(DB)
        await update.message.reply_text("⚠️ Уважаемый пользователь, ведутся технические работы.")
        return

    # 3) Парсинг фразы
    parsed = parse_text_to_schedule(msg)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Примеры:\n"
            "— через 5 минут попить воды\n"
            "— сегодня в 16:00 купить молоко\n"
            "— завтра в 9:15 встреча с Андреем\n"
            "— каждый день в 09:30 зарядка\n"
            "— 30 августа в 09:00 заплатить за кредит"
        )
        return

    kind, when, text = parsed
    job_name = make_job_name(chat_id)

    # Одноразовое: планируем через количество секунд (минимум 1 сек)
    if kind == "once":
        seconds = max(1, int((when - now_local()).total_seconds()))
        context.job_queue.run_once(
            callback=remind,
            when=seconds,
            name=job_name,
            data={"chat_id": chat_id, "text": text, "kind": "once", "job_name": job_name},
        )

    # Ежедневное: повторяем каждые сутки, first — точное время первого запуска
    elif kind == "daily":
        first_delay = max(1, int((when - now_local()).total_seconds()))
        context.job_queue.run_repeating(
            callback=remind,
            interval=86400,
            first=first_delay,
            name=job_name,
            data={"chat_id": chat_id, "text": text, "kind": "daily", "job_name": job_name},
        )

    add_user_task(chat_id, job_name, when, kind, text)
    await update.message.reply_text(f"✅ Ок, напомню {human_dt(when)} — «{text}».")

async def remind(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "Напоминание")
    kind = data.get("kind", "once")
    job_name = data.get("job_name")

    if chat_id:
        try:
            await context.bot.send_message(chat_id, f"⏰ Напоминание: {text}")
        except Exception as e:
            log.exception("Send message failed: %s", e)

    # одноразовые удаляем из списка дел после срабатывания
    if kind == "once" and chat_id and job_name:
        remove_user_task_by_jobname(chat_id, job_name)

# Глобальный обработчик ошибок — чтобы бот не «молчал»
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Упс, случилась ошибка. Уже исправляю.")
    except Exception:
        pass

# ================== MAIN ==================
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    app = Application.builder().token(token).build()

    # Команды (видны в меню)
    async def _post_init(_):
        await app.bot.set_my_commands(BOT_COMMANDS)
    app.post_init = _post_init

    # Хэндлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    app.add_error_handler(on_error)
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except RuntimeError:  # если цикл уже запущен
        loop = asyncio.get_event_loop()
        loop.create_task(main())
        loop.run_forever()
