# -*- coding: utf-8 -*-
"""
Личный ассистент-бот (Telegram) — Render-ready.

Функции:
- Приватный доступ ключами VIP001..VIP100 (вшиты, хранятся и помечаются в SQLite)
- Приветствие с примерами форматов
- Парсинг:
    • «через 30 секунд поесть», «через 5 минут позвонить», «через 2 часа …»
    • «сегодня в 18:30 …», «завтра в 09:00 …»
    • «каждый день в 07:45 …»
    • «30 августа в 10:00 …», «30.08.2025 в 10:00 …»
- /affairs — список ближайших дел (до 20), сортировка по ближайшему запуску
- "affairs delete N" (текст) и /affairs_delete N — удалить по номеру из последнего списка
- Техработы: /maintenance_on и /maintenance_off (запоминает чаты и шлёт «бот снова работает»)
- Ключи (только админ): /keys_left, /keys_free, /keys_used, /keys_reset VIP001
- Persist всех задач в SQLite + автопланирование при старте
- Снятие webhook при старте (исключает getUpdates Conflict на Render)
- Healthcheck HTTP на $PORT (для пинга аптаймом)

Авторизация запоминается в таблице users (переживает рестарты).
"""

import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional, List, Dict, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = "8492146866:AAE6yWRhg1wa9qn7_PV3NRJS6lh1dFtjxqA"
ADMIN_ID = 963586834
TZ = ZoneInfo("Europe/Kaliningrad")
DB_PATH = "assistant.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("assistant-bot")

# ---------- HEALTHCHECK (Render) ----------
class _Health(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # тихий сервер
        return
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

def start_health():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------- БАЗА ДАННЫХ ----------
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
        INSERT OR IGNORE INTO settings(key,value) VALUES('maintenance','0');

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
            day_of_month INTEGER
        );

        CREATE TABLE IF NOT EXISTS maintenance_waitlist (
            chat_id INTEGER PRIMARY KEY
        );
        """)
        # автозаполнение ключей VIP001..VIP100
        existing = {r[0] for r in conn.execute("SELECT key FROM access_keys")}
        to_add = [(f"VIP{i:03d}",) for i in range(1,101) if f"VIP{i:03d}" not in existing]
        if to_add:
            conn.executemany("INSERT INTO access_keys(key) VALUES(?)", to_add)
        conn.commit()

# ---------- ДОСТУП/КЛЮЧИ ----------
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID
def is_authorized(chat_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT is_authorized FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row[0])

def try_consume_key(text: str, chat_id: int) -> bool:
    key = re.sub(r"\s+", "", (text or "")).upper()
    if not re.fullmatch(r"VIP\d{3}", key):
        return False
    with db() as conn:
        row = conn.execute("SELECT key, used_by_chat_id FROM access_keys WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        if row[1] and row[1] != chat_id:
            return False  # ключ уже занят другим
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO users(chat_id,is_authorized,key_used,authorized_at_utc) VALUES(?,?,?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET is_authorized=excluded.is_authorized,"
                     " key_used=excluded.key_used, authorized_at_utc=excluded.authorized_at_utc",
                     (chat_id,1,key,now))
        conn.execute("UPDATE access_keys SET used_by_chat_id=?, used_at_utc=? WHERE key=?",
                     (chat_id, now, key))
        conn.commit()
        return True

def keys_left() -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM access_keys WHERE used_by_chat_id IS NULL").fetchone()
        return int(row[0]) if row else 0

# ---------- ТЕХРАБОТЫ ----------
def maintenance_on() -> bool:
    with db() as conn:
        v = conn.execute("SELECT value FROM settings WHERE key='maintenance'").fetchone()
        return (v and v[0] == "1")

def set_maintenance(flag: bool):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('maintenance',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("1" if flag else "0",))
        conn.commit()

def guard_maintenance(update: Update) -> bool:
    if maintenance_on() and not is_admin(update):
        try:
            update.effective_message.reply_text(
                "⚠️⚠️⚠️ Уважаемые пользователи, проводятся технические работы. Попробуйте позже."
            )
        except Exception:
            pass
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO maintenance_waitlist(chat_id) VALUES(?)",
                         (update.effective_chat.id,))
            conn.commit()
        return True
    return False

# ---------- МОДЕЛЬ ЗАДАЧ ----------
@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str           # 'once' | 'daily' | 'monthly'
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def row_to_task(row: Tuple) -> Task:
    return Task(
        id=row[0], chat_id=row[1], title=row[2], type=row[3],
        run_at_utc=datetime.fromisoformat(row[4]) if row[4] else None,
        hour=row[5], minute=row[6], day_of_month=row[7]
    )

def add_task(chat_id: int, title: str, ttype: str,
             run_at_utc: Optional[datetime], hour: Optional[int],
             minute: Optional[int], day_of_month: Optional[int]) -> int:
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO tasks (chat_id,title,type,run_at_utc,hour,minute,day_of_month)
            VALUES (?,?,?,?,?,?,?)
        """, (chat_id, title, ttype,
              run_at_utc.isoformat() if run_at_utc else None,
              hour, minute, day_of_month))
        conn.commit()
        return cur.lastrowid

def get_task(task_id: int) -> Optional[Task]:
    with db() as conn:
        row = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE id=?",
                           (task_id,)).fetchone()
        return row_to_task(row) if row else None

def list_tasks(chat_id: int) -> List[Task]:
    with db() as conn:
        rows = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE chat_id=?",
                            (chat_id,)).fetchall()
        return [row_to_task(r) for r in rows]

def delete_task(task_id: int):
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()

# ---------- ПАРСЕР ТЕКСТА ----------
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12
}

RELATIVE_RE = re.compile(
    r"^\s*через\s+(\d+)\s*(секунд(?:у|ы)?|сек|с|минут(?:у|ы)?|мин|м|час(?:а|ов)?|ч)\s+(.+)$",
    re.I
)
TODAY_RE    = re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
TOMORROW_RE = re.compile(r"^\s*завтра\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DAILY_RE    = re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DATE_NUM_RE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)
DATE_TXT_RE = re.compile(r"^\s*(\d{1,2})\s+([а-яА-Я]+)(?:\s+(\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)

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
        else:
            delta = timedelta(hours=amount)

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

    m = DATE_NUM_RE.match(text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or now_tz.year)
        h, mi = int(m.group(4) or 10), int(m.group(5) or 0)
        title = m.group(6).strip()
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3):
            run_local = datetime(y+1, mo, d, h, mi, tzinfo=TZ)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DATE_TXT_RE.match(text)
    if m:
        d, mon = int(m.group(1)), m.group(2).lower()
        if mon not in MONTHS:
            return None
        y = int(m.group(3) or now_tz.year)
        h, mi = int(m.group(4) or 10), int(m.group(5) or 0)
        title = m.group(6).strip()
        mo = MONTHS[mon]
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3):
            run_local = datetime(y+1, mo, d, h, mi, tzinfo=TZ)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    return None

# ---------- ПЛАНИРОВЩИК ----------
def fmt_dt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if t:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def job_monthly(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if not t:
        return
    if datetime.now(TZ).day == t.day_of_month:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def schedule_task(app: Application, t: Optional[Task]):
    if t is None:
        return
    jq = app.job_queue
    # удалить старые джобы с тем же именем
    for j in jq.get_jobs_by_name(f"task_{t.id}"):
        j.schedule_removal()

    if t.type == "once" and t.run_at_utc and t.run_at_utc > datetime.now(timezone.utc):
        jq.run_once(job_once, t.run_at_utc, name=f"task_{t.id}", data={"task_id": t.id})
    elif t.type == "daily":
        jq.run_daily(job_once, time=dtime(t.hour, t.minute, tzinfo=TZ),
                     name=f"task_{t.id}", data={"task_id": t.id})
    elif t.type == "monthly":
        jq.run_daily(job_monthly, time=dtime(t.hour, t.minute, tzinfo=TZ),
                     name=f"task_{t.id}", data={"task_id": t.id})

async def reschedule_all(app: Application):
    with db() as conn:
        rows = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks").fetchall()
    for r in rows:
        await schedule_task(app, row_to_task(r))

# ---------- КОМАНДЫ ----------
LAST_LIST: Dict[int, List[int]] = {}  # chat_id -> [task_ids]

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    chat_id = update.effective_chat.id
    if is_authorized(chat_id) or is_admin(update):
        await update.message.reply_text(
            "👋 Привет, я твой личный ассистент. Я помогу тебе оптимизировать все твои рутинные задачи, "
            "чтобы ты сосредоточился на самом главном и ничего не забыл.\n\n"
            "Примеры:\n"
            "• через 2 минуты / через 5 минут — поесть\n"
            "• сегодня в 18:30 — попить воды\n"
            "• завтра в 09:00 — сходить в зал\n"
            "• каждый день в 07:45 — чистить зубы\n"
            "• 30 августа в 10:00 — оплатить кредит\n\n"
            "❗ Чтобы напомнить «за N минут до встречи», просто поставь напоминание на время N минут раньше."
        )
    else:
        await update.message.reply_text("Этот бот приватный. Введите приватный ключ в формате ABC123 (например, VIP003).")

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    chat_id = update.effective_chat.id
    if not (is_authorized(chat_id) or is_admin(update)):
        await update.message.reply_text("Этот бот приватный. Введите ключ VIPxxx.")
        return

    tasks = list_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("Пока дел нет.")
        return

    now = datetime.now(TZ)

    def next_run(t: Task) -> datetime:
        if t.type == "once" and t.run_at_utc:
            return t.run_at_utc.astimezone(TZ)
        if t.type == "daily":
            cand = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if cand <= now: cand += timedelta(days=1)
            return cand
        # monthly
        y, m = now.year, now.month
        for _ in range(24):
            try:
                cand = datetime(y, m, t.day_of_month, t.hour, t.minute, tzinfo=TZ)
                if cand > now: return cand
                m = 1 if m == 12 else m + 1
                if m == 1: y += 1
            except ValueError:
                m = 1 if m == 12 else m + 1
                if m == 1: y += 1
        return now + timedelta(days=30)

    tasks_sorted = sorted(tasks, key=next_run)[:20]
    LAST_LIST[chat_id] = [t.id for t in tasks_sorted]

    lines = []
    for i, t in enumerate(tasks_sorted, 1):
        if t.type == "once":
            when = fmt_dt_local(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {when}")
        await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    chat_id = update.effective_chat.id
    if not (is_authorized(chat_id) or is_admin(update)):
        await update.message.reply_text("Этот бот приватный. Введите ключ VIPxxx.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /affairs_delete <номер>")
        return
    idx = int(ctx.args[0])
    ids = LAST_LIST.get(chat_id)
    if not ids or idx < 1 or idx > len(ids):
        await update.message.reply_text("Сначала открой список /affairs и проверь номер.")
        return
    tid = ids[idx-1]
    t = get_task(tid)
    if t:
        delete_task(t.id)
        await update.message.reply_text(f"🗑 Удалено: «{t.title}».")
    else:
        await update.message.reply_text("Это дело уже удалено.")

# админ — ключи
async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    await update.message.reply_text(f"Свободных ключей: {keys_left()} из 100.")

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    with db() as conn:
        rows = conn.execute("SELECT key FROM access_keys WHERE used_by_chat_id IS NULL ORDER BY key").fetchall()
    await update.message.reply_text("Свободные: " + (", ".join(r[0] for r in rows) if rows else "нет"))

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    with db() as conn:
        rows = conn.execute("SELECT key, used_by_chat_id FROM access_keys WHERE used_by_chat_id IS NOT NULL ORDER BY key").fetchall()
    if not rows:
        await update.message.reply_text("Использованных ключей нет.")
        return
    lines = [f"{k} — chat {cid}" for k, cid in rows]
    await update.message.reply_text("Использованные:\n" + "\n".join(lines))

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    if not ctx.args or not re.fullmatch(r"VIP\d{3}", ctx.args[0].upper()):
        await update.message.reply_text("Формат: /keys_reset VIP001")
        return
    key = ctx.args[0].upper()
    with db() as conn:
        conn.execute("UPDATE access_keys SET used_by_chat_id=NULL, used_at_utc=NULL WHERE key=?", (key,))
        # снимаем авторизацию у пользователя, если надо
        conn.execute("UPDATE users SET is_authorized=0, key_used=NULL WHERE key_used=?", (key,))
        conn.commit()
    await update.message.reply_text(f"Ключ {key} сброшен (свободен).")

# техработы
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
    # уведомляем ожидавших
    with db() as conn:
        rows = conn.execute("SELECT chat_id FROM maintenance_waitlist").fetchall()
        conn.execute("DELETE FROM maintenance_waitlist")
        conn.commit()
    for (cid,) in rows:
        try:
            await ctx.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# ---------- ТЕКСТЫ ----------
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return

    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # Авторизация по ключу (только если ещё не авторизован)
    if not is_authorized(chat_id) and not is_admin(update):
        if try_consume_key(text, chat_id):
            await update.message.reply_text("✅ Доступ подтверждён! Теперь можешь добавлять дела и использовать команду «/affairs».")
        else:
            await update.message.reply_text("❌ Неверный ключ. Введи ключ формата VIPxxx (например, VIP003).")
        return

    # Текстовый "affairs delete N"
    m = re.fullmatch(r"(?i)\s*affairs\s+delete\s+(\d+)\s*", text)
    if m:
        idx = int(m.group(1))
        ids = LAST_LIST.get(chat_id)
        if not ids or idx < 1 or idx > len(ids):
            await update.message.reply_text("Сначала открой список /affairs и проверь номер.")
            return
        tid = ids[idx-1]
        t = get_task(tid)
        if t:
            delete_task(t.id)
            await update.message.reply_text(f"🗑 Удалено: «{t.title}».")
        else:
            await update.message.reply_text("Это дело уже удалено.")
        return

    # Парсинг новой задачи
    parsed = parse_user_text_to_task(text, datetime.now(TZ))
    if not parsed:
        await update.message.reply_text(
            "⚠️ Не понял задачу. Примеры: «через 5 минут поесть», «сегодня в 18:30 позвонить», "
            "«каждый день в 07:45 зарядка», «30 августа в 10:00 оплатить кредит»."
        )
        return

    task_id = add_task(chat_id, parsed.title, parsed.type,
                       parsed.run_at_utc, parsed.hour, parsed.minute, parsed.day_of_month)
    t = get_task(task_id)
    if not t:
        await update.message.reply_text("⚠️ Не удалось сохранить задачу. Попробуй ещё раз.")
        return
    await schedule_task(ctx.application, t)

    if t.type == "once":
        await update.message.reply_text(f"Отлично, напомню: «{t.title}» — {fmt_dt_local(t.run_at_utc)}")
    elif t.type == "daily":
        await update.message.reply_text(f"Отлично, напомню: каждый день в {t.hour:02d}:{t.minute:02d} — «{t.title}»")
    else:
        await update.message.reply_text(f"Отлично, напомню: каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d} — «{t.title}»")

# ================ MAIN ================
def main():
    start_health()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

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

    async def on_startup(app_: Application):
        # убираем любой webhook, чтобы polling не конфликтовал (Conflict)
        await app_.bot.delete_webhook(drop_pending_updates=True)
        await reschedule_all(app_)
        import telegram, sys
        log.info(
            "Bot started. TZ=%s | PTB=%s | Python=%s",
            TZ,
            getattr(telegram, "__version__", "unknown"),
            sys.version.split()[0],
        )

    app.post_init = on_startup
    app.run_polling()  # максимально простой запуск без дополнительных аргументов


if __name__ == "__main__":
    main()
