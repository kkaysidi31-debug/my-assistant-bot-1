# -*- coding: utf-8 -*-
"""
Минимальный, но полный Telegram-бот-ассистент под Render.

Есть:
- Приватный доступ по ключам VIP001..VIP100 (хранение в SQLite).
- Авторизация пользователей (переживает перезапуск).
- Парсинг естественных фраз:
  • «через 30 секунд поесть», «через 5 минут позвонить», «через 2 часа …»
  • «сегодня в 18:30 …»
  • «завтра в 09:00 …»
  • «каждый день в 07:45 …»
  • «30 августа в 10:00 …» или «30.08 в 10:00 …»
- Напоминания вовремя (JobQueue), восстановление после перезапуска.
- /affairs — список ближайших дел (до 20)
- Удаление по номеру: «affairs delete 3» или /affairs_delete 3
- Ключи для админа: /keys_left (сколько осталось)
- Healthcheck-порт для Render (пинг).

Готов к деплою:
- requirements.txt: python-telegram-bot==20.6 (обязательно)
- runtime.txt: python-3.11.6 (рекомендую)
- Start Command: python3 main.py
- Build Command: python3 -m pip install --upgrade pip && python3 -m pip install -r requirements.txt
"""

import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ====== НАСТРОЙКИ ======
BOT_TOKEN = "8492146866:AAE6yWRhg1wa9qn7_PV3NRJS6lh1dFtjxqA"  # твой токен
ADMIN_ID = 963586834                                   # твой Telegram ID
TZ = ZoneInfo("Europe/Kaliningrad")                    # часовой пояс
DB_PATH = "assistant_min.db"                           # файл БД

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("assistant-min")

# ====== Healthcheck для Render ======
class _Health(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): return
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

def start_health():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# ====== БАЗА ДАННЫХ ======
def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY,
            is_auth INTEGER NOT NULL DEFAULT 0,
            key_used TEXT,
            authorized_at_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS access_keys(
            key TEXT PRIMARY KEY,
            used_by_chat_id INTEGER,
            used_at_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('once','daily','monthly')),
            run_at_utc TEXT,
            hour INTEGER,
            minute INTEGER,
            day_of_month INTEGER
        );
        """)
        # Заполнить ключи VIP001..VIP100, если их нет
        have = {r[0] for r in conn.execute("SELECT key FROM access_keys")}
        to_add = [(f"VIP{i:03d}",) for i in range(1, 101) if f"VIP{i:03d}" not in have]
        if to_add:
            conn.executemany("INSERT INTO access_keys(key) VALUES(?)", to_add)
        conn.commit()

def is_auth(chat_id: int) -> bool:
    with db() as conn:
        r = conn.execute("SELECT is_auth FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r[0])

def try_key(chat_id: int, text: str) -> bool:
    key = re.sub(r"\s+", "", text).upper()
    if not re.fullmatch(r"VIP\d{3}", key):
        return False
    with db() as conn:
        r = conn.execute("SELECT key, used_by_chat_id FROM access_keys WHERE key=?", (key,)).fetchone()
        if not r: return False
        if r[1] and r[1] != chat_id: return False
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO users(chat_id,is_auth,key_used,authorized_at_utc) VALUES(?,?,?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET is_auth=excluded.is_auth, key_used=excluded.key_used, authorized_at_utc=excluded.authorized_at_utc",
                     (chat_id, 1, key, now))
        conn.execute("UPDATE access_keys SET used_by_chat_id=?, used_at_utc=? WHERE key=?", (chat_id, now, key))
        conn.commit()
        return True

def keys_left() -> int:
    with db() as conn:
        r = conn.execute("SELECT COUNT(*) FROM access_keys WHERE used_by_chat_id IS NULL").fetchone()
        return int(r[0]) if r else 0

# ====== МОДЕЛЬ ЗАДАЧ ======
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

def row_to_task(row: Tuple) -> Task:
    return Task(row[0], row[1], row[2], row[3],
                datetime.fromisoformat(row[4]) if row[4] else None,
                row[5], row[6], row[7])

def add_task(chat_id: int, title: str, ttype: str,
             run_at_utc: Optional[datetime], hour: Optional[int],
             minute: Optional[int], day_of_month: Optional[int]) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month) VALUES(?,?,?,?,?,?,?)",
            (chat_id, title, ttype, run_at_utc.isoformat() if run_at_utc else None,
             hour, minute, day_of_month)
        )
        conn.commit()
        return cur.lastrowid

def get_task(task_id: int) -> Optional[Task]:
    with db() as conn:
        r = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE id=?",
                         (task_id,)).fetchone()
        return row_to_task(r) if r else None

def list_tasks(chat_id: int) -> List[Task]:
    with db() as conn:
        rows = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE chat_id=?",
                            (chat_id,)).fetchall()
        return [row_to_task(r) for r in rows]

def delete_task(task_id: int):
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()

# ====== ПАРСЕР ======
MONTHS = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
          "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}

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
class Parsed:
    type: str
    title: str
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def parse_text(text: str, now: datetime) -> Optional[Parsed]:
    text = text.strip()

    m = RELATIVE_RE.match(text)
    if m:
        amount = int(m.group(1)); unit = m.group(2).lower(); title = m.group(3).strip()
        if unit.startswith("сек") or unit == "с": delta = timedelta(seconds=amount)
        elif unit.startswith("мин") or unit == "м": delta = timedelta(minutes=amount)
        else: delta = timedelta(hours=amount)
        run_local = now + delta
        return Parsed("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TODAY_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now: run_local += timedelta(days=1)
        return Parsed("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TOMORROW_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return Parsed("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DAILY_RE.match(text)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        return Parsed("daily", title, None, h, mi, None)

    m = DATE_NUM_RE.match(text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or now.year)
        h = int(m.group(4) or 10); mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now and not m.group(3):
            run_local = datetime(y+1, mo, d, h, mi, tzinfo=TZ)
        return Parsed("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DATE_TXT_RE.match(text)
    if m:
        d = int(m.group(1)); mon = m.group(2).lower()
        if mon not in MONTHS: return None
        y = int(m.group(3) or now.year)
        h = int(m.group(4) or 10); mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        mo = MONTHS[mon]
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now and not m.group(3):
            run_local = datetime(y+1, mo, d, h, mi, tzinfo=TZ)
        return Parsed("once", title, run_local.astimezone(timezone.utc), None, None, None)

    return None

# ====== ПЛАНИРОВЩИК ======
def fmt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    tid = ctx.job.data["task_id"]
    t = get_task(tid)
    if t:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def schedule(app: Application, t: Task):
    jq = app.job_queue
    # снять старые джобы с таким именем (если пере-планируем)
    for j in jq.get_jobs_by_name(f"task_{t.id}"):
        j.schedule_removal()

    if t.type == "once" and t.run_at_utc and t.run_at_utc > datetime.now(timezone.utc):
        jq.run_once(job_once, when=t.run_at_utc, name=f"task_{t.id}", data={"task_id": t.id})
    elif t.type == "daily":
        jq.run_daily(job_once, time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                     name=f"task_{t.id}", data={"task_id": t.id})
    elif t.type == "monthly":
        # просто «проверяем число» каждый день в указанное время
        async def monthly_check(ctx: ContextTypes.DEFAULT_TYPE):
            task = get_task(ctx.job.data["task_id"])
            if task and datetime.now(TZ).day == task.day_of_month:
                await ctx.bot.send_message(task.chat_id, f"🔔 Напоминание: {task.title}")
        jq.run_daily(monthly_check, time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                     name=f"task_{t.id}", data={"task_id": t.id})

async def reschedule_all(app: Application):
    with db() as conn:
        rows = conn.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks").fetchall()
    for r in rows:
        await schedule(app, row_to_task(r))

# ====== КОМАНДЫ ======
LAST_LIST = {}  # chat_id -> [task_ids]

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Привет, я твой личный ассистент. Я помогу тебе оптимизировать рутину, "
        "чтобы ты сфокусировался на главном и ничего не забыл.\n\n"
        "Этот бот приватный. Введите приватный ключ в формате ABC123 (например, VIP003).\n\n"
        "Примеры:\n"
        "• через 2 минуты поесть\n"
        "• сегодня в 18:30 попить воды\n"
        "• завтра в 09:00 сходить в зал\n"
        "• каждый день в 07:45 чистить зубы\n"
        "• 30 августа в 10:00 оплатить кредит\n\n"
        "❗ Нужно напомнить за N минут до встречи? Поставь напоминание на время N минут раньше.")
    await update.message.reply_text(msg)

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not (is_auth(chat_id) or update.effective_user.id == ADMIN_ID):
        await update.message.reply_text("Этот бот приватный. Введи ключ (например, VIP003).")
        return

    tasks = list_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("У тебя пока нет дел.")
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
            when = fmt_local(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {when}")

    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not (is_auth(chat_id) or update.effective_user.id == ADMIN_ID):
        await update.message.reply_text("Этот бот приватный. Введи ключ (например, VIP003).")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /affairs_delete <номер>")
        return
    idx = int(ctx.args[0])
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

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Свободных ключей: {keys_left()} из 100.")

# ====== ТЕКСТ ======
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Авторизация
    if not is_auth(chat_id) and update.effective_user.id != ADMIN_ID:
        if try_key(chat_id, text):
            await update.message.reply_text("✅ Доступ подтверждён! Теперь можешь добавлять дела и использовать /affairs.")
        else:
            await update.message.reply_text("❌ Неверный ключ. Введи ключ формата VIPxxx (например, VIP003).")
        return

    # Удаление через текст: "affairs delete 5"
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

    # Добавление новой задачи
    now = datetime.now(TZ)
    p = parse_text(text, now)
    if not p:
        await update.message.reply_text("⚠ Не понял задачу. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    task_id = add_task(chat_id, p.title, p.type, p.run_at_utc, p.hour, p.minute, p.day_of_month)
    t = get_task(task_id)
    await schedule(ctx.application, t)

    if t.type == "once":
        await update.message.reply_text(f"Отлично, напомню: «{t.title}» — {fmt_local(t.run_at_utc)}")
    elif t.type == "daily":
        await update.message.reply_text(f"Отлично, напомню: каждый день в {t.hour:02d}:{t.minute:02d} — «{t.title}»")
    else:
        await update.message.reply_text(f"Отлично, напомню: каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d} — «{t.title}»")

# ====== MAIN ======
def main():
    start_health()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_start(app_: Application):
        await app_.bot.delete_webhook(drop_pending_updates=True)
        await reschedule_all(app_)
        import telegram, sys
        log.info("Bot started. TZ=%s | PTB=%s | Python=%s",
                 TZ, getattr(telegram, '__version__', 'unknown'), sys.version.split()[0])

    app.post_init = on_start
    app.run_polling()

if __name__ == "__main__":
    main()
        
