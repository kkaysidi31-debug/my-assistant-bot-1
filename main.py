# -*- coding: utf-8 -*-
"""
Личный ассистент-бот с напоминаниями.

Функции:
- Приватный доступ по ключам (VIP001–VIP100).
- Приветствие с примерами.
- Парсинг задач:
    • «через 30 секунд поесть», «через 5 минут позвонить», «через 2 часа …»
    • «сегодня в 18:30 …»
    • «завтра в 09:00 …»
    • «каждый день в 07:45 …»
    • «30.08 в 10:00 …» или «30 августа в 10:00 …» (год опционально)
- Список дел: /affairs
- Удаление по номеру: «affairs delete 3» (и команда /affairs_delete 3)
- Админ: /maintenance_on и /maintenance_off
- SQLite-хранение задач (переживают рестарт)
- Автовосстановление задач при старте
- Снятие webhook при старте (исключает Conflict на Render)
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional, List, Dict, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = "8492146866:AAE6yWRhg1wa9qn7_PV3NRJS6lh1dFtjxqA"
ADMIN_ID = 963586834
TZ = ZoneInfo("Europe/Kaliningrad")
DB_PATH = "tasks.db"

# Приватные ключи (вшиты в код)
ACCESS_KEYS = {f"VIP{i:03d}" for i in range(1, 101)}
# Кто уже авторизован (in-memory; при желании можно вынести в БД)
AUTHORIZED: Dict[int, bool] = {}

MAINTENANCE = False  # флаг техработ

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("reminder-bot")

# ===================== МОДЕЛЬ/БД =====================
@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str  # 'once' | 'daily' | 'monthly'
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                type TEXT NOT NULL,
                run_at_utc TEXT,
                hour INTEGER,
                minute INTEGER,
                day_of_month INTEGER
            );
            """
        )
        conn.commit()

def add_task(chat_id: int, title: str, type_: str,
             run_at_utc: Optional[datetime], hour: Optional[int],
             minute: Optional[int], day_of_month: Optional[int]) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                chat_id, title, type_,
                run_at_utc.isoformat() if run_at_utc else None,
                hour, minute, day_of_month
            )
        )
        conn.commit()
        return cur.lastrowid

def row_to_task(row: Tuple) -> Task:
    return Task(
        id=row[0],
        chat_id=row[1],
        title=row[2],
        type=row[3],
        run_at_utc=datetime.fromisoformat(row[4]) if row[4] else None,
        hour=row[5],
        minute=row[6],
        day_of_month=row[7]
    )

def get_task(task_id: int) -> Optional[Task]:
    with db() as conn:
        row = conn.execute(
            "SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE id=?",
            (task_id,)
        ).fetchone()
        return row_to_task(row) if row else None

def list_tasks(chat_id: int) -> List[Task]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE chat_id=?",
            (chat_id,)
        ).fetchall()
        return [row_to_task(r) for r in rows]

def delete_task(task_id: int):
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()

# ===================== ПАРСЕР =====================
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# через N [сек/секунд/секунду/мин/минут/минуту/ч/час/часа/часов] <текст>
RELATIVE_RE = re.compile(
    r"^\s*через\s+(\d+)\s*(секунд(?:у|ы)?|сек|с|минут(?:у|ы)?|мин|м|час(?:а|ов)?|ч)\s+(.+)$",
    re.I
)
TODAY_RE    = re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
TOMORROW_RE = re.compile(r"^\s*завтра\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DAILY_RE    = re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
# 30.08[.2025] в 10:00 <текст>
DATE_NUM_RE = re.compile(
    r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$",
    re.I
)
# 30 августа [2025] в 10:00 <текст>
DATE_TXT_RE = re.compile(
    r"^\s*(\d{1,2})\s+([а-яА-Я]+)(?:\s+(\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$",
    re.I
)

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
        d = int(m.group(1))
        mo = int(m.group(2))
        y = int(m.group(3) or now_tz.year)
        h = int(m.group(4) or 10)
        mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3):
            run_local = datetime(y + 1, mo, d, h, mi, tzinfo=TZ)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DATE_TXT_RE.match(text)
    if m:
        d = int(m.group(1))
        mon = m.group(2).lower()
        if mon not in MONTHS:
            return None
        y = int(m.group(3) or now_tz.year)
        h = int(m.group(4) or 10)
        mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        mo = MONTHS[mon]
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3):
            run_local = datetime(y + 1, mo, d, h, mi, tzinfo=TZ)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    return None

# ===================== ПЛАНИРОВЩИК =====================
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

LAST_LIST: Dict[int, List[int]] = {}  # chat_id -> [task_ids в последнем списке]

async def remind_once(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if not t:
        return
    await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def remind_monthly(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if not t:
        return
    # проверяем число
    now = datetime.now(TZ)
    if now.day == t.day_of_month:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def schedule_task(app: Application, t: Task):
    if not t:
        return
    jq = app.job_queue
    name = f"task_{t.id}"
    for j in jq.get_jobs_by_name(name):
        j.schedule_removal()

    if t.type == "once" and t.run_at_utc:
        if t.run_at_utc > datetime.now(timezone.utc):
            jq.run_once(remind_once, when=t.run_at_utc, name=name, data={"task_id": t.id})
    elif t.type == "daily":
        jq.run_daily(remind_once, time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                     name=name, data={"task_id": t.id})
    elif t.type == "monthly":
        jq.run_daily(remind_monthly, time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                     name=name, data={"task_id": t.id})

async def reschedule_all(app: Application):
    with db() as conn:
        rows = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks").fetchall()
    for r in rows:
        await schedule_task(app, row_to_task(r))

# ===================== ПОМОЩНИКИ =====================
def fmt_dt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

# ===================== КОМАНДЫ =====================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет, я твой личный ассистент. Я помогу тебе оптимизировать все твои рутинные задачи, "
        "чтобы ты сосредоточился на самом главном и ничего не забыл.\n\n"
        "Этот бот приватный. Введите приватный ключ в формате ABC123.\n\n"
        "Примеры:\n"
        "• через 2 минуты / через 5 минут — поесть\n"
        "• сегодня в 18:30 — попить воды\n"
        "• завтра в 09:00 — сходить в зал\n"
        "• каждый день в 07:45 — чистить зубы\n"
        "• 30 августа в 10:00 — оплатить кредит\n\n"
        "❗ Если встреча в 15:00, а напоминание нужно за час — поставь напоминание на 14:00."
    )

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tasks = list_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("Пока дел нет.")
        return

    now = datetime.now(TZ)

    def next_run(t: Task) -> datetime:
        if t.type == "once" and t.run_at_utc:
            return t.run_at_utc.astimezone(TZ)
        if t.type == "daily":
            return compute_next_for_daily(t.hour, t.minute, now)
        return compute_next_for_monthly(t.day_of_month, t.hour, t.minute, now)

    sorted_tasks = sorted(tasks, key=next_run)[:20]
    LAST_LIST[chat_id] = [t.id for t in sorted_tasks]

    lines = []
    for i, t in enumerate(sorted_tasks, 1):
        if t.type == "once":
            when = fmt_dt_local(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {when}")

    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /affairs_delete <номер>")
        return
    idx = int(ctx.args[0])
    tasks = list_tasks(chat_id)
    if not tasks or idx < 1 or idx > len(tasks):
        await update.message.reply_text("Нет задачи с таким номером.")
        return
    t = tasks[idx - 1]
    delete_task(t.id)
    await update.message.reply_text(f"🗑 Удалено: «{t.title}».")

async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    MAINTENANCE = True
    await update.message.reply_text("🟡 Технические работы включены.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    MAINTENANCE = False
    await update.message.reply_text("🟢 Технические работы выключены.")

# ===================== ТЕКСТЫ =====================
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Техработы
    if MAINTENANCE and update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ Бот на технических работах. Попробуй позже.")
        return

    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # 1) Авторизация: проверяем ключ ТОЛЬКО если пользователь еще не авторизован
    if not AUTHORIZED.get(chat_id, False):
        key = re.sub(r"\s+", "", text).upper()
        if key in ACCESS_KEYS:
            AUTHORIZED[chat_id] = True
            await update.message.reply_text("✅ Доступ подтверждён! Теперь можешь добавлять дела и использовать команду «/affairs».")
        else:
            await update.message.reply_text("❌ Неверный ключ. Введите ключ в формате ABC123 (например, VIP003).")
        return

    # 2) Удаление через текст: "affairs delete 7"
    m = re.fullmatch(r"(?i)\s*affairs\s+delete\s+(\d+)\s*", text)
    if m:
        idx = int(m.group(1))
        ids = LAST_LIST.get(chat_id)
        if not ids or idx < 1 or idx > len(ids):
            await update.message.reply_text("Сначала открой список /affairs и проверь номер.")
            return
        tid = ids[idx - 1]
        t = get_task(tid)
        if t:
            delete_task(t.id)
            await update.message.reply_text(f"🗑 Удалено: «{t.title}».")
        else:
            await update.message.reply_text("Это дело уже удалено.")
        return

    # 3) Новая задача
    now_tz = datetime.now(TZ)
    parsed = parse_user_text_to_task(text, now_tz)
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
        when = fmt_dt_local(t.run_at_utc)
        await update.message.reply_text(f"Отлично, напомню: «{t.title}» — {when}")
    elif t.type == "daily":
        await update.message.reply_text(f"Отлично, напомню: каждый день в {t.hour:02d}:{t.minute:02d} — «{t.title}»")
    else:
        await update.message.reply_text(f"Отлично, напомню: каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d} — «{t.title}»")

# ===================== MAIN =====================
def main():
    init_db()app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))

    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_startup(app_: Application):
        # убираем любой старый вебхук, чтобы polling не конфликтовал на Render
        await app_.bot.delete_webhook(drop_pending_updates=True)
        await reschedule_all(app_)
        log.info("Bot started. Timezone=%s", TZ)

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
