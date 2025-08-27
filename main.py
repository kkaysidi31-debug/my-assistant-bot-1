# -*- coding: utf-8 -*-
"""
Telegram бот-напоминалка (приватный по ключам) + healthcheck HTTP для Render
"""

import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# -------------------- НАСТРОЙКИ --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8492146866:AAE6yWRhg1wa9qn7_PV3NRJS6lh1dFtjxqA")
ADMIN_ID = int(os.getenv("ADMIN_ID", "963586834"))
TZ = ZoneInfo("Europe/Kaliningrad")
DB_PATH = "reminder_bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("reminder-bot")

# -------------------- HEALTHCHECK --------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# -------------------- БД --------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            is_authorized INTEGER NOT NULL DEFAULT 0,
            key_used TEXT,
            authorized_at_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS access_keys (
            key TEXT PRIMARY KEY,
            used_by_chat_id INTEGER,
            used_at_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('once','daily','monthly')),
            run_at_utc TEXT,
            hour INTEGER,
            minute INTEGER,
            day_of_month INTEGER,
            tz TEXT NOT NULL DEFAULT 'Europe/Kaliningrad',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at_utc TEXT NOT NULL,
            last_triggered_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS maintenance_waitlist (
            chat_id INTEGER PRIMARY KEY
        );
        """)
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('maintenance','0')")
        # Сгенерим ключи VIP001..VIP100
        existing = {r[0] for r in conn.execute("SELECT key FROM access_keys")}
        to_add = [(f"VIP{i:03d}",) for i in range(1, 101) if f"VIP{i:03d}" not in existing]
        if to_add:
            conn.executemany("INSERT INTO access_keys(key) VALUES(?)", to_add)
        conn.commit()

# -------------------- ДОСТУП --------------------
def is_admin(update: Update) -> bool:
    return (update.effective_user and update.effective_user.id == ADMIN_ID)

def get_user_auth(chat_id: int) -> bool:
    with db() as conn:
        r = conn.execute("SELECT is_authorized FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r[0]) if r else False

def set_user_auth(chat_id: int, key_used: str):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO users(chat_id,is_authorized,key_used,authorized_at_utc) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET is_authorized=excluded.is_authorized, "
            "key_used=excluded.key_used, authorized_at_utc=excluded.authorized_at_utc",(chat_id, 1, key_used, now)
        )
        conn.commit()

def try_consume_key(raw_text: str, chat_id: int) -> bool:
    k = re.sub(r"\s+", "", raw_text).upper()
    if not re.fullmatch(r"VIP\d{3}", k):
        return False
    with db() as conn:
        row = conn.execute("SELECT key, used_by_chat_id FROM access_keys WHERE key=?", (k,)).fetchone()
        if not row:
            return False
        if row[1] is not None and row[1] != chat_id:
            return False
        conn.execute("UPDATE access_keys SET used_by_chat_id=?, used_at_utc=? WHERE key=?",
                     (chat_id, datetime.now(timezone.utc).isoformat(), k))
        conn.commit()
    set_user_auth(chat_id, k)
    return True

# -------------------- ТЕХРАБОТЫ --------------------
def maintenance_on() -> bool:
    with db() as conn:
        v = conn.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
        return (v and v[0] == "1")

def set_maintenance(flag: bool):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('maintenance',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if flag else "0",))
        conn.commit()

def guard_maintenance(update: Update) -> bool:
    if maintenance_on() and not is_admin(update):
        try:
            update.effective_message.reply_text(
                "⚠️⚠️⚠️ Уважаемые пользователи, проводятся технические работы.\n"
                "Пожалуйста, попробуйте позже."
            )
        except Exception:
            pass
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO maintenance_waitlist(chat_id) VALUES(?)",
                         (update.effective_chat.id,))
            conn.commit()
        return True
    return False

# -------------------- ПРИВЕТСТВИЕ --------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    chat_id = update.effective_chat.id
    if is_admin(update) or get_user_auth(chat_id):
        await update.message.reply_text(
            "Привет, я твой личный ассистент. Я помогу тебе оптимизировать все твои рутинные задачи, "
            "чтобы ты сосредоточился на самом главном и ничего не забыл.\n\n"
            "Примеры:\n"
            "• через 2 минуты / через 5 минут — поесть\n"
            "• сегодня в 18:30 — попить воды\n"
            "• завтра в 09:00 — сходить в зал\n"
            "• каждый день в 07:45 — чистить зубы\n"
            "• 30 августа в 10:00 — оплатить кредит\n\n"
            "❗ Если встреча в 15:00, а напоминание нужно за час — напиши задачу на 14:00."
        )
    else:
        await update.message.reply_text("Этот бот приватный. Введите приватный ключ в формате ABC123")

# -------------------- ПАРСЕР --------------------
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
RELATIVE_RE = re.compile(r"^\s*через\s+(\d+)\s*(секунд[уы]?|сек|с|минут[уы]?|мин|м|час(?:а|ов)?|ч)\s+(.+)$", re.I)
TODAY_RE = re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
TOMORROW_RE= re.compile(r"^\s*завтра\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DAILY_RE= re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DATE_RE_NUM = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)
DATE_RE_TEXT = re.compile(r"^\s*(\d{1,2})\s+([а-яА-Я]+)(?:\s+(\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)

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
        unit = m.group(2).lower()
        title = m.group(3).strip()
        if unit.startswith("сек") or unit == "с":
            delta = timedelta(seconds=amount)
        elif unit.startswith("мин") or unit == "м":
            delta = timedelta(minutes=amount)
        elif unit.startswith("час") or unit == "ч":
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        run_local = now_tz + delta
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TODAY_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz: run_local += timedelta(days=1)
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

    m = DATE_RE_NUM.match(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3) or now_tz.year)
        h, mi, title = int(m.group(4) or 10), int(m.group(5) or 0), m.group(6).strip()
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3): run_local = datetime(y+1, mo, d, h, mi, tzinfo=TZ)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DATE_RE_TEXT.match(text)
    if m:
        d, mon = int(m.group(1)), m.group(2).lower()
        if mon not in MONTHS: return None
        y = int(m.group(3) or now_tz.year)
        h, mi = int(m.group(4) or 10), int(m.group(5) or 0)
        title = m.group(6).strip()
        mo = MONTHS[mon]
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3): run_local = datetime(y+1, mo, d, h, mi, tzinfo=TZ)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    return None

# -------------------- МОДЕЛЬ И ХРАНИЛИЩЕ ЗАДАЧ --------------------
@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str               # 'once' | 'daily' | 'monthly'
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]
    tz: str
    is_active: bool
    created_at_utc: datetime
    last_triggered_utc: Optional[datetime]

def row_to_task(row: Tuple) -> Task:
    def dt(s): return datetime.fromisoformat(s) if s else None
    return Task(
        id=row[0], chat_id=row[1], title=row[2], type=row[3],
        run_at_utc=dt(row[4]), hour=row[5], minute=row[6], day_of_month=row[7],
        tz=row[8], is_active=bool(row[9]), created_at_utc=dt(row[10]), last_triggered_utc=dt(row[11])
    )

def add_task(chat_id, title, ttype, run_at_utc, hour, minute, day_of_month):
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO tasks (chat_id,title,type,run_at_utc,hour,minute,day_of_month,tz,is_active,created_at_utc)
            VALUES (?,?,?,?,?,?,?,?,1,?)
        """, (
            chat_id, title, ttype,
            run_at_utc.isoformat() if run_at_utc else None,
            hour, minute, day_of_month, "Europe/Kaliningrad",
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
        return cur.lastrowid

def get_task(task_id: int) -> Optional[Task]:
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row_to_task(row) if row else None

def cancel_task(task_id: int):
    with db() as conn:
        conn.execute("UPDATE tasks SET is_active=0 WHERE id=?", (task_id,))
        conn.commit()

def list_active_tasks(chat_id: int) -> List[Task]:with db() as conn:
        return [row_to_task(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE chat_id=? AND is_active=1", (chat_id,)
        )]

def mark_triggered(task_id: int):
    with db() as conn:
        conn.execute("UPDATE tasks SET last_triggered_utc=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), task_id))
        conn.commit()

# -------------------- ПЛАНИРОВЩИК --------------------
LAST_LIST_INDEX: Dict[int, List[int]] = {}

def fmt_dt_kaliningrad(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def compute_next_for_daily(hour: int, minute: int, now_tz: datetime) -> datetime:
    cand = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now_tz:
        cand += timedelta(days=1)
    return cand

def compute_next_for_monthly(day: int, hour: int, minute: int, now_tz: datetime) -> datetime:
    y, m = now_tz.year, now_tz.month
    for _ in range(24):
        try:
            cand = datetime(y, m, day, hour, minute, tzinfo=TZ)
            if cand > now_tz:
                return cand
            m = 1 if m == 12 else m + 1
            if m == 1:
                y += 1
        except ValueError:
            m = 1 if m == 12 else m + 1
            if m == 1:
                y += 1
    return now_tz + timedelta(days=30)

async def job_fire(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if not t or not t.is_active:
        return
    try:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")
    finally:
        mark_triggered(tid)
        if t.type == "once":
            cancel_task(tid)

async def job_fire_monthly(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if not t or not t.is_active:
        return
    try:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")
    finally:
        mark_triggered(tid)
        # запланируем следующее «каждое N число в H:M»
        now_tz = datetime.now(TZ)
        nxt = compute_next_for_monthly(t.day_of_month, t.hour, t.minute, now_tz)
        ctx.job_queue.run_once(job_fire_monthly, nxt.astimezone(timezone.utc),
                               name=f"task_{t.id}", data={"task_id": t.id})

async def schedule_task(app: Application, t: Task):
    jq = app.job_queue
    # убираем старые джобы с таким именем
    for j in jq.get_jobs_by_name(f"task_{t.id}"):
        j.schedule_removal()
    if not t.is_active:
        return
    if t.type == "once":
        if t.run_at_utc and t.run_at_utc > datetime.now(timezone.utc):
            jq.run_once(job_fire, t.run_at_utc, name=f"task_{t.id}", data={"task_id": t.id})
    elif t.type == "daily":
        jq.run_daily(job_fire, time=time(t.hour, t.minute, tzinfo=TZ),
                     name=f"task_{t.id}", data={"task_id": t.id})
    elif t.type == "monthly":
        nxt = compute_next_for_monthly(t.day_of_month, t.hour, t.minute, datetime.now(TZ))
        jq.run_once(job_fire_monthly, nxt.astimezone(timezone.utc),
                    name=f"task_{t.id}", data={"task_id": t.id})

async def reschedule_all(app: Application):
    with db() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE is_active=1").fetchall()
    for r in rows:
        await schedule_task(app, row_to_task(r))

# -------------------- ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ --------------------
async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    chat_id = update.effective_chat.id
    tasks = list_active_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("Пока дел нет.")
        return

    now_tz = datetime.now(TZ)
    def next_run(t: Task) -> datetime:
        if t.type == "once":
            return t.run_at_utc.astimezone(TZ)
        elif t.type == "daily":
            return compute_next_for_daily(t.hour, t.minute, now_tz)
        else:
            return compute_next_for_monthly(t.day_of_month, t.hour, t.minute, now_tz)
            tasks_sorted = sorted(tasks, key=next_run)[:20]
    LAST_LIST_INDEX[chat_id] = [t.id for t in tasks_sorted]

    lines = []
    for i, t in enumerate(tasks_sorted, 1):
        if t.type == "once":
            when = fmt_dt_kaliningrad(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {when}")
    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    text = (update.message.text or "").strip()

    # Авторизация ключом
    if not (is_admin(update) or get_user_auth(update.effective_chat.id)):
        if try_consume_key(text, update.effective_chat.id):
            await update.message.reply_text("✅ Доступ подтверждён! Теперь можешь добавлять дела и использовать «affairs».")
        else:
            await update.message.reply_text("Этот бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # Команды
    if re.fullmatch(r"(?i)\s*affairs\s*", text):
        await affairs_cmd(update, ctx)
        return

    m = re.fullmatch(r"(?i)\s*affairs\s+delete\s+(\d+)\s*", text)
    if m:
        idx = int(m.group(1))
        mapping = LAST_LIST_INDEX.get(update.effective_chat.id)
        if not mapping or idx < 1 or idx > len(mapping):
            await update.message.reply_text("Неверный номер. Сначала открой список: «affairs».")
            return
        task_id = mapping[idx - 1]
        t = get_task(task_id)
        if not t or not t.is_active:
            await update.message.reply_text("Это дело уже удалено.")
            return
        cancel_task(task_id)
        for j in ctx.application.job_queue.get_jobs_by_name(f"task_{task_id}"):
            j.schedule_removal()
        await update.message.reply_text(f"🗑 Удалено: «{t.title}».")
        mapping.pop(idx - 1)
        return

    # Парсинг новой задачи
    parsed = parse_user_text_to_task(text, datetime.now(TZ))
    if not parsed:
        await update.message.reply_text(
            "Не понял формат. Примеры: «через 5 минут поесть», «сегодня в 18:30 позвонить», "
            "«каждый день в 07:45 зарядка», «30 августа в 10:00 оплатить кредит»."
        )
        return

    task_id = add_task(
        update.effective_chat.id,
        parsed.title,
        parsed.type,
        parsed.run_at_utc,
        parsed.hour,
        parsed.minute,
        parsed.day_of_month
    )
    # ВАЖНО: сразу ставим в планировщик
    await schedule_task(ctx.application, get_task(task_id))

    if parsed.type == "once":
        await update.message.reply_text(f"Отлично, напомню: «{parsed.title}» — {fmt_dt_kaliningrad(parsed.run_at_utc)}")
    elif parsed.type == "daily":
        await update.message.reply_text(f"Отлично, напомню: каждый день в {parsed.hour:02d}:{parsed.minute:02d} — «{parsed.title}»")
    else:
        await update.message.reply_text(f"Отлично, напомню: каждое {parsed.day_of_month} число в {parsed.hour:02d}:{parsed.minute:02d} — «{parsed.title}»")

# -------------------- АДМИН КОМАНДЫ --------------------
async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    set_maintenance(True)
    await update.message.reply_text("🟡 Технические работы включены.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    set_maintenance(False)
    await update.message.reply_text("🟢 Технические работы выключены.")
    # уведомим тех, кто писал во время работ
    with db() as conn:
        rows = conn.execute("SELECT chat_id FROM maintenance_waitlist").fetchall()
        conn.execute("DELETE FROM maintenance_waitlist")
        conn.commit()
    for (cid,) in rows:
        try:
            await ctx.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

async def keys_all_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): 
        await update.message.reply_text("Команда только для админа.")
        return
    with db() as conn:
        rows = conn.execute("SELECT key, used_by_chat_id FROM access_keys ORDER BY key").fetchall()
    lines = [f"{k} — {'занят (chat ' + str(cid) + ')' if cid else 'свободен'}" for k, cid in rows]
    await update.message.reply_text("Все ключи:\n" + "\n".join(lines))

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /keys_reset VIP001")
        return
    k = ctx.args[0].upper()
    if not re.fullmatch(r"VIP\d{3}", k):
        await update.message.reply_text("Неверный формат ключа. Пример: VIP001")
        return
    with db() as conn:
        conn.execute("UPDATE access_keys SET used_by_chat_id=NULL, used_at_utc=NULL WHERE key=?", (k,))
        conn.commit()
    await update.message.reply_text(f"Ключ {k} сброшен и снова свободен.")

# -------------------- MAIN --------------------
def main():
    start_health_server()
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))
    app.add_handler(CommandHandler("keys", keys_all_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_startup(app_: Application):
        await reschedule_all(app_)
        log.info("Bot started. Timezone=%s", TZ)

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
