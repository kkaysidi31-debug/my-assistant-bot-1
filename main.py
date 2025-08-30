# -*- coding: utf-8 -*-
import os
import re
import time
import sqlite3
import logging
import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import pytz
import requests
import asyncio
import threading
from aiohttp import web

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================ НАСТРОЙКИ ============================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip() or "0")
TZ = pytz.timezone("Europe/Kaliningrad")  # UTC+2 зимой, +3 летом (как в Калининграде)

DB_PATH = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)

# ============================ БАЗА ============================

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              type TEXT NOT NULL,              -- once | daily
              run_at_utc INTEGER,              -- epoch seconds (для once)
              hh INTEGER,                      -- для daily
              mm INTEGER
            );
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS keys(
              key TEXT PRIMARY KEY,
              issued INTEGER DEFAULT 0,
              used_by INTEGER
            );
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS flags(
              name TEXT PRIMARY KEY,
              val TEXT
            );
            """
        )
    logging.info("DB ready")

def set_flag(name: str, val: str):
    with db() as c:
        c.execute("INSERT INTO flags(name,val) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET val=excluded.val", (name, val))

def get_flag(name: str, default: str = "0") -> str:
    with db() as c:
        row = c.execute("SELECT val FROM flags WHERE name=?", (name,)).fetchone()
        return row["val"] if row else default

# ============================ КЛЮЧИ ============================

ALPHABET = string.ascii_letters + string.digits  # A-Z a-z 0-9

def gen_key() -> str:
    return "".join(random.choice(ALPHABET) for _ in range(5))

def ensure_keys_pool(n: int = 1000):
    """Заполнить пул до n необissued ключей."""
    with db() as c:
        have = c.execute("SELECT COUNT(*) AS cnt FROM keys").fetchone()["cnt"]
        to_add = max(0, n - have)
        if to_add > 0:
            # добавляем только новые (PRIMARY KEY гарантирует уникальность)
            for _ in range(to_add):
                k = gen_key()
                try:
                    c.execute("INSERT INTO keys(key, issued, used_by) VALUES(?, 0, NULL)", (k,))
                except sqlite3.IntegrityError:
                    pass
    logging.info("Keys pool ensured. total~%s", n)

def keys_free() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) AS cnt FROM keys WHERE issued=0 AND used_by IS NULL").fetchone()["cnt"]

def keys_used() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) AS cnt FROM keys WHERE used_by IS NOT NULL").fetchone()["cnt"]

def keys_left() -> int:
    with db() as c:
        # «свободные» считаем как невыданные и неиспользованные
        return c.execute("SELECT COUNT(*) AS cnt FROM keys WHERE issued=0 AND used_by IS NULL").fetchone()["cnt"]

def keys_reset():
    with db() as c:
        c.execute("UPDATE keys SET issued=0, used_by=NULL")

def issue_random_key() -> Optional[str]:
    """Админ запрашивает ключ — помечаем как issued=1 и отдаём."""
    with db() as c:
        row = c.execute("SELECT key FROM keys WHERE issued=0 AND used_by IS NULL LIMIT 1").fetchone()
        if not row:
            return None
        k = row["key"]
        c.execute("UPDATE keys SET issued=1 WHERE key=?", (k,))
        return k

def use_key(chat_id: int, key: str) -> bool:
    """Пользователь применяет ключ. Разрешаем только выданные (issued=1) и неиспользованные."""
    with db() as c:
        row = c.execute("SELECT key, issued, used_by FROM keys WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        if row["used_by"] is not None:
            return False
        if int(row["issued"]) != 1:
            return False
        c.execute("UPDATE keys SET used_by=? WHERE key=?", (chat_id, key))
        return True

def is_auth(chat_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT 1 FROM keys WHERE used_by=?", (chat_id,)).fetchone()
        return bool(row) or chat_id == ADMIN_ID

# ============================ ВЕБХУК reset ============================

def reset_webhook(bot_token: str):
    url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
    try:
        resp = requests.get(url, timeout=10)
        logging.info("Webhook reset: %s", resp.json())
    except Exception as e:
        logging.warning("Reset webhook failed: %s", e)

# ============================ ПИНГ-СЕРВЕР ДЛЯ RENDER ============================

async def _alive(_):
    return web.Response(text="alive")

async def run_web():
    app_web = web.Application()
    app_web.router.add_get("/", _alive)
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("HTTP ping server started on port %s", port)

def start_web_in_thread():
    def _runner():
        asyncio.run(run_web())
    threading.Thread(target=_runner, daemon=True).start()

# ============================ РАСПИСАНИЕ ============================

@dataclass
class Task:
    id: int
    chat_id: int
    text: str
    type: str        # once | daily
    run_at_utc: Optional[int] = None
    hh: Optional[int] = None
    mm: Optional[int] = None

def load_active_tasks() -> List[Task]:
    out: List[Task] = []
    with db() as c:
        for r in c.execute("SELECT * FROM tasks"):
            out.append(Task(
                id=r["id"], chat_id=r["chat_id"], text=r["text"],
                type=r["type"], run_at_utc=r["run_at_utc"], hh=r["hh"], mm=r["mm"]
            ))
    return out

async def schedule_task(app: Application, t: Task):
    jq = app.job_queue
    if t.type == "once" and t.run_at_utc:
        when = datetime.utcfromtimestamp(t.run_at_utc).replace(tzinfo=pytz.UTC)
        jq.run_once(callback=notify_job, when=when, name=f"task-{t.id}", data={"task_id": t.id})
    elif t.type == "daily" and t.hh is not None and t.mm is not None:
        jq.run_daily(callback=notify_job, time=datetime.time(datetime(2000,1,1, t.hh, t.mm, 0, tzinfo=TZ)),
                     name=f"task-{t.id}", data={"task_id": t.id})
    else:
        logging.warning("Skip schedule bad task: %s", t)

async def notify_job(context: ContextTypes.DEFAULT_TYPE):
    task_id = context.job.data["task_id"]
    with db() as c:
        row = c.execute("SELECT chat_id, text, type FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return
        chat_id = row["chat_id"]
        text = row["text"]
        ttype = row["type"]
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ Напоминание: {text}")
        if ttype == "once":
            c.execute("DELETE FROM tasks WHERE id=?", (task_id,))

# ============================ ПАРСЕР ТЕКСТА ============================

def parse_text_to_task(chat_id: int, text: str) -> Optional[Task]:
    s = text.strip().lower()

    # через N минут|секунд <что-то>
    m = re.fullmatch(r"через\s+(\d+)\s*(минут[уы]?|секунд[уы]?)\s+(.+)", s)
    if m:
        qty = int(m.group(1))
        unit = m.group(2)
        body = m.group(3)
        delta = timedelta(minutes=qty) if unit.startswith("минут") else timedelta(seconds=qty)
        run_local = datetime.now(TZ) + delta
        run_utc = int(run_local.astimezone(pytz.UTC).timestamp())
        return Task(id=0, chat_id=chat_id, text=body, type="once", run_at_utc=run_utc)

    # сегодня в HH:MM <что-то>
    m = re.fullmatch(r"сегодня\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        body = m.group(3)
        now = datetime.now(TZ)
        run_local = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_local < now:
            run_local += timedelta(days=1)
        run_utc = int(run_local.astimezone(pytz.UTC).timestamp())
        return Task(id=0, chat_id=chat_id, text=body, type="once", run_at_utc=run_utc)

    # завтра в HH:MM <что-то>
    m = re.fullmatch(r"завтра\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        body = m.group(3)
        run_local = (datetime.now(TZ) + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        run_utc = int(run_local.astimezone(pytz.UTC).timestamp())
        return Task(id=0, chat_id=chat_id, text=body, type="once", run_at_utc=run_utc)

    # каждый день в HH:MM <что-то>
    m = re.fullmatch(r"каждый\s+день\s+в\s+(\d{1,2}):(\d{2})\s+(.+)", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        body = m.group(3)
        return Task(id=0, chat_id=chat_id, text=body, type="daily", hh=hh, mm=mm)

    return None

# ============================ ХЭНДЛЕРЫ ============================

WELCOME_PRIVATE = "Этот бот приватный. Введите ключ доступа."
HELP_TEXT = (
    "Примеры:\n"
    "• через 2 минуты поесть\n"
    "• сегодня в 18:30 позвонить\n"
    "• завтра в 09:00 в зал\n"
    "• каждый день в 07:45 зарядка\n"
)

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_auth(chat_id):
        await update.message.reply_text(WELCOME_PRIVATE)
        return
    await update.message.reply_text("Привет, я твой личный ассистент.\n" + HELP_TEXT)

async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def log_any_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    logging.info("TEXT FROM %s(%s): %r", update.effective_user.id, update.effective_chat.id, m.text)

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    import traceback
    logging.error("ERROR: %s", traceback.format_exc())

# --- Техработы
async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    set_flag("maintenance", "1")
    await update.message.reply_text("🛠 Техработы включены.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    set_flag("maintenance", "0")
    await update.message.reply_text("✅ Техработы выключены.")

# --- Ключи (только админ)
async def issue_key_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    k = issue_random_key()
    if not k:
        await update.message.reply_text("Ключей нет. Пополни пул.")
    else:
        await update.message.reply_text(f"🔑 Твой ключ: `{k}`", parse_mode="Markdown")

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Осталось ключей: {keys_left()}")

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Свободные (не выданы): {keys_free()}")

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"Использованные: {keys_used()}")

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    keys_reset()
    await update.message.reply_text("Ключи сброшены (все свободны).")

# --- Список дел / удаление (минимальный вариант)
async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_chat.id):
        await update.message.reply_text(WELCOME_PRIVATE)
        return
    with db() as c:
        rows = c.execute("SELECT id, text, type, run_at_utc, hh, mm FROM tasks WHERE chat_id=?", (update.effective_chat.id,)).fetchall()
    if not rows:
        await update.message.reply_text("Список пуст.")
        return
    lines = []
    for r in rows:
        if r["type"] == "once":
            dt = datetime.utcfromtimestamp(r["run_at_utc"]).replace(tzinfo=pytz.UTC).astimezone(TZ)
            when = dt.strftime("%d.%m %H:%M")
        else:
            when = f"каждый день {r['hh']:02d}:{r['mm']:02d}"
        lines.append(f"{r['id']}. {r['text']} — {when}")
    await update.message.reply_text("\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_chat.id):
        await update.message.reply_text(WELCOME_PRIVATE)
        return
    m = re.fullmatch(r"/affairs_delete\s+(\d+)", update.message.text.strip())
    if not m:
        await update.message.reply_text("Формат: /affairs_delete <id>")
        return
    tid = int(m.group(1))
    with db() as c:
        c.execute("DELETE FROM tasks WHERE id=? AND chat_id=?", (tid, update.effective_chat.id))
    await update.message.reply_text("Удалил.")

# --- Общий текст: либо ключ, либо напоминание
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # техработы
    if get_flag("maintenance", "0") == "1" and update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🛠 Ведутся техработы. Попробуй позже.")
        return

    # если не авторизован — ждём ключ (5 символов)
    if not is_auth(chat_id):
        k = text
        if len(k) == 5 and use_key(chat_id, k):
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text("Теперь можно создавать напоминания.\n" + HELP_TEXT)
        else:
            await update.message.reply_text(WELCOME_PRIVATE)
        return

    # парсим задачу
    t = parse_text_to_task(chat_id, text)
    if not t:
        await update.message.reply_text("⚠️ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    # сохраняем
    with db() as c:
        if t.type == "once":
            cur = c.execute(
                "INSERT INTO tasks(chat_id, text, type, run_at_utc) VALUES(?,?,?,?)",
                (t.chat_id, t.text, t.type, t.run_at_utc),
            )
        else:
            cur = c.execute(
                "INSERT INTO tasks(chat_id, text, type, hh, mm) VALUES(?,?,?,?,?)",
                (t.chat_id, t.text, t.type, t.hh, t.mm),
            )
        t.id = cur.lastrowid

    # планируем
    await schedule_task(ctx.application, t)

    # ответ пользователю
    if t.type == "once":
        ts_local = datetime.utcfromtimestamp(t.run_at_utc).replace(tzinfo=pytz.UTC).astimezone(TZ)
        await update.message.reply_text(f"Отлично, напомню: «{t.text}» — {ts_local.strftime('%d.%m.%Y %H:%M')}")
    else:
        await update.message.reply_text(f"Отлично, напомню каждый день в {t.hh:02d}:{t.mm:02d}: «{t.text}»")

# ============================ СТАРТ/ПЕРЕЗАПУСК ============================

async def on_startup(app: Application):
    # на всякий случай удалим webhook, чтобы не было конфликта getUpdates
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)

    # перезапланируем активные задачи
    try:
        for t in load_active_tasks():
            await schedule_task(app, t)
        logging.info("Rescheduled all tasks.")
    except Exception:
        logging.exception("Reschedule failed")

# ============================ MAIN ============================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render -> Environment.")

    init_db()
    ensure_keys_pool(1000)

    # сброс вебхука и запуск HTTP-пинга
    reset_webhook(BOT_TOKEN)
    start_web_in_thread()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # меню команд
    try:
        app.bot.set_my_commands([
            BotCommand("start", "Помощь и примеры"),
            BotCommand("affairs", "Список дел"),
            BotCommand("affairs_delete", "Удалить дело по номеру"),
            BotCommand("maintenance_on", "Техработы: включить (админ)"),
            BotCommand("maintenance_off", "Техработы: выключить (админ)"),
            BotCommand("issue_key", "Выдать ключ (админ)"),
            BotCommand("keys_left", "Осталось ключей (админ)"),
            BotCommand("keys_free", "Свободные ключи (админ)"),
            BotCommand("keys_used", "Использованные ключи (админ)"),
            BotCommand("keys_reset", "Сбросить ключи (админ)"),
            BotCommand("ping", "Проверка связи"),
        ])
    except Exception:
        logging.exception("set_my_commands failed")

    # отладочные
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(MessageHandler(filters.TEXT, log_any_text), group=-1)
    app.add_error_handler(on_error)

    # обычные пользователи
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))

    # админ (ключи)
    app.add_handler(CommandHandler("issue_key", issue_key_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))
    app.add_handler(CommandHandler("keys_free", keys_free_cmd))
    app.add_handler(CommandHandler("keys_used", keys_used_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    # техработы
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))

    # текст — постановка задач / ввод ключа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # стартовые действия
    app.post_init = on_startup

    logging.info("Bot started. Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
