import os
import re
import sqlite3
import string
import random
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional, List, Tuple

import pytz
from aiohttp import web

from telegram import Update, BotCommand
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ====================== НАСТРОЙКИ ======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
TZ_NAME = os.environ.get("TZ", "Europe/Kaliningrad")
TZ = pytz.timezone(TZ_NAME)

DB_PATH = "bot.db"

WELCOME_TEXT = (
    "Привет, я твой личный ассистент. Помогу оптимизировать рутинные задачи, "
    "чтобы ты ничего не забыл.\n\n"
    "Примеры:\n"
    "• через 2 минуты поесть / через 30 секунд позвонить\n"
    "• сегодня в 18:30 попить воды\n"
    "• завтра в 09:00 сходить в зал\n"
    "• каждый день в 07:45 чистить зубы\n"
    "• 30 августа в 10:00 оплатить кредит\n\n"
    "❗ Напоминание «за N минут»: просто поставь время на N минут раньше."
)

# ====================== УТИЛИТЫ ======================

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db() -> None:
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users(
              chat_id INTEGER PRIMARY KEY,
              authed  INTEGER DEFAULT 0
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              title TEXT NOT NULL,
              type TEXT NOT NULL, -- once|daily|monthly
              run_at_utc TEXT,    -- ISO для once
              hour INTEGER,
              minute INTEGER,
              day_of_month INTEGER,
              active INTEGER DEFAULT 1
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS keys(
              key TEXT PRIMARY KEY,
              issued INTEGER DEFAULT 0,   -- выдан админу (зарезервирован)
              used_by INTEGER             -- chat_id, кто активировал
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings(
              name TEXT PRIMARY KEY,
              value TEXT
            );
        """)
        # флаг техработ
        c.execute("INSERT OR IGNORE INTO settings(name,value) VALUES('maintenance','0');")
    logging.info("DB ready")

# ---- Ключи доступа ----

ALPHABET = string.ascii_letters + string.digits  # A..Z a..z 0..9

def random_key(n: int = 5) -> str:
    return "".join(random.choice(ALPHABET) for _ in range(n))

def ensure_keys_pool(target_total: int = 1000) -> None:
    """Генерим пул из 1000 уникальных ключей по 5 символов (если не хватает)."""
    with db() as c:
        cur = c.execute("SELECT COUNT(*) AS cnt FROM keys")
        have = cur.fetchone()["cnt"]
        need = max(0, target_total - have)
        if need == 0:
            return
        batch = set()
        while len(batch) < need:
            k = random_key(5)
            batch.add(k)
        c.executemany("INSERT OR IGNORE INTO keys(key) VALUES(?)", [(k,) for k in batch])
    logging.info("Keys pool ensured. total=%s", target_total)

def keys_left() -> int:
    with db() as c:
        # свободные == не выданы и не использованы
        cur = c.execute("SELECT COUNT(*) AS cnt FROM keys WHERE issued=0 AND used_by IS NULL")
        return cur.fetchone()["cnt"]

def keys_free() -> int:
    with db() as c:
        cur = c.execute("SELECT COUNT(*) AS cnt FROM keys WHERE used_by IS NULL")
        return cur.fetchone()["cnt"]

def keys_used() -> int:
    with db() as c:
        cur = c.execute("SELECT COUNT(*) AS cnt FROM keys WHERE used_by IS NOT NULL")
        return cur.fetchone()["cnt"]

def issue_random_key() -> Optional[str]:"""Админ запрашивает ключ — помечаем как issued=1 и возвращаем"""
    with db() as c:
        row = c.execute("SELECT key FROM keys WHERE issued=0 AND used_by IS NULL LIMIT 1" ).fetchone()
        if not row:
            return None
        k = row["key"]
        c.execute("UPDATE keys SET issued=1 WHERE key=?", (k,))
        return k

def keys_reset_all() -> None:
    with db() as c:
        c.execute("UPDATE keys SET issued=0, used_by=NULL")

def use_key(chat_id: int, key: str) -> bool:
    """Пользователь прислал ключ. Разрешаем только выданные (issued=1) и неиспользованные."""
    with db() as c:
        row = c.execute(
            "SELECT key, issued, used_by FROM keys WHERE key=?",
            (key,)
        ).fetchone()
        if not row:
            return False
        if row["used_by"] is not None:
            return False
        if int(row["issued"]) != 1:
            return False
        c.execute("UPDATE keys SET used_by=? WHERE key=?", (chat_id, key))
        c.execute("INSERT OR IGNORE INTO users(chat_id, authed) VALUES(?,1)", (chat_id,))
        c.execute("UPDATE users SET authed=1 WHERE chat_id=?", (chat_id,))
        return True

def is_authed(chat_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT authed FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row["authed"])

# ---- Техработы ----

def maintenance_on() -> None:
    with db() as c:
        c.execute("UPDATE settings SET value='1' WHERE name='maintenance'")

def maintenance_off() -> None:
    with db() as c:
        c.execute("UPDATE settings SET value='0' WHERE name='maintenance'")

def is_maintenance() -> bool:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE name='maintenance'").fetchone()
        return (row and row["value"] == "1")

# ====================== ПАРСИНГ ТЕКСТА ======================

@dataclass
class Task:
    chat_id: int
    title: str
    type: str                     # once|daily|monthly
    run_at_utc: Optional[datetime] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    day_of_month: Optional[int] = None

def to_utc(dt_local: datetime) -> datetime:
    return dt_local.astimezone(timezone.utc)

def parse(text: str, chat_id: int) -> Optional[Task]:
    text = text.strip().lower()

    # через N секунд/минут/часов
    m = re.fullmatch(r"через\s+(\d+)\s*(секунд(?:ы)?|сек|минут(?:ы)?|мин|час(?:а|ов)?)\s+(.+)", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        title = m.group(3).strip()
        now = datetime.now(TZ)
        if unit.startswith("сек"):
            run_local = now + timedelta(seconds=n)
        elif unit.startswith("мин"):
            run_local = now + timedelta(minutes=n)
        else:
            run_local = now + timedelta(hours=n)
        return Task(chat_id, title, "once", run_at_utc=to_utc(run_local))
    # сегодня в HH:MM ...
    m = re.fullmatch(r"сегодня\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", text)
    if m:
        h, mnt = int(m.group(1)), int(m.group(2))
        title = m.group(3).strip()
        now = datetime.now(TZ)
        run_local = datetime(now.year, now.month, now.day, h, mnt, tzinfo=TZ)
        if run_local < now:
            run_local += timedelta(days=1)
        return Task(chat_id, title, "once", run_at_utc=to_utc(run_local))
    # завтра в HH:MM ...
    m = re.fullmatch(r"завтра\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", text)
    if m:
        h, mnt = int(m.group(1)), int(m.group(2))
        title = m.group(3).strip()
        now = datetime.now(TZ)
        run_local = datetime(now.year, now.month, now.day, h, mnt, tzinfo=TZ) + timedelta(days=1)
        return Task(chat_id, title, "once", run_at_utc=to_utc(run_local))
    # каждый день в HH:MM ...
    m = re.fullmatch(r"каждый\s+день\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", text)
    if m:
        h, mnt = int(m.group(1)), int(m.group(2))
        title = m.group(3).strip()
        return Task(chat_id, title, "daily", hour=h, minute=mnt)
    # DD.MM.YYYY HH:MM ...
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s+(.+)", text)
    if m:
        d, mon, y, h, mnt = map(int, m.groups()[:5])
        title = m.group(6).strip()
        run_local = datetime(y, mon, d, h, mnt, tzinfo=TZ)
        return Task(chat_id, title, "once", run_at_utc=to_utc(run_local))

    return None

# ====================== ПЛАНИРОВАНИЕ ======================

def fmt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

async def job_once(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = ctx.job.data or {}
    task_id = job_data.get("task_id")
    t = get_task(task_id)
    if not t:
        return
    await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")
    # деактивируем
    with db() as c:
        c.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))

async def job_daily(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = ctx.job.data or {}
    task_id = job_data.get("task_id")
    t = get_task(task_id)
    if not t:
        return
    await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")

def get_task(task_id: int) -> Optional[Task]:
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not r:
            return None
        return Task(
            chat_id=r["chat_id"], title=r["title"], type=r["type"],
            run_at_utc=datetime.fromisoformat(r["run_at_utc"]) if r["run_at_utc"] else None,
            hour=r["hour"], minute=r["minute"], day_of_month=r["day_of_month"]
        )

def save_task(t: Task) -> int:
    with db() as c:
        c.execute("""
            INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month,active)
            VALUES(?,?,?,?,?,?,?,1)
        """, (
            t.chat_id, t.title, t.type,
            t.run_at_utc.isoformat() if t.run_at_utc else None,
            t.hour, t.minute, t.day_of_month
        ))
        return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

async def schedule_task(app: Application, task_id: int) -> None:
    t = get_task(task_id)
    if not t or not hasattr(app, "job_queue"):
        return
    jq = app.job_queue
    # удалим старые джобы с этим task_id
    for j in jq.get_jobs_by_name(f"task-{task_id}"):
        j.schedule_removal()

    if t.type == "once" and t.run_at_utc:
        when = t.run_at_utc
        if when < datetime.now(timezone.utc):
            # просрочено — отключаем
            with db() as c:
                c.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
            return
        jq.run_once(job_once, when=when, name=f"task-{task_id}", data={"task_id": task_id})
    elif t.type == "daily" and t.hour is not None and t.minute is not None:
        jq.run_daily(
            job_daily,
            time=dtime(hour=t.hour, minute=t.minute, tzinfo=TZ),
            name=f"task-{task_id}",
            data={"task_id": task_id}
        )

async def reschedule_all(app: Application) -> None:
    with db() as c:
        rows = c.execute("SELECT id FROM tasks WHERE active=1").fetchall()
    for r in rows:
        await schedule_task(app, r["id"])

# ====================== ВЕБ-СЕРВЕР ДЛЯ ПИНГОВ ======================

async def handle_alive(_request):
    return web.Response(text="alive")

async def run_web():
    app = web.Application()
    app.add_routes([web.get("/", handle_alive)])
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Web keepalive on :%s", port)

# ====================== ХЕНДЛЕРЫ ======================

def user_is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authed(chat_id):
        await update.message.reply_text("Этот бот приватный. Введите ключ доступа.")
        return
    # уже авторизован
    await update.message.reply_text("✅ Ключ принят.")
    await update.message.reply_text(WELCOME_TEXT)

async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    maintenance_on()
    await update.message.reply_text("🚧 Техработы включены.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    maintenance_off()
    await update.message.reply_text("✅ Техработы выключены.")

async def issue_key_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    k = issue_random_key()
    if not k:
        await update.message.reply_text("Ключи закончились.")
    else:
        await update.message.reply_text(f"🔑 Твой ключ: `{k}`", parse_mode="Markdown")

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    await update.message.reply_text(str(keys_left()))

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    await update.message.reply_text(str(keys_free()))

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    await update.message.reply_text(str(keys_used()))

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_is_admin(update):
        return
    keys_reset_all()
    await update.message.reply_text("Ключи сброшены.")

# Список дел
async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as c:
        rows = c.execute(
            "SELECT id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks "
            "WHERE chat_id=? AND active=1 ORDER BY id", (chat_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("Пока дел нет.")
        return
    lines = ["Твои дела:"]
    for i, r in enumerate(rows, start=1):
        if r["type"] == "once":
            dt = fmt_local(datetime.fromisoformat(r["run_at_utc"]))
            lines.append(f"{i}. {r['title']} — {dt}")
        elif r["type"] == "daily":
            lines.append(f"{i}. {r['title']} — каждый день в {r['hour']:02d}:{r['minute']:02d}")
        else:
            lines.append(f"{i}. {r['title']}")
    await update.message.reply_text("\n".join(lines))

# /affairs_delete 3
async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Укажи номер: /affairs_delete 3")
        return
    try:
        idx = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Номер должен быть числом.")
        return

    with db() as c:
        rows = c.execute(
            "SELECT id FROM tasks WHERE chat_id=? AND active=1 ORDER BY id",
            (chat_id,)
        ).fetchall()
    if idx < 1 or idx > len(rows):
        await update.message.reply_text("Нет дела с таким номером.")
        return
    task_id = rows[idx - 1]["id"]
    with db() as c:
        c.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
    # снять из job_queue
    app: Application = ctx.application
    for j in app.job_queue.get_jobs_by_name(f"task-{task_id}"):
        j.schedule_removal()
    await update.message.reply_text("✅ Удалено.")

# Текст: ключ или создание напоминания
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # техработы блокируют всех, кроме админа
    if is_maintenance() and not user_is_admin(update):
        await update.message.reply_text("🚧 Сейчас техработы. Попробуй позже.")
        return

    # если не авторизован — пробуем принять ключ
    if not is_authed(chat_id):
        if use_key(chat_id, text):
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text(WELCOME_TEXT)
        else:
            await update.message.reply_text("❌ Неверный ключ доступа.")
        return

    # авторизован — парсим задачу
    t = parse(text, chat_id)
    if not t:
        await update.message.reply_text("⚠️ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    task_id = save_task(t)
    await schedule_task(ctx.application, task_id)

    if t.type == "once" and t.run_at_utc:
        await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — {fmt_local(t.run_at_utc)}")
    elif t.type == "daily":
        await update.message.reply_text(f"✅ Отлично, напомню каждый день в {t.hour:02d}:{t.minute:02d}: «{t.title}»")
    else:
        await update.message.reply_text("✅ Напоминание создано.")

# ====================== СТАРТ/ПЕРЕЗАПУСК ======================

async def on_startup(app: Application):
    # удалим старый webhook на всякий случай, чтобы polling не конфликтовал
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)
    # перепланируем все задачи из БД
    try:
        await reschedule_all(app)
    except Exception:
        logging.exception("reschedule_all failed")
    # выставим команды меню
    try:
        cmds = [
            BotCommand("start", "Помощь и примеры"),
            BotCommand("affairs", "Список дел"),
            BotCommand("affairs_delete", "Удалить дело по номеру"),
            BotCommand("maintenance_on", "Техработы: включить (только админ)"),
            BotCommand("maintenance_off", "Техработы: выключить (только админ)"),
            BotCommand("issue_key", "Выдать ключ (только админ)"),
            BotCommand("keys_left", "Свободных ключей (только админ)"),
            BotCommand("keys_free", "Неиспользованные + выданные (только админ)"),
            BotCommand("keys_used", "Использованные (только админ)"),
            BotCommand("keys_reset", "Сбросить все ключи (только админ)")
        ]
        await app.bot.set_my_commands(cmds)
    except Exception as e:
        logging.warning("set_my_commands failed: %s", e)

# ====================== MAIN ======================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render -> Environment.")

    init_db()
    ensure_keys_pool(1000)

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))

    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))

    app.add_handler(CommandHandler("issue_key", issue_key_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))
    app.add_handler(CommandHandler("keys_free", keys_free_cmd))
    app.add_handler(CommandHandler("keys_used", keys_used_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    # текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = on_startup  # вызовется перед run_polling

    # запускаем одновременно polling и веб-сервер для UptimeRobot
    await run_web()
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
