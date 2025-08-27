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
BOT_TOKEN = "8492146866:AAE6yWRhg1wa9qn7_PV3NRJS6lh1dFtjxqA"
ADMIN_ID = 963586834
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
        existing = {r[0] for r in conn.execute("SELECT key FROM access_keys")}
        to_add = [(f"VIP{i:03d}",) for i in range(1, 101) if f"VIP{i:03d}" not in existing]
        if to_add:
            conn.executemany("INSERT INTO access_keys(key) VALUES(?)", to_add)
        conn.commit()

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

# -------------------- РЕГУЛЯРКИ ДЛЯ ПАРСИНГА --------------------
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
RELATIVE_RE = re.compile(
    r"^\s*через\s+(\d+)\s*"
    r"(?:секунд(?:у|ы)?|сек|с|"
    r"минут(?:у|ы)?|мин|м|"
    r"час(?:а|ов)?|ч)"
    r"\s+(.+)$",
    re.I
)
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

# -------------------- ПАРСЕР --------------------
def parse_user_text_to_task(text: str, now_tz: datetime) -> Optional[ParsedTask]:
    text = text.strip()

    m = RELATIVE_RE.match(text)
    if m:
        amount = int(m.group(1))
        title = m.group(2).strip()
        unit_text = text.lower()
        if "сек" in unit_text:
            delta = timedelta(seconds=amount)
        elif "мин" in unit_text:
            delta = timedelta(minutes=amount)
        elif "час" in unit_text:
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

# -------------------- ЗАДАЧИ, AFFAIRS, job_queue и др. --------------------
# ⚠️ Тут код длинный: Task dataclass, add_task/get_task/cancel_task/list_active_tasks,
# функции schedule_task, reschedule_all, job_fire, job_fire_monthly,
# handle_text (с try/except и сообщением ⚠️ Упс), affairs_cmd, admin-команды и main().

# -------------------- MAIN --------------------
def main():
    start_health_server()
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))
    app.add_handler(CommandHandler("keys", keys_all_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_startup(app_: Application):
        await reschedule_all(app_)
        log.info("Bot started. Timezone=%s", TZ)

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
