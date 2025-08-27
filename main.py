# -*- coding: utf-8 -*-
"""
Telegram бот-напоминалка (приватный по ключам)
- TZ: Europe/Kaliningrad
- Приветствие:
  «Привет, я твой личный ассистент, буду помогать тебе с распределением рутинных задач,
   чтобы твой день проходил максимально эффективно.»
- Доступ только по ключам VIP001..VIP100 (одноразовые, можно вводить как VIP 001).
- Команды пользователя:
  • /start — приветствие (и запрос ключа, если не авторизован)
  • affairs — список 20 ближайших дел (от ближайшего к дальнему)
  • affairs delete <N> — удалить дело по номеру из последнего списка
- Добавление дел обычным текстом:
  • «через 5 минут поесть», «через 30 сек попить воды», «через 2 часа лечь»
  • «сегодня в 18:30 позвонить маме», «завтра в 09:00 отправить отчёт»
  • «каждый день в 07:45 зарядка»
  • «каждое 15 число в 10:00 платить за интернет»
  • «27.08.2025 в 14:00 встреча» (или «27.08 в 14:00 встреча»)
- Техработы (только админ):
  • /maintenance_on, /maintenance_off, /maintenance_status
- Админ-ключи:
  • /keys — все ключи (состояния)
  • /keys_free — свободные
  • /keys_used — занятые (с chat_id)
  • /keys_reset VIP001 — сбросить ключ
"""

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# -------------------- НАСТРОЙКИ --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")   # вставь токен от BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "963586834"))             # твой Telegram ID
TZ = ZoneInfo("Europe/Kaliningrad")
DB_PATH = "reminder_bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("reminder-bot")

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
        # флаг техработ
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('maintenance','0')")
        # заполнить ключи VIP001..VIP100 при первом запуске
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
            "key_used=excluded.key_used, authorized_at_utc=excluded.authorized_at_utc",
            (chat_id, 1, key_used, now)
        )
        conn.commit()

def try_consume_key(raw_text: str, chat_id: int) -> bool:
    k = re.sub(r"\s+", "", raw_text).upper()  # убираем пробелы, делаем верхний регистр
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

def ensure_authorized(update: Update) -> bool:
    if is_admin(update):
        return True
    chat_id = update.effective_chat.id
    if get_user_auth(chat_id):
        return True
    update.effective_message.reply_text(
        "Привет, я твой личный ассистент, буду помогать тебе с распределением рутинных задач, "
        "чтобы твой день проходил максимально эффективно.\n\n"
        "Этот бот приватный. Введите ключ доступа в формате «VIP001» (три буквы + три цифры)."
    )
    return False

# -------------------- ТЕХРАБОТЫ --------------------
def get_setting(key: str, default: str = "0") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()

def maintenance_on() -> bool:
    return get_setting("maintenance", "0") == "1"

def guard_maintenance(update: Update) -> bool:
    if maintenance_on() and not is_admin(update):
        try:
            update.effective_message.reply_text(
                "⚠️⚠️⚠️ Уважаемые пользователи, проводятся технические работы.\n"
                "Пожалуйста, попробуйте позже."
            )
        except Exception:
            pass
        waitlist_add(update.effective_chat.id)
        return True
    return False

def set_maintenance(value: bool):
    set_setting("maintenance", "1" if value else "0")

def waitlist_add(chat_id: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO maintenance_waitlist(chat_id) VALUES(?)", (chat_id,))
        conn.commit()

def waitlist_get_all() -> List[int]:
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT chat_id FROM maintenance_waitlist")]

def waitlist_clear():
    with db() as conn:
        conn.execute("DELETE FROM maintenance_waitlist")
        conn.commit()

# -------------------- ЗАДАЧИ --------------------
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
    tz: str
    is_active: bool
    created_at_utc: datetime
    last_triggered_utc: Optional[datetime]

def row_to_task(row: Tuple) -> Task:
    def dt(s): return datetime.fromisoformat(s) if s else None
    return Task(
        id=row[0], chat_id=row[1], title=row[2], type=row[3],
        run_at_utc=dt(row[4]), hour=row[5], minute=row[6], day_of_month=row[7],tz=row[8], is_active=bool(row[9]), created_at_utc=dt(row[10]), last_triggered_utc=dt(row[11])
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

def list_active_tasks(chat_id: int) -> List[Task]:
    with db() as conn:
        return [row_to_task(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE chat_id=? AND is_active=1", (chat_id,)
        )]

def mark_triggered(task_id: int):
    with db() as conn:
        conn.execute("UPDATE tasks SET last_triggered_utc=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), task_id))
        conn.commit()

# -------------------- УТИЛИТЫ --------------------
LAST_LIST_INDEX: Dict[int, List[int]] = {}  # chat_id -> task_ids из последнего списка

def fmt_dt_kaliningrad(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def compute_next_for_daily(hour: int, minute: int, now_tz: datetime) -> datetime:
    candidate = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_tz:
        candidate += timedelta(days=1)
    return candidate

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
            continue
    return now_tz + timedelta(days=30)

# -------------------- ПЛАНИРОВЩИК --------------------
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
        now_tz = datetime.now(TZ)
        nxt = compute_next_for_monthly(t.day_of_month, t.hour, t.minute, now_tz)
        ctx.job_queue.run_once(job_fire_monthly, nxt.astimezone(timezone.utc),
                               name=f"task_{t.id}", data={"task_id": t.id})

async def schedule_task(app: Application, t: Task):
    jq = app.job_queue
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

# -------------------- ПАРСЕР --------------------
RELATIVE_RE = re.compile(r"^\s*через\s+(\d+)\s*(секунд[ыу]?|сек|минут[уы]?|мин|час(?:а|ов)?|ч)\s+(.+)$", re.I)
TODAY_RE   = re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.I)
TOMORROW_RE= re.compile(r"^\s*завтра\s*в\s*(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.I)
DAILY_RE   = re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.I)
MONTHLY_RE = re.compile(r"^\s*кажд(?:ый|ое)\s*(\d{1,2})\s*число(?:\s*в\s*(\d{1,2})(?::(\d{2}))?)?\s+(.+)$", re.I)
DATE_RE    = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})(?::(\d{2}))?)?\s+(.+)$", re.I)

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
        if unit.startswith("сек"):
            delta = timedelta(seconds=amount)
        elif unit.startswith("мин"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("час") or unit == "ч":
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        run_local = now_tz + delta
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TODAY_RE.match(text)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        title = m.group(3).strip()
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz:
            run_local += timedelta(days=1)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TOMORROW_RE.match(text)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        title = m.group(3).strip()
        run_local = (now_tz + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DAILY_RE.match(text)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        title = m.group(3).strip()
        return ParsedTask("daily", title, None, h, mi, None)

    m = MONTHLY_RE.match(text)
    if m:
        day = int(m.group(1))
        h = int(m.group(2) or 10)
        mi = int(m.group(3) or 0)
        title = m.group(4).strip()
        return ParsedTask("monthly", title, None, h, mi, day)

    m = DATE_RE.match(text)
    if m:
        d = int(m.group(1)); mo = int(m.group(2)); y = int(m.group(3) or now_tz.year)
        h = int(m.group(4) or 10); mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        try:
            run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
            if run_local <= now_tz and not m.group(3):
                run_local = datetime(y + 1, mo, d, h, mi, tzinfo=TZ)
            return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)
        except ValueError:
            return None

    return None

# -------------------- ХЭНДЛЕРЫ (пользователь) --------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    if is_admin(update) or get_user_auth(update.effective_chat.id):
        await update.message.reply_text(
            "Привет, я твой личный ассистент, буду помогать тебе с распределением рутинных задач, " "чтобы твой день проходил максимально эффективно.\n\n"
            "Команды:\n"
            "• affairs — список дел (20 ближайших)\n"
            "• affairs delete <номер> — удалить дело по номеру\n\n"
            "Добавляй дела текстом: «через 5 минут поесть», «сегодня в 18:30…», «каждый день в 07:45…», "
            "«каждое 15 число в 10:00…», «27.08.2025 в 14:00…»"
        )
        return
    await update.message.reply_text(
        "Привет, я твой личный ассистент, буду помогать тебе с распределением рутинных задач, "
        "чтобы твой день проходил максимально эффективно.\n\n"
        "Этот бот приватный. Введите ключ доступа в формате «VIP001» (три буквы + три цифры)."
    )

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    if not ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    tasks = list_active_tasks(chat_id)

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

    if not tasks_sorted:
        await update.message.reply_text("Пока дел нет. Добавь что-нибудь: «через 2 минуты поспать».")
        return

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
            await update.message.reply_text("✅ Доступ подтверждён! Можешь добавлять дела и использовать команду «affairs».")
        else:
            if re.fullmatch(r"(?i)\s*vip\s*\d{3}\s*", text):
                await update.message.reply_text("⛔ Неверный или уже использованный ключ. Попробуй другой.")
            else:
                await update.message.reply_text("Этот бот приватный. Введите ключ доступа в формате «VIP001».")
        return

    # Текстовые команды
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

    # Добавление задач по тексту
    now_tz = datetime.now(TZ)
    parsed = parse_user_text_to_task(text, now_tz)
    if not parsed:
        await update.message.reply_text(
            "Не понял формат. Примеры:\n"
            "• через 5 минут поесть / через 30 сек попить воды\n"
            "• сегодня в 18:30 позвонить маме\n"
            "• завтра в 09:00 отправить отчёт\n"
          "• каждый день в 07:45 зарядка\n"
            "• каждое 15 число в 10:00 платить за интернет\n"
            "• 27.08.2025 в 14:00 встреча\n\n"
            "Список дел: «affairs». Удаление: «affairs delete 3»."
        )
        return

    task_id = add_task(
        chat_id=update.effective_chat.id,
        title=parsed.title,
        ttype=parsed.type,
        run_at_utc=parsed.run_at_utc,
        hour=parsed.hour,
        minute=parsed.minute,
        day_of_month=parsed.day_of_month
    )
    await schedule_task(ctx.application, get_task(task_id))

    if parsed.type == "once":
        await update.message.reply_text(f"✅ Ок! Напомню: «{parsed.title}» — {fmt_dt_kaliningrad(parsed.run_at_utc)}")
    elif parsed.type == "daily":
        await update.message.reply_text(f"✅ Ок! Ежедневно в {parsed.hour:02d}:{parsed.minute:02d} — «{parsed.title}»")
    else:
        await update.message.reply_text(
            f"✅ Ок! Каждое {parsed.day_of_month} число в {parsed.hour:02d}:{parsed.minute:02d} — «{parsed.title}»"
        )

# -------------------- ХЭНДЛЕРЫ (админ: техработы) --------------------
async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    set_maintenance(True)
    await update.message.reply_text("🟡 Технические работы включены. Пользователи будут предупреждены.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    set_maintenance(False)
    await update.message.reply_text("🟢 Технические работы выключены. Рассылаю уведомления…")
    for cid in waitlist_get_all():
        try:
            await ctx.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass
    waitlist_clear()

async def maintenance_status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    status = "включены" if maintenance_on() else "выключены"
    await update.message.reply_text(f"Статус техработ: {status}")

# -------------------- ХЭНДЛЕРЫ (админ: ключи) --------------------
async def keys_all_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    with db() as conn:
        rows = conn.execute("SELECT key, used_by_chat_id FROM access_keys ORDER BY key").fetchall()
    lines = [f"{k} — {'занят (chat ' + str(cid) + ')' if cid else 'свободен'}" for k, cid in rows]
    await update.message.reply_text("Все ключи:\n" + "\n".join(lines[:200]))

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    with db() as conn:
        rows = conn.execute("SELECT key FROM access_keys WHERE used_by_chat_id IS NULL ORDER BY key").fetchall()
    if not rows:
        await update.message.reply_text("Свободных ключей нет.")
        return
    await update.message.reply_text("Свободные ключи:\n" + ", ".join(r[0] for r in rows))

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    with db() as conn:
        rows = conn.execute("SELECT key, used_by_chat_id FROM access_keys WHERE used_by_chat_id IS NOT NULL ORDER BY key").fetchall()
    if not rows:
        await update.message.reply_text("Нет использованных ключей.")
        return
    await update.message.reply_text("Использованные ключи:\n" + "\n".join(f"{k} — chat {cid}" for k, cid in rows))

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
        row = conn.execute("SELECT key FROM access_keys WHERE key=?", (k,)).fetchone()
        if not row:
            await update.message.reply_text("Такого ключа нет.")
            return
        conn.execute("UPDATE access_keys SET used_by_chat_id=NULL, used_at_utc=NULL WHERE key=?", (k,))
        conn.commit()
    await update.message.reply_text(f"Ключ {k} сброшен и снова свободен.")

# -------------------- MAIN --------------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Пользовательские
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Админ: техработы
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))
    app.add_handler(CommandHandler("maintenance_status", maintenance_status_cmd))

    # Админ: ключи
    app.add_handler(CommandHandler("keys", keys_all_cmd))
    app.add_handler(CommandHandler("keys_free", keys_free_cmd))
    app.add_handler(CommandHandler("keys_used", keys_used_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    async def on_startup(app_: Application):
        await reschedule_all(app_)
        log.info("Bot started. Timezone=%s", TZ)

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
