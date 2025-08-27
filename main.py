import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8492146866:AAE6yWRhg1wa9qn7_PV3NRJS6lh1dFtjxqA"
ADMIN_ID = 963586834
TZ = timezone(timedelta(hours=2), name="Europe/Kaliningrad")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

DB_FILE = "tasks.db"
ALLOWED_KEYS = [f"VIP{i:03d}" for i in range(1, 101)]
ACCESS_GRANTED: Dict[int, bool] = {}
MAINTENANCE = False
LAST_LIST_INDEX: Dict[int, List[int]] = {}

# ================= ДАННЫЕ =================
@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

# ================= БАЗА =================
def db():
    return sqlite3.connect(DB_FILE)

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                title TEXT,
                type TEXT,
                run_at_utc TEXT,
                hour INTEGER,
                minute INTEGER,
                day_of_month INTEGER
            )
            """
        )

def add_task(chat_id, title, type_, run_at_utc, hour, minute, day_of_month):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month) VALUES (?,?,?,?,?,?,?)",
            (
                chat_id,
                title,
                type_,
                run_at_utc.isoformat() if run_at_utc else None,
                hour,
                minute,
                day_of_month,
            ),
        )
        return cur.lastrowid

def get_task(task_id: int) -> Optional[Task]:
    with db() as conn:
        cur = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        return row and Task(
            id=row[0],
            chat_id=row[1],
            title=row[2],
            type=row[3],
            run_at_utc=datetime.fromisoformat(row[4]) if row[4] else None,
            hour=row[5],
            minute=row[6],
            day_of_month=row[7],
        )

def list_active_tasks(chat_id: int) -> List[Task]:
    with db() as conn:
        cur = conn.execute("SELECT * FROM tasks WHERE chat_id=?", (chat_id,))
        rows = cur.fetchall()
        return [
            Task(
                id=r[0],
                chat_id=r[1],
                title=r[2],
                type=r[3],
                run_at_utc=datetime.fromisoformat(r[4]) if r[4] else None,
                hour=r[5],
                minute=r[6],
                day_of_month=r[7],
            )
            for r in rows
        ]

def delete_task(task_id: int):
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))

# ================= ПАРСИНГ =================
RELATIVE_RE = re.compile(r"через\s+(\d+)\s+(.+)", re.I)
TODAY_RE = re.compile(r"сегодня\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", re.I)
TOMORROW_RE = re.compile(r"завтра\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", re.I)
DAILY_RE = re.compile(r"каждый\s+день\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", re.I)
MONTHLY_RE = re.compile(r"(\d{1,2})\s+августа\s+(.+)", re.I)

@dataclass
class ParsedTask:
    type: str
    title: str
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def parse_user_text_to_task(text: str, now_tz: datetime) -> Optional[ParsedTask]:
    text = text.strip()
    m = RELATIVE_RE.match(text)
    if m:
        amount = int(m.group(1))
        title = m.group(2).strip()
        low = text.lower()
        if "сек" in low:
            delta = timedelta(seconds=amount)
        elif "мин" in low:
            delta = timedelta(minutes=amount)
        elif "час" in low:
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        run_local = now_tz + delta
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)
    m = TODAY_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz:
            run_local += timedelta(days=1)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)
    m = TOMORROW_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = (now_tz + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)
    m = DAILY_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        return ParsedTask("daily", title, None, h, mi, None)
    m = MONTHLY_RE.match(text)
    if m:
        d, title = int(m.group(1)), m.group(2).strip()
        return ParsedTask("monthly", title, None, None, None, d)
    return None

def fmt_dt_kaliningrad(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

# ================= JOB =================
async def schedule_task(app: Application, t: Task):
    if app is None or t is None:
        return
    jq = app.job_queue
    name = f"task_{t.id}"
    for old in jq.get_jobs_by_name(name):
        old.schedule_removal()
    if t.type == "once":
        jq.run_once(remind_task, when=t.run_at_utc, name=name, data=t)
    elif t.type == "daily":
        jq.run_daily(remind_task, time=datetime.now().replace(hour=t.hour, minute=t.minute, second=0).timetz(), name=name, data=t)
    elif t.type == "monthly":
        jq.run_daily(remind_task, time=datetime.now().replace(hour=t.hour, minute=t.minute, second=0).timetz(), name=name, data=t)

async def remind_task(ctx: ContextTypes.DEFAULT_TYPE):
    t: Task = ctx.job.data
    if t.type == "monthly":
        today = datetime.now(TZ).day
        if today != t.day_of_month:
            return
    await ctx.bot.send_message(t.chat_id, f"⏰ Напоминание: {t.title}")

# ================= КОМАНДЫ =================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет, я твой личный ассистент.\n"
        "Я помогу тебе оптимизировать все твои рутинные задачи.\n\n"
        "Этот бот приватный. Введите приватный ключ в формате ABC123\n\n"
        "Примеры:\n"
        "• через 5 минут поесть\n"
        "• сегодня в 14:00 попить воды\n"
        "• завтра в 19:00 сходить в зал\n"
        "• каждый день в 08:00 чистить зубы\n"
        "• 30 августа заплатить за кредит"
    )

async def keys_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    if key in ALLOWED_KEYS:
        ACCESS_GRANTED[update.effective_chat.id] = True
        await update.message.reply_text("✅ Доступ подтверждён! Теперь можешь добавлять дела и использовать команду «/affairs».")
    else:
        await update.message.reply_text("❌ Неверный ключ.")

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tasks = list_active_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("Пока дел нет.")
        return
    lines = []
    for i, t in enumerate(tasks, 1):
        if t.type == "once":
            when = fmt_dt_kaliningrad(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"каждое {t.day_of_month} число"
        lines.append(f"{i}. {t.title} — {when}")
    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Используй: /affairs_delete <номер>")
        return
    try:
        num = int(ctx.args[0]) - 1
    except:
        await update.message.reply_text("Неверный номер.")
        return
    tasks = list_active_tasks(chat_id)
    if num < 0 or num >= len(tasks):
        await update.message.reply_text("Нет задачи с таким номером.")
        return
    t = tasks[num]
    delete_task(t.id)
    await update.message.reply_text(f"🗑 Задача «{t.title}» удалена.")

async def maintenance_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    MAINTENANCE = True
    await update.message.reply_text("⚠️ Технические работы. Бот временно недоступен.")

async def maintenance_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Бот снова работает.")

# ================= ТЕКСТ =================
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE:
        await update.message.reply_text("⚠️ Бот на техработах.")
        return
    if not ACCESS_GRANTED.get(update.effective_chat.id):
        await update.message.reply_text("❌ У тебя нет доступа. Введи ключ.")
        return
    now_tz = datetime.now(TZ)
    parsed = parse_user_text_to_task(update.message.text, now_tz)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял задачу. Попробуй: «через 5 минут поесть»")
        return
    task_id = add_task(
        update.effective_chat.id,
        parsed.title,
        parsed.type,
        parsed.run_at_utc,
        parsed.hour,
        parsed.minute,
        parsed.day_of_month,
    )
    t = get_task(task_id)
    await schedule_task(ctx.application, t)
    if t.type == "once":
        when = fmt_dt_kaliningrad(t.run_at_utc)
    elif t.type == "daily":
        when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
    else:
        when = f"каждое {t.day_of_month} число"
    await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — {when}")

# ================= MAIN =================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, keys_cmd), 0)  # проверка ключа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text), 1)

    async def on_startup(app_: Application):
        await app_.bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot started. Timezone=%s", TZ)

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
