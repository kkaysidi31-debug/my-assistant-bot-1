# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional, List, Tuple
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = "8492146866:AAHR_lrK9o18dGI0-ngfkVZUhbPQ4YSmr48"
ADMIN_ID = 963586834
TZ = ZoneInfo("Europe/Kaliningrad")
DB_FILE = "assistant.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("assistant-bot")

# ===================== HEALTHCHECK =======================
class Health(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):  # тишина в логах
        pass
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Health)
    Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Health server on :%s", port)

# ===================== БАЗА ДАННЫХ =======================
def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          chat_id INTEGER PRIMARY KEY,
          is_auth INTEGER NOT NULL DEFAULT 0,
          key_used TEXT
        );
        CREATE TABLE IF NOT EXISTS access_keys(
          key TEXT PRIMARY KEY,
          used_by_chat_id INTEGER
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
        # VIP001..VIP100 — добавим недостающие
        have = {r[0] for r in c.execute("SELECT key FROM access_keys")}
        for i in range(1, 101):
            key = f"VIP{i:03d}"
            if key not in have:
                c.execute("INSERT INTO access_keys(key) VALUES(?)", (key,))
        c.commit()

def is_auth(chat_id: int) -> bool:
    with db() as c:
        r = c.execute("SELECT is_auth FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r[0])

def try_use_key(chat_id: int, text: str) -> bool:
    key = re.sub(r"\s+", "", text).upper()
    if not re.fullmatch(r"VIP\d{3}", key):
        return False
    with db() as c:
        row = c.execute("SELECT used_by_chat_id FROM access_keys WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        used_by = row[0]
        if used_by and used_by != chat_id:
            return False
        c.execute(
            "INSERT INTO users(chat_id,is_auth,key_used) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET is_auth=excluded.is_auth, key_used=excluded.key_used",
            (chat_id, 1, key)
        )
        c.execute("UPDATE access_keys SET used_by_chat_id=? WHERE key=?", (chat_id, key))
        c.commit()
        return True

def keys_left_count() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM access_keys WHERE used_by_chat_id IS NULL").fetchone()[0]

@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str  # once / daily / monthly
    run_at_utc: Optional[datetime]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]

def row_to_task(r: Tuple) -> Task:
    return Task(
        r[0], r[1], r[2], r[3],
        datetime.fromisoformat(r[4]) if r[4] else None,
        r[5], r[6], r[7]
    )

def add_task(chat_id: int, title: str, typ: str, run_at_utc: Optional[datetime], h: Optional[int],
             m: Optional[int], d: Optional[int]) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month) "
            "VALUES(?,?,?,?,?,?,?)",
            (chat_id, title, typ, run_at_utc.isoformat() if run_at_utc else None, h, m, d)
        )
        c.commit()
        return cur.lastrowid

def get_task(tid: int) -> Optional[Task]:
    with db() as c:
        r = c.execute(
            "SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE id=?",
            (tid,)
        ).fetchone()
        return row_to_task(r) if r else None

def list_tasks(chat_id: int) -> List[Task]:
    with db() as c:
        rows = c.execute(
            "SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE chat_id=?",
            (chat_id,)
        ).fetchall()
        return [row_to_task(r) for r in rows]

def delete_task(tid: int):
    with db() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        c.commit()

# ====================== ПАРСИНГ =========================
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12
}

REL_RE = re.compile(r"^\s*через\s+(\d+)\s*(сек(?:унд(?:у|ы)?)?|с|мин(?:ут(?:у|ы)?)?|м|час(?:а|ов)?|ч)\s+(.+)$", re.I)
TODAY_RE = re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
TOMORROW_RE = re.compile(r"^\s*завтра\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DAILY_RE = re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DMY_NUM_RE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)
DMY_TXT_RE = re.compile(r"^\s*(\d{1,2})\s+([а-яА-Я]+)(?:\s+(\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)

@dataclass
class ParsedTask:
    type: str
    title: str
    run_utc: Optional[datetime]
    h: Optional[int]
    m: Optional[int]
    d: Optional[int]

def parse_user_text_to_task(text: str, now_tz: datetime) -> Optional[ParsedTask]:
    t = text.strip()

    m = REL_RE.match(t)
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
        # маленький сдвиг, чтобы точно было в будущем
        run_local = run_local + timedelta(seconds=1)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TODAY_RE.match(t)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz:
            run_local = run_local + timedelta(days=1)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TOMORROW_RE.match(t)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = (now_tz + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DAILY_RE.match(t)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        return ParsedTask("daily", title, None, h, mi, None)

    m = DMY_NUM_RE.match(t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or now_tz.year)
        h = int(m.group(4) or 10)
        mi = int(m.group(5) or 0)
        title = m.group(6).strip()
        run_local = datetime(y, mo, d, h, mi, tzinfo=TZ)
        if run_local <= now_tz and not m.group(3):
            run_local = datetime(y + 1, mo, d, h, mi, tzinfo=TZ)
            return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DMY_TXT_RE.match(t)
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

# ===================== ПЛАНИРОВЩИК ======================
def fmt_local(utc_dt: datetime) -> str:
    return utc_dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    t = get_task(ctx.job.data["id"])
    if t:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def schedule_task(app: Application, t: Task):
    """Безопасно (без исключений) перепланируем задачу."""
    try:
        jq = app.job_queue
        for j in jq.get_jobs_by_name(f"task_{t.id}"):
            j.schedule_removal()

        if t.type == "once":
            if not t.run_at_utc:
                return
            # если вдруг просрочено — сдвигаем на +15 секунд от текущего
            now_utc = datetime.now(timezone.utc)
            when = t.run_at_utc
            if when <= now_utc:
                when = now_utc + timedelta(seconds=15)
            jq.run_once(job_once, when=when, name=f"task_{t.id}", data={"id": t.id})
        elif t.type == "daily":
            jq.run_daily(
                job_once,
                time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                name=f"task_{t.id}", data={"id": t.id}
            )
        elif t.type == "monthly":
            async def monthly_fire(ctx: ContextTypes.DEFAULT_TYPE):
                tt = get_task(ctx.job.data["id"])
                if tt and datetime.now(TZ).day == tt.day_of_month:
                    await ctx.bot.send_message(tt.chat_id, f"🔔 Напоминание: {tt.title}")
            jq.run_daily(
                monthly_fire,
                time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
                name=f"task_{t.id}", data={"id": t.id}
            )
    except Exception:
        log.exception("schedule_task failed")

async def reschedule_all(app: Application):
    with db() as c:
        rows = c.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks").fetchall()
    for r in rows:
        await schedule_task(app, row_to_task(r))

# ===================== КОМАНДЫ ==========================
LAST_LIST_INDEX: dict[int, List[int]] = {}

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

START_PROMPT = "Этот бот приватный. Введите ключ доступа в формате ABC123."

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_PROMPT)

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Свободных ключей: {keys_left_count()} из 100.")

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not (is_auth(chat_id) or update.effective_user.id == ADMIN_ID):
        await update.message.reply_text(START_PROMPT)
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
            if cand <= now:
                cand = cand + timedelta(days=1)
            return cand
        # monthly
        y, m = now.year, now.month
        for _ in range(24):
            try:
                cand = datetime(y, m, t.day_of_month, t.hour, t.minute, tzinfo=TZ)
                if cand > now:
                    return cand
            except ValueError:
                pass
            m = 1 if m == 12 else m + 1
            if m == 1:
                y += 1
        return now + timedelta(days=30)

    tasks_sorted = sorted(tasks, key=next_run)[:20]
    LAST_LIST_INDEX[chat_id] = [t.id for t in tasks_sorted]

    lines = []
    for i, t in enumerate(tasks_sorted, 1):
        if t.type == "once":
            w = fmt_local(t.run_at_utc)
        elif t.type == "daily":
            w = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            w = f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {w}")

    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not (is_auth(chat_id) or update.effective_user.id == ADMIN_ID):
        await update.message.reply_text(START_PROMPT)
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /affairs_delete <номер> (смотри /affairs)")
        return
    idx = int(ctx.args[0])
    ids = LAST_LIST_INDEX.get(chat_id)
    if not ids or idx < 1 or idx > len(ids):
        await update.message.reply_text("Сначала открой список /affairs и укажи корректный номер.")
        return
    tid = ids[idx - 1]
    t = get_task(tid)
    if t:
        delete_task(t.id)
        await update.message.reply_text(f"🗑 Удалено: «{t.title}»")
    else:
        await update.message.reply_text("Это дело уже удалено.")

# ===================== ТЕКСТ: ДОСТУП + ДОБАВЛЕНИЕ =========
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # ---- Приватный доступ
    if not is_auth(chat_id) and update.effective_user.id != ADMIN_ID:
        if try_use_key(chat_id, text):
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text(WELCOME_TEXT)
        else:
            await update.message.reply_text("❌ Неверный ключ доступа.")
        return

    # ---- Удаление через текст: "affairs delete 3"
    m = re.fullmatch(r"(?i)\s*affairs\s+delete\s+(\d+)\s*", text)
    if m:
        idx = int(m.group(1))
        ids = LAST_LIST_INDEX.get(chat_id)
        if not ids or idx < 1 or idx > len(ids):
            await update.message.reply_text("Сначала открой /affairs.")
            return
        tid = ids[idx - 1]
        t = get_task(tid)
        if t:
            delete_task(t.id)
            await update.message.reply_text(f"🗑 Удалено: «{t.title}»")
        else:
            await update.message.reply_text("Это дело уже удалено.")
        return

    # ---- Добавление напоминания
    now_local = datetime.now(TZ)
    p = parse_user_text_to_task(text, now_local)
    if not p:
        await update.message.reply_text("⚠ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    # Подтверждение пользователю
    if p.type == "once":
        when_str = (p.run_utc or now_local.astimezone(timezone.utc)).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
        confirm = f"Отлично, напомню: «{p.title}» — {when_str}"
    elif p.type == "daily":
        confirm = f"Отлично, напомню: каждый день в {p.h:02d}:{p.m:02d} — «{p.title}»"
    else:
        confirm = f"Отлично, напомню: каждое {p.d} число в {p.h:02d}:{p.m:02d} — «{p.title}»"

    await update.message.reply_text(confirm)

    # Сохраняем и планируем (без падений)
    tid = add_task(chat_id, p.title, p.type, p.run_utc, p.h, p.m, p.d)
    t = get_task(tid)
    await schedule_task(ctx.application, t)

# ========================= MAIN =========================
def main():
    start_health_server()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))

    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_start(app_: Application):
        # убираем webhook, чтобы polling не конфликтовал
        await app_.bot.delete_webhook(drop_pending_updates=True)
        await reschedule_all(app_)
        import telegram, sys
        log.info("Bot started. Timezone=%s | PTB=%s | Python=%s", TZ, getattr(telegram, "__version__", "?"), sys.version)

    app.post_init = on_start
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
