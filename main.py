# -*- coding: utf-8 -*-
"""
Telegram бот-напоминалка
Фичи:
- Часовой пояс: Europe/Kaliningrad
- Понимает:
  1) «через 5 минут поесть», «через 2 часа лечь», «через 30 сек пить воду»
  2) «сегодня в 18:30 позвонить маме»
  3) «завтра в 09:00 отправить отчёт»
  4) «каждый день в 07:45 зарядка»
  5) «каждое 15 число в 10:00 платить за интернет»
  6) «27.08.2025 в 14:00 встреча» или «27.08 в 14:00 встреча»
- /tasks (или «fx дела») — 20 ближайших дел, с нумерацией
- fx del <номер> или /del <номер> — удалить дело по номеру из последнего списка
- Режим техработ: /maintenance_on /maintenance_off /maintenance_status — только админ
  (во время работ обычным пользователям бот отвечает предупреждением и запоминает чаты;
   при выключении рассылает «Бот снова работает.»)
"""

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Optional, Dict, Any, List, Tuple
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# -------------------- НАСТРОЙКИ --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")  # <<< подставь токен от BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "963586834"))            # твой Telegram ID
TZ = ZoneInfo("Europe/Kaliningrad")                           # UTC+2
DB_PATH = "reminder_bot.db"

USER_KEYBOARD = ReplyKeyboardMarkup(
    [["fx дела", "fx del "]],  # "fx del " — шаблон, допиши номер после пробела
    resize_keyboard=True
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
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

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('once','daily','monthly')),
            run_at_utc TEXT,          -- для одноразовых
            hour INTEGER,             -- для daily/monthly
            minute INTEGER,           -- для daily/monthly
            day_of_month INTEGER,     -- для monthly
            tz TEXT NOT NULL DEFAULT 'Europe/Kaliningrad',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at_utc TEXT NOT NULL,
            last_triggered_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS maintenance_waitlist (
            chat_id INTEGER PRIMARY KEY
        );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('maintenance','0')"
        )
        conn.commit()

def get_setting(key: str, default: str = "0") -> str:
    with db() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()

def maintenance_on() -> bool:
    return get_setting("maintenance", "0") == "1"

def set_maintenance(value: bool):
    set_setting("maintenance", "1" if value else "0")

def waitlist_add(chat_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO maintenance_waitlist(chat_id) VALUES(?)",
            (chat_id,)
        )
        conn.commit()

def waitlist_get_all() -> List[int]:
    with db() as conn:
        cur = conn.execute("SELECT chat_id FROM maintenance_waitlist")
        return [r[0] for r in cur.fetchall()]

def waitlist_clear():
    with db() as conn:
        conn.execute("DELETE FROM maintenance_waitlist")
        conn.commit()

# -------------------- МОДЕЛЬ ЗАДАЧ --------------------
@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str  # 'once' | 'daily' | 'monthly'
    run_at_utc: Optional[datetime]  # для once
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]
    tz: str
    is_active: bool
    created_at_utc: datetime
    last_triggered_utc: Optional[datetime]

def row_to_task(row: Tuple) -> Task:
    def parse_dt(s: Optional[str]) -> Optional[datetime]:
        return datetime.fromisoformat(s) if s else None
    return Task(
        id=row[0],
        chat_id=row[1],
        title=row[2],
        type=row[3],
        run_at_utc=parse_dt(row[4]),
        hour=row[5],
        minute=row[6],
        day_of_month=row[7],
        tz=row[8],
        is_active=bool(row[9]),
        created_at_utc=parse_dt(row[10]),
        last_triggered_utc=parse_dt(row[11]),
    )

def add_task(
    chat_id: int,
    title: str,
    ttype: str,
    run_at_utc: Optional[datetime],
    hour: Optional[int],
    minute: Optional[int],
    day_of_month: Optional[int],
    tzname: str = "Europe/Kaliningrad",
) -> int:
    with db() as conn:
        cur = conn.execute("""
        INSERT INTO tasks (chat_id,title,type,run_at_utc,hour,minute,day_of_month,tz,is_active,created_at_utc)
        VALUES (?,?,?,?,?,?,?,?,1,?)
        """, (
            chat_id, title, ttype,
            run_at_utc.isoformat() if run_at_utc else None,
            hour, minute, day_of_month,
            tzname,
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
        return cur.lastrowid

def get_task(task_id: int) -> Optional[Task]:
    with db() as conn:
        cur = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        return row_to_task(row) if row else None

def cancel_task(task_id: int):
    with db() as conn:
        conn.execute("UPDATE tasks SET is_active=0 WHERE id=?", (task_id,))
        conn.commit()

def list_active_tasks(chat_id: Optional[int] = None) -> List[Task]:
    with db() as conn:
        if chat_id is None:
            cur = conn.execute("SELECT * FROM tasks WHERE is_active=1")
        else:
            cur = conn.execute(
                "SELECT * FROM tasks WHERE chat_id=? AND is_active=1", (chat_id,)
            )
        return [row_to_task(r) for r in cur.fetchall()]

def mark_triggered(task_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE tasks SET last_triggered_utc=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), task_id)
        )
        conn.commit()

# -------------------- ВСПОМОГАТЕЛЬНОЕ --------------------
LAST_LIST_INDEX: Dict[int, List[int]] = {}  # chat_id -> [task_ids по последнему списку]

def is_admin(update: Update) -> bool:
    return (update.effective_user and update.effective_user.id == ADMIN_ID)

def guard_maintenance(update: Update) -> bool:
    """Вернёт True, если нужно прервать обработку (идут техработы и это не админ)."""
    if maintenance_on() and not is_admin(update):
        try:
            update.effective_message.reply_text(
                "⚠️⚠️⚠️ Уважаемые пользователи, проводятся технические работы.\n"
                "Пожалуйста, попробуйте позже."
            )
        except Exception:
            pass
        if update.effective_chat:
            waitlist_add(update.effective_chat.id)
        return True
    return False

def fmt_dt_kaliningrad(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(TZ)
    return local.strftime("%d.%m.%Y %H:%M")

def compute_next_for_daily(hour: int, minute: int, now_tz: datetime) -> datetime:
    candidate = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_tz:
        candidate += timedelta(days=1)
    return candidate

def compute_next_for_monthly(day: int, hour: int, minute: int, now_tz: datetime) -> datetime:
    # Ищем ближайшую валидную дату (учитывая 29/30/31)
    y, m = now_tz.year, now_tz.month
    # Первую попытку делаем в текущем месяце (если валидно и позже текущего времени)
    for _ in range(24):  # до 2 лет вперёд — запас
        try:
            candidate = datetime(y, m, day, hour, minute, tzinfo=TZ)
            if candidate > now_tz:
                return candidate
            # иначе двигаем месяц
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
        except ValueError:
            # такого дня нет в этом месяце — двигаем месяц
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
            continue
    return now_tz + timedelta(days=30)

# -------------------- ПЛАНИРОВЩИК --------------------
async def job_fire(context: ContextTypes.DEFAULT_TYPE):
    task_id = context.job.data["task_id"]
    t = get_task(task_id)
    if not t or not t.is_active:
        return
    try:
        await context.bot.send_message(chat_id=t.chat_id, text=f"🔔 Напоминание: {t.title}")
    finally:
        mark_triggered(task_id)
        if t.type == "once":
            cancel_task(task_id)

async def job_fire_monthly(context: ContextTypes.DEFAULT_TYPE):
    task_id = context.job.data["task_id"]
    t = get_task(task_id)
    if not t or not t.is_active:
        return
    try:
        await context.bot.send_message(chat_id=t.chat_id, text=f"🔔 Напоминание: {t.title}")
    finally:
        mark_triggered(task_id)
        # перепланировать на следующее подходящее «число в HH:MM»
        now_tz = datetime.now(TZ)
        nxt = compute_next_for_monthly(t.day_of_month, t.hour, t.minute, now_tz)
        context.job_queue.run_once(
            callback=job_fire_monthly,
            when=nxt.astimezone(timezone.utc),
            name=f"task_{t.id}",
            data={"task_id": t.id}
        )

async def schedule_task(app: Application, task: Task):
    jq = app.job_queue
    name = f"task_{task.id}"
    # Сносим старые jobs с тем же именем
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()

    if not task.is_active:
        return

    if task.type == "once":
        if task.run_at_utc and task.run_at_utc > datetime.now(timezone.utc):
            jq.run_once(job_fire, when=task.run_at_utc, name=name, data={"task_id": task.id})
    elif task.type == "daily":
        jq.run_daily(
            callback=job_fire,
            time=time(task.hour, task.minute, tzinfo=TZ),
            name=name,
            data={"task_id": task.id}
        )
    elif task.type == "monthly":
        now_tz = datetime.now(TZ)
        nxt = compute_next_for_monthly(task.day_of_month, task.hour, task.minute, now_tz)
        jq.run_once(
            callback=job_fire_monthly,
            when=nxt.astimezone(timezone.utc),
            name=name,
            data={"task_id": task.id}
        )

async def reschedule_all(app: Application):
    # Поднимаем все активные задачи для всех чатов
    for t in list_active_tasks(chat_id=None):
        await schedule_task(app, t)

# -------------------- ПАРСЕР КОМАНД --------------------
RELATIVE_RE = re.compile(r"^\s*через\s+(\d+)\s*(секунд[ыу]?|сек|минут[уы]?|мин|час(?:а|ов)?|ч)\s+(.+)$", re.I)
TODAY_RE   = re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.I)
TOMORROW_RE= re.compile(r"^\s*завтра\s*в\s*(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.I)
DAILY_RE   = re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})(?::(\d{2}))?\s+(.+)$", re.I)
MONTHLY_RE = re.compile(r"^\s*кажд(?:ый|ое)\s*(\d{1,2})\s*число(?:\s*в\s*(\d{1,2})(?::(\d{2}))?)?\s+(.+)$", re.I)
DATE_RE    = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})(?::(\d{2}))?)?\s+(.+)$", re.I)

@dataclass
class ParsedTask:
    type: str                     # 'once'|'daily'|'monthly'
    title: str
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def parse_user_text_to_task(text: str, now_tz: datetime) -> Optional[ParsedTask]:
    text = text.strip()

    # 1) через N единиц
    m = RELATIVE_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        title = m.group(3).strip()
        if amount < 0:
            return None
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

    # 2) сегодня в HH(:MM)
    m = TODAY_RE.match(text)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        title = m.group(3).strip()
        if not (0 <= h < 24 and 0 <= mi < 60):
            return None
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz:
            run_local += timedelta(days=1)  # если уже прошло — на завтра
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    # 3) завтра в HH(:MM)
    m = TOMORROW_RE.match(text)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        title = m.group(3).strip()
        if not (0 <= h < 24 and 0 <= mi < 60):
            return None
        run_local = (now_tz + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    # 4) каждый день в HH(:MM)
    m = DAILY_RE.match(text)
    if m:
        h = int(m.group(1)); mi = int(m.group(2) or 0)
        title = m.group(3).strip()
        if not (0 <= h < 24 and 0 <= mi < 60):
            return None
        return ParsedTask("daily", title, None, h, mi, None)

    # 5) каждое <день> число (в HH:MM)?
    m = MONTHLY_RE.match(text)
    if m:
        day = int(m.group(1))
        h = int(m.group(2) or 10)
        mi = int(m.group(3) or 0)
        title = m.group(4).strip()
        if not (1 <= day <= 31 and 0 <= h < 24 and 0 <= mi < 60):
            return None
        return ParsedTask("monthly", title, None, h, mi, day)

    # 6) DD.MM(.YYYY)? (в HH:MM)? <текст>
    m = DATE_RE.match(text)
    if m:
        d = int(m.group(1)); mo = int(m.group(2)); y = int(m.group(3) or now_tz.year)
        h = int(m.group(4) or 10); mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        try:
            run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
            if run_local <= now_tz:
                # если год не указан и дата уже прошла — переносим на следующий год
                if not m.group(3):
                    run_local = datetime(y + 1, mo, d, h, mi, tzinfo=TZ)
            return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)
        except ValueError:
            return None

    return None

# -------------------- ХЭНДЛЕРЫ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    await update.message.reply_text(
        "Привет! Я бот-напоминалка.\n"
        "Понимаю такие формы:\n"
        "• «через 5 минут поесть» / «через 30 сек попить воды» / «через 2 часа лечь»\n"
        "• «сегодня в 18:30 позвонить маме»\n"
        "• «завтра в 09:00 отправить отчёт»\n"
        "• «каждый день в 07:45 зарядка»\n"
        "• «каждое 15 число в 10:00 платить за интернет»\n"
        "• «27.08.2025 в 14:00 встреча»\n\n"
        "Команды:\n"
        "• /tasks — показать 20 ближайших дел (или «fx дела»)\n"
        "• fx del <номер> (или /del <номер>) — удалить дело по номеру из списка\n"
        "• /maintenance_on /maintenance_off /maintenance_status — только админ",
        reply_markup=USER_KEYBOARD
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        return
    chat_id = update.effective_chat.id
    tasks = list_active_tasks(chat_id)

    # Сортируем по ближайшему запуску
    now_tz = datetime.now(TZ)

    def next_run(t: Task) -> datetime:
        if t.type == "once":
            return t.run_at_utc.astimezone(TZ) if t.run_at_utc else now_tz + timedelta(days=3650)
        elif t.type == "daily":
            return compute_next_for_daily(t.hour, t.minute, now_tz)
        else:
            return compute_next_for_monthly(t.day_of_month, t.hour, t.minute, now_tz)

    tasks_sorted = sorted(tasks, key=next_run)[:20]
    LAST_LIST_INDEX[chat_id] = [t.id for t in tasks_sorted]

    if not tasks_sorted:
        await update.message.reply_text("Пока дел нет. Добавь что-нибудь, например: «через 2 минуты поспать».")
        return

    lines = []
    for idx, t in enumerate(tasks_sorted, 1):
        if t.type == "once":
            when = fmt_dt_kaliningrad(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{idx}. {t.title} — {when}")
    await update.message.reply_text("Ближайшие дела:\n" + "\n".join(lines))

async def del_by_index(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: Optional[int] = None):
    chat_id = update.effective_chat.id
    if idx is None:
        if guard_maintenance(update):
            return
        args = context.args
        if not args or not args[0].isdigit():
            await update.message.reply_text("Формат: fx del <номер>  (номер из /tasks).")
            return
        idx = int(args[0])

    mapping = LAST_LIST_INDEX.get(chat_id)
    if not mapping or idx < 1 or idx > len(mapping):
        await update.message.reply_text("Неверный номер. Сначала покажи список: /tasks или «fx дела».")
        return

    task_id = mapping[idx - 1]
    t = get_task(task_id)
    if not t or not t.is_active:
        await update.message.reply_text("Это дело уже удалено.")
        return

    cancel_task(task_id)
    # Снесём jobs с таким именем
    name = f"task_{task_id}"
    for job in context.application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    await update.message.reply_text(f"🗑 Удалено: «{t.title}».")
    mapping.pop(idx - 1)

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await del_by_index(update, context, None)

async def fx_text_shortcuts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if guard_maintenance(update):
        return

    # «fx дела»
    if re.fullmatch(r"(?i)fx\s+дела", text):
        await tasks_cmd(update, context)
        return

    # «fx del <номер>»
    m = re.match(r"(?i)^fx\s+del\s+(\d+)\s*$", text)
    if m:
        await del_by_index(update, context, int(m.group(1)))
        return

    # иначе — попытка распарсить как добавление задачи
    await add_by_nlp(update, context)

async def add_by_nlp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if guard_maintenance(update):
        return
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
            "• 27.08.2025 в 14:00 встреча"
        )
        return

    task_id = add_task(
        chat_id=update.effective_chat.id,
        title=parsed.title,
        ttype=parsed.type,
        run_at_utc=parsed.run_at_utc,
        hour=parsed.hour,
        minute=parsed.minute,
        day_of_month=parsed.day_of_month,
        tzname="Europe/Kaliningrad",
    )
    # Подписать задачу в планировщике
    await schedule_task(context.application, get_task(task_id))

    # Ответ пользователю
    if parsed.type == "once":
        when = fmt_dt_kaliningrad(parsed.run_at_utc)
        await update.message.reply_text(f"✅ Ок! Напомню: «{parsed.title}» — {when}")
    elif parsed.type == "daily":
        await update.message.reply_text(f"✅ Ок! Ежедневно в {parsed.hour:02d}:{parsed.minute:02d} — «{parsed.title}»")
    else:
        await update.message.reply_text(
            f"✅ Ок! Каждое {parsed.day_of_month} число в {parsed.hour:02d}:{parsed.minute:02d} — «{parsed.title}»"
        )

# -------------------- ТЕХРАБОТЫ (админ) --------------------
async def maintenance_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    set_maintenance(True)
    await update.message.reply_text("🟡 Технические работы включены. Пользователи будут предупреждены.")

async def maintenance_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    set_maintenance(False)
    await update.message.reply_text("🟢 Технические работы выключены. Рассылаю уведомления…")
    chats = waitlist_get_all()
    for cid in chats:
        try:
            await context.bot.send_message(chat_id=cid, text="✅ Бот снова работает.")
        except Exception:
            pass
    waitlist_clear()

async def maintenance_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команда только для админа.")
        return
    status = "включены" if maintenance_on() else "выключены"
    await update.message.reply_text(f"Статус техработ: {status}")

# -------------------- MAIN --------------------
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))
    app.add_handler(CommandHandler("maintenance_status", maintenance_status_cmd))

    # Текстовые обработки: «fx дела», «fx del N», иначе — создать задачу
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fx_text_shortcuts))

    async def on_startup(app_: Application):
        await reschedule_all(app_)
        log.info("Bot started. Timezone=%s", TZ)

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
