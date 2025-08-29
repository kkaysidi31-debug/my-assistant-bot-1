import os
import re
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional, List, Tuple, Dict

from aiohttp import web
import telegram
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, filters, Update as TgUpdate
)

# -------------------- НАСТРОЙКИ --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
TZ = timezone(timedelta(hours=2))

ptb_version = telegram.__version__

# Приватные ключи (VIP001..VIP100)
ALL_KEYS = [f"VIP{str(i).zfill(3)}" for i in range(1, 101)]

# Флаг обслуживания (admin-only)
MAINTENANCE = False

# Память для списка /affairs -> индексы выдачи
LAST_LIST_INDEX: Dict[int, List[int]] = {}  # chat_id -> [task_id, ...]

WELCOME_TEXT = (
    "Привет, я твой личный ассистент. Я помогу тебе оптимизировать все твои рутинные задачи, "
    "чтобы ты сосредоточился на самом главном и ничего не забыл.\n\n"
    "Примеры:\n"
    "• через 2 минуты поесть / через 30 секунд позвонить\n"
    "• сегодня в 18:30 попить воды\n"
    "• завтра в 09:00 сходить в зал\n"
    "• каждый день в 07:45 чистить зубы\n"
    "• 30 августа в 10:00 оплатить кредит\n\n"
    "❗ Напоминание «за N минут»: просто поставь время на N минут раньше."
)

# -------------------- СУБД --------------------
DB_PATH = "bot.db"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL,                  -- once|daily|monthly
            run_at_utc TEXT,                     -- ISO для once
            hour INTEGER,                        -- для daily/monthly
            minute INTEGER,                      -- для daily/monthly
            day_of_month INTEGER                 -- для monthly
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS auth(
            chat_id INTEGER PRIMARY KEY,
            ok INTEGER NOT NULL DEFAULT 0
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS keys(
            key TEXT PRIMARY KEY,
            used_by INTEGER
        );
        """)
        # Инициализируем ключи, если пусто
        cur = c.execute("SELECT COUNT(*) as n FROM keys;").fetchone()
        if cur["n"] == 0:
            c.executemany("INSERT OR IGNORE INTO keys(key, used_by) VALUES(?, NULL);",
                          [(k,) for k in ALL_KEYS])
    logging.info("DB ready")

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

# -------------------- ВСПОМОГАТЕЛЬНЫЕ --------------------
def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc)

def _future_utc(dt_utc: datetime) -> datetime:
    now = datetime.now(timezone.utc)
    if dt_utc <= now:
        dt_utc = now + timedelta(seconds=2)
    return dt_utc

def fmt(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def is_auth(chat_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT ok FROM auth WHERE chat_id=?;", (chat_id,)).fetchone()
        return bool(row and row["ok"])

def set_auth(chat_id: int, ok: bool):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO auth(chat_id, ok) VALUES(?, ?);", (chat_id, 1 if ok else 0))
def use_key(chat_id: int, key: str) -> bool:
    key = key.strip().upper()
    with db() as c:
        row = c.execute("SELECT used_by FROM keys WHERE key=?;", (key,)).fetchone()
        if not row:
            return False
        if row["used_by"] is not None:
            return False
        c.execute("UPDATE keys SET used_by=? WHERE key=?;", (chat_id, key))
    set_auth(chat_id, True)
    return True

def list_keys() -> Tuple[List[str], List[Tuple[str,int]]]:
    with db() as c:
        free = [r["key"] for r in c.execute("SELECT key FROM keys WHERE used_by IS NULL ORDER BY key;")]
        used = [(r["key"], r["used_by"]) for r in c.execute("SELECT key, used_by FROM keys WHERE used_by IS NOT NULL ORDER BY key;")]
    return free, used

def reset_keys():
    with db() as c:
        c.execute("UPDATE keys SET used_by=NULL;")
        c.execute("DELETE FROM auth;")

def add_task(t: Task) -> int:
    with db() as c:
        cur = c.execute("""
        INSERT INTO tasks(chat_id, title, type, run_at_utc, hour, minute, day_of_month)
        VALUES(?, ?, ?, ?, ?, ?, ?);
        """, (
            t.chat_id, t.title, t.type,
            t.run_at_utc.isoformat() if t.run_at_utc else None,
            t.hour, t.minute, t.day_of_month
        ))
        return int(cur.lastrowid)

def get_task(task_id: int) -> Optional[Task]:
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?;", (task_id,)).fetchone()
    if not r: return None
    return Task(
        id=r["id"], chat_id=r["chat_id"], title=r["title"], type=r["type"],
        run_at_utc=datetime.fromisoformat(r["run_at_utc"]).astimezone(timezone.utc) if r["run_at_utc"] else None,
        hour=r["hour"], minute=r["minute"], day_of_month=r["day_of_month"]
    )

def list_active_tasks(chat_id: int) -> List[Task]:
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE chat_id=? ORDER BY id ASC;", (chat_id,)).fetchall()
    res: List[Task] = []
    for r in rows:
        res.append(Task(
            id=r["id"], chat_id=r["chat_id"], title=r["title"], type=r["type"],
            run_at_utc=datetime.fromisoformat(r["run_at_utc"]).astimezone(timezone.utc) if r["run_at_utc"] else None,
            hour=r["hour"], minute=r["minute"], day_of_month=r["day_of_month"]
        ))
    return res

def delete_task(task_id: int) -> bool:
    with db() as c:
        cur = c.execute("DELETE FROM tasks WHERE id=?;", (task_id,))
        return cur.rowcount > 0

# -------------------- ПАРСИНГ ТЕКСТА --------------------
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12
}

RELATIVE_RE = re.compile(r"(?i)^через\s+(\d+)\s+(секунд\w*|минут\w*|час\w*)\s+(.+)$")
TODAY_RE    = re.compile(r"(?i)^(?:сегодня)\s+в?\s*(\d{1,2}):(\d{2})\s+(.+)$")
TOMORROW_RE = re.compile(r"(?i)^(?:завтра)\s+в?\s*(\d{1,2}):(\d{2})\s+(.+)$")
DAILY_RE    = re.compile(r"(?i)^(?:каждый\s+день|ежедневно)\s+в?\s*(\d{1,2}):(\d{2})\s+(.+)$")
MONTHLY_RE  = re.compile(r"(?i)^(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$")

@dataclass
class ParsedTask:
    type: str                 # once|daily|monthly
    title: str
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def parse_user_text_to_task(text: str, now_tz: datetime) -> Optional[ParsedTask]:
    t = text.strip()

    m = RELATIVE_RE.match(t)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        title = m.group(3).strip()
        if re.search(r"секунд", unit):
            delta = timedelta(seconds=amount)
        elif re.search(r"минут", unit):
            delta = timedelta(minutes=amount)
        elif re.search(r"час", unit):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        run_local = now_tz + delta
        return ParsedTask("once", title, _to_utc(run_local), None, None, None)

    m = TODAY_RE.match(t)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz:
            run_local = run_local + timedelta(days=1)
        return ParsedTask("once", title, _to_utc(run_local), None, None, None)

    m = TOMORROW_RE.match(t)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = (now_tz + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return ParsedTask("once", title, _to_utc(run_local), None, None, None)

    m = DAILY_RE.match(t)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        return ParsedTask("daily", title, None, h, mi, None)

    m = MONTHLY_RE.match(t)
    if m:
        day = int(m.group(1))
        mon = MONTHS[m.group(2).lower()]
        h, mi = int(m.group(3)), int(m.group(4))
        title = m.group(5).strip()
        # Для monthly храним только день/час/минуты
        return ParsedTask("monthly", title, None, h, mi, day)

    return None

# -------------------- JOB QUEUE --------------------
async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        t_id = ctx.job.data["id"]
        t = get_task(t_id)
        if not t:
            return
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")
        if t.type == "once":
            delete_task(t.id)
    except Exception as e:
        logging.exception("job_once failed: %s", e)

async def schedule(app: Application, t: Task):
    jq = app.job_queue
    try:
        if t.type == "once":
            run_at = _future_utc(t.run_at_utc)
            jq.run_once(job_once, when=run_at, name=f"task_{t.id}", data={"id": t.id})
        elif t.type == "daily":
            jq.run_daily(job_once,
                         time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                         name=f"task_{t.id}", data={"id": t.id})
        elif t.type == "monthly":
            async def monthly_wrapper(ctx: ContextTypes.DEFAULT_TYPE):
                tloc = get_task(ctx.job.data["id"])
                now_local = datetime.now(TZ)
                if tloc and now_local.day == tloc.day_of_month:
                    await job_once(ctx)
            jq.run_daily(monthly_wrapper,
                         time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                         name=f"task_{t.id}", data={"id": t.id})
        else:
            logging.warning("Unknown task type %s", t.type)
            return
    except Exception as e:
        logging.exception("schedule failed: %s", e)
        # Сообщаем и пробуем ретрай
        try:
            await app.bot.send_message(
                chat_id=t.chat_id,
                text="⚠️ Задачу сохранил, но возникла ошибка при планировании. Попробую ещё раз через минуту."
            )
        except Exception:
            pass

        def _retry(_ctx):
            try:
                if t.type == "once":
                    t.run_at_utc = datetime.now(timezone.utc) + timedelta(minutes=1)
                app.create_task(schedule(app, t))
            except Exception:
                logging.exception("Retry schedule failed")

        app.job_queue.run_once(_retry, when=timedelta(minutes=1))

async def reschedule_all(app: Application):
    for t in list_active_tasks_for_all():
        await schedule(app, t)

def list_active_tasks_for_all() -> List[Task]:
    with db() as c:
        rows = c.execute("SELECT * FROM tasks;").fetchall()
    res = []
    for r in rows:
        res.append(Task(
            id=r["id"], chat_id=r["chat_id"], title=r["title"], type=r["type"],
            run_at_utc=datetime.fromisoformat(r["run_at_utc"]).astimezone(timezone.utc) if r["run_at_utc"] else None,
            hour=r["hour"], minute=r["minute"], day_of_month=r["day_of_month"]
        ))
    return res

# -------------------- ХЭЛС-ЭНДПОИНТ --------------------
async def start_health():
    async def handle(_request):
        return web.Response(text="alive", status=200)
    app = web.Application()
    app.router.add_get("/", handle)
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("Health server on 0.0.0.0:%s", port)

# -------------------- КОМАНДЫ --------------------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Этот бот приватный. Введите ключ доступа в формате ABC123.")
    if ADMIN_ID and update.effective_user and update.effective_user.id == ADMIN_ID:
        logging.info("Admin started the bot")

async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    global MAINTENANCE; MAINTENANCE = True
    await update.message.reply_text("🔧 Режим обслуживания включён.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    global MAINTENANCE; MAINTENANCE = False
    await update.message.reply_text("✅ Режим обслуживания выключен.")

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    free, used = list_keys()
    await update.message.reply_text(f"Свободных ключей: {len(free)}")

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    free, _ = list_keys()
    await update.message.reply_text("Свободные:\n" + (", ".join(free) if free else "—"))

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    _, used = list_keys()
    if not used:
        await update.message.reply_text("Использованных нет.")
        return
    txt = "\n".join(f"{k} → {uid}" for k, uid in used)
    await update.message.reply_text(txt)

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    reset_keys()
    await update.message.reply_text("Ключи и доступы сброшены.")

def guard_maintenance(update: Update) -> bool:
    if not MAINTENANCE:
        return False
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        return False
    # пользователям во время обслуживания
    if update.message:
        update.message.reply_text("🔧 Бот на обслуживании. Попробуй позже.")
    return True

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update): return
    chat_id = update.effective_chat.id
    tasks = list_active_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("Пока дел нет.")
        return

    # Сортируем по ближайшему запуску
    def next_run(t: Task) -> datetime:
        now = datetime.now(TZ)
        if t.type == "once":
            return t.run_at_utc.astimezone(TZ)
        elif t.type == "daily":
            cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if cand <= now: cand += timedelta(days=1)
            return cand
        else:
            # monthly
            cand = now.replace(day=min(t.day_of_month, 28), hour=t.hour, minute=t.minute, second=0, microsecond=0)
            while cand.day != t.day_of_month:
                cand += timedelta(days=1)
            if cand <= now:
                # следующий месяц
                nm = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
                cand = nm.replace(day=min(t.day_of_month, 28), hour=t.hour, minute=t.minute, second=0, microsecond=0)
                while cand.day != t.day_of_month:
                    cand += timedelta(days=1)
            return cand

    tasks_sorted = sorted(tasks, key=next_run)[:20]
    LAST_LIST_INDEX[chat_id] = [t.id for t in tasks_sorted]

    lines = []
    for i, t in enumerate(tasks_sorted, 1):
        if t.type == "once":
            when = fmt(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {str(t.hour).zfill(2)}:{str(t.minute).zfill(2)}"
        else:
            when = f"каждое {t.day_of_month}-е в {str(t.hour).zfill(2)}:{str(t.minute).zfill(2)}"
        lines.append(f"{i}. {t.title} — {when}")
    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update): return
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Используй: /affairs_delete N")
        return
    try:
        idx = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Номер должен быть числом.")
        return
    ids = LAST_LIST_INDEX.get(chat_id, [])
    if not ids or idx < 1 or idx > len(ids):
        await update.message.reply_text("Сначала открой /affairs.")
        return
    t_id = ids[idx-1]
    t = get_task(t_id)
    if t and delete_task(t_id):
        await update.message.reply_text(f"❌ Удалено: «{t.title}»")
    else:
        await update.message.reply_text("Это дело уже удалено.")

# -------------------- ТЕКСТ --------------------
AFFAIRS_DELETE_TEXT_RE = re.compile(r"(?i)^\s*affairs\s*delete\s+(\d+)\s*$")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update): return
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # --- Авторизация по ключу ---
    if not is_auth(chat_id) and (not update.effective_user or update.effective_user.id != ADMIN_ID):
        if use_key(chat_id, text):
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text(WELCOME_TEXT)
        else:
            await update.message.reply_text("❌ Неверный ключ доступа.")
        return

    # --- Удаление через текст "affairs delete 3" ---
    m = AFFAIRS_DELETE_TEXT_RE.fullmatch(text)
    if m:
        ids = LAST_LIST_INDEX.get(chat_id, [])
        idx = int(m.group(1))
        if not ids or idx < 1 or idx > len(ids):
            await update.message.reply_text("Сначала открой /affairs.")
            return
        t_id = ids[idx-1]
        t = get_task(t_id)
        if t and delete_task(t_id):
            await update.message.reply_text(f"❌ Удалено: «{t.title}»")
        else:
            await update.message.reply_text("Это дело уже удалено.")
        return

    # --- Добавление задач ---
    now_local = datetime.now(TZ)
    parsed = parse_user_text_to_task(text, now_local)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    task = Task(
        id=0, chat_id=chat_id, title=parsed.title, type=parsed.type,
        run_at_utc=parsed.run_at_utc if parsed.type == "once" else None,
        hour=parsed.hour, minute=parsed.minute, day_of_month=parsed.day_of_month
    )
    new_id = add_task(task)
    task.id = new_id
    await schedule(ctx.application, task)

    if task.type == "once":
        await update.message.reply_text(f"✅ Отлично, напомню: «{task.title}» — {fmt(task.run_at_utc)}")
    elif task.type == "daily":
        await update.message.reply_text(f"✅ Отлично, напомню: каждый день в {str(task.hour).zfill(2)}:{str(task.minute).zfill(2)} — «{task.title}»")
    else:
        await update.message.reply_text(f"✅ Отлично, напомню: {task.day_of_month}-го числа в {str(task.hour).zfill(2)}:{str(task.minute).zfill(2)} — «{task.title}»")

# -------------------- MAIN --------------------
async def on_startup(app: Application):
    # health сервер (для UptimeRobot)
    await start_health()
    # снимаем вебхук (чтобы polling работал на Render)
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    # восстанавливаем расписание
    try:
        for t in list_active_tasks_for_all():
            await schedule(app, t)
    except Exception:
        logging.exception("reschedule_all failed")
    logging.info("Bot started.PTB=%s TZ=%s", ptb_version, TZ)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render -> Environment.")

    init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))
    app.add_handler(CommandHandler("keys_free", keys_free_cmd))
    app.add_handler(CommandHandler("keys_used", keys_used_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = on_startup
    app.run_polling(allowed_updates=TgUpdate.ALL_TYPES)

if __name__ == "__main__":
    main()
