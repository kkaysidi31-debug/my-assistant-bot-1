import os
import re
import sqlite3
import logging
import secrets
import string
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Optional, List, Tuple, Dict

import pytz
from aiohttp import web
from telegram import (
    Update,
    BotCommand,
)
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler,
    ContextTypes, filters, JobQueue,
)

# -------------------- НАСТРОЙКИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TZ_NAME = os.environ.get("TZ", "Europe/Kaliningrad").strip() or "Europe/Kaliningrad"
TZ = pytz.timezone(TZ_NAME)

PORT = int(os.environ.get("PORT", "10000"))

DB_PATH = "bot.db"

WELCOME_TEXT = (
    "Привет, я твой личный ассистент. Помогу тебе оптимизировать рутинные задачи, чтобы ты ничего не забыл.\n\n"
    "Примеры:\n"
    "• через 2 минуты поесть / через 30 секунд позвонить\n"
    "• сегодня в 18:30 попить воды\n"
    "• завтра в 09:00 сходить в зал\n"
    "• каждый день в 07:45 чистить зубы\n"
    "• 30 августа в 10:00 оплатить кредит\n\n"
    "❗ Напоминание «за N минут»: просто поставь время на N минут раньше."
)

# -------------------- ХРАНИЛКИ --------------------
def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS auth(
            chat_id INTEGER PRIMARY KEY
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL,           -- once/daily/monthly
            run_at_utc TEXT,              -- ISO
            hour INTEGER, minute INTEGER,
            day_of_month INTEGER,
            active INTEGER DEFAULT 1
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS keys(
            key TEXT PRIMARY KEY,
            issued INTEGER DEFAULT 0,   -- выдан админу, но не применён
            used_by INTEGER,           -- chat_id, если активирован
            used_ts TEXT               -- ISO when used
        )""")

# -------------------- КЛЮЧИ --------------------
ALPH = string.ascii_letters + string.digits

def gen_key() -> str:
    return "".join(secrets.choice(ALPH) for _ in range(5))

def ensure_keys_pool(n: int = 1000):
    with db() as c:
        cur = c.execute("SELECT COUNT(*) FROM keys WHERE used_by IS NULL")
        free_now = cur.fetchone()[0]
        need = max(0, n - free_now)
        if need > 0:
            rows = [(gen_key(), 0, None, None) for _ in range(need)]
            # избегаем коллизий: вставляем по одной с игнором
            for k, i, u, t in rows:
                c.execute("INSERT OR IGNORE INTO keys(key,issued,used_by,used_ts) VALUES(?,?,?,?)",
                          (k, i, u, t))
            logging.info("Keys ensured: +%s (free now will be >= %s)", need, n)

def stats_keys() -> Tuple[int,int,int]:
    with db() as c:
        free_ = c.execute("SELECT COUNT(*) FROM keys WHERE used_by IS NULL").fetchone()[0]
        used_ = c.execute("SELECT COUNT(*) FROM keys WHERE used_by IS NOT NULL").fetchone()[0]
        issued_ = c.execute("SELECT COUNT(*) FROM keys WHERE issued=1 AND used_by IS NULL").fetchone()[0]
    return free_, used_, issued_

def issue_random_key() -> Optional[str]:
    with db() as c:
        row = c.execute(
            "SELECT key FROM keys WHERE issued=0 AND used_by IS NULL LIMIT 1"
        ).fetchone()
        if not row:
            return None
        k = row[0]
        c.execute("UPDATE keys SET issued=1 WHERE key=?", (k,))
        return k

def use_key(chat_id: int, key: str) -> bool:
    with db() as c:
        row = c.execute(
            "SELECT key, issued, used_by FROM keys WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return False
        _, issued, used_by = row
        if used_by is not None:
            return False
        # разрешаем использовать только выданные ключи
        if issued != 1:
            return False
        c.execute(
            "UPDATE keys SET used_by=?, used_ts=? WHERE key=?",
            (chat_id, datetime.now(timezone.utc).isoformat(), key)
        )
        c.execute("INSERT OR IGNORE INTO auth(chat_id) VALUES(?)", (chat_id,))
        return True

def is_auth(chat_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT 1 FROM auth WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row)

# -------------------- ВСПОМОГАТОРЫ ВРЕМЕНИ --------------------
def now_tz() -> datetime:
    return datetime.now(TZ)

def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = TZ.localize(dt_local)
    return dt_local.astimezone(timezone.utc)

def fmt(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

# -------------------- JOBS --------------------
@dataclass
class Task:
    id: int
    chat_id: int
    title: str
    type: str
    run_at_utc: Optional[str]
    hour: Optional[int]
    minute: Optional[int]
    day_of_month: Optional[int]
    active: int

def get_task(task_id:int) -> Optional[Task]:
    with db() as c:
        r = c.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month,active FROM tasks WHERE id=?",
                      (task_id,)).fetchone()
    return Task(*r) if r else None

async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    t = get_task(ctx.job.data["id"])
    if not t or not t.active:
        return
    await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")
    # деактивируем одноразовую
    with db() as c:
        c.execute("UPDATE tasks SET active=0 WHERE id=?", (t.id,))

async def job_daily(ctx: ContextTypes.DEFAULT_TYPE):
    t = get_task(ctx.job.data["id"])
    if t and t.active:
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")

async def job_monthly(ctx: ContextTypes.DEFAULT_TYPE):
    t = get_task(ctx.job.data["id"])
    if t and t.active and now_tz().day == (t.day_of_month or 1):
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")

async def schedule(app: Application, t: Task):
    jq = app.job_queue
    # Сначала снимаем старые джобы с таким id
    for j in jq.get_jobs_by_name(f"task_{t.id}"):
        j.schedule_removal()

    if t.type == "once" and t.run_at_utc:
        run_at_utc = datetime.fromisoformat(t.run_at_utc)
        if run_at_utc > datetime.now(timezone.utc):
            jq.run_once(
                job_once,
                when=run_at_utc,
                data={"id": t.id},
                name=f"task_{t.id}",
            )
    elif t.type == "daily":
        jq.run_daily(
            job_daily,
            time=time(hour=t.hour or 0, minute=t.minute or 0, tzinfo=TZ),
            data={"id": t.id},
            name=f"task_{t.id}",
        )
    elif t.type == "monthly":
        jq.run_daily(
            job_monthly,
            time=time(hour=t.hour or 0, minute=t.minute or 0, tzinfo=TZ),
            data={"id": t.id},
            name=f"task_{t.id}",
        )

async def reschedule_all(app: Application):
    with db() as c:
        rows = c.execute(
            "SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month,active "
            "FROM tasks WHERE active=1"
        ).fetchall()
    for row in rows:
        await schedule(app, Task(*row))

# -------------------- ПАРСЕР ТЕКСТА --------------------
months = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12
}

def parse_task(text:str) -> Optional[Tuple[str, str, datetime, Optional[int], Optional[int], Optional[int]]]:
    """
    Возвращает: (type, title, run_at_local, hour, minute, day_of_month)
    type: once/daily/monthly
    """
    t = text.lower().strip()

    # через N минут/секунд/часов
    m = re.async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE["on"] = True
    await update.message.reply_text("Техработы включены.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE["on"] = False
    await update.message.reply_text("Техработы выключены.")

# --- админ: ключи ---
async def issue_key_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    k = issue_random_key()
    if not k:
        await update.message.reply_text("Свободных ключей нет.")
        return
    await update.message.reply_text(f"Твой ключ: `{k}`", parse_mode="Markdown")

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    free_, used_, issued_ = stats_keys()
    await update.message.reply_text(f"Свободно: {free_}\nВыдано (ждут активации): {issued_}\nИспользовано: {used_}")

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    free_, _, _ = stats_keys()
    await update.message.reply_text(str(free_))

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    _, used_, _ = stats_keys()
    await update.message.reply_text(str(used_))

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    # перегенерация пула (опасно в проде; оставим только дозаполнение)
    ensure_keys_pool(1000)
    await update.message.reply_text("Пул ключей пополнен до 1000 свободных.")

# --- текст: авторизация + добавление дел ---
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Техработы
    if MAINTENANCE["on"] and update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Идут техработы. Попробуй позже.")
        return

    # Авторизация
    if not is_auth(chat):
        if use_key(chat, text):
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text(WELCOME_TEXT)
        else:
            await update.message.reply_text("❌ Неверный ключ доступа.")
        return

    # Удаление текстом: "affairs delete 3" (альтернатива команде)
    m = re.fullmatch(r"(?i)\s*affairs\s*delete\s+(\d+)\s*", text)
    if m:
        idx = int(m.group(1))
        with db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE chat_id=? AND active=1 ORDER BY id", (chat,)).fetchall()]
        if not ids or idx<1 or idx>len(ids):
            await update.message.reply_text("Сначала открой /affairs.")
            return
        tid = ids[idx-1]
        with db() as c:
            c.execute("UPDATE tasks SET active=0 WHERE id=?", (tid,))
        for j in ctx.application.job_queue.get_jobs_by_name(f"task_{tid}"):
            j.schedule_removal()
        await update.message.reply_text("Удалено.")
        return

    # Добавление
    parsed = parse_task(text)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    typ, title, run_local, hh, mm, dom = parsed
    run_at_utc = None
    hour = mmnt = daym = None
    if typ == "once":
        run_at_utc = to_utc(run_local).isoformat()
    elif typ == "daily":
        hour, mmnt = hh, mm
    else:
        hour, mmnt, daym = hh, mm, dom

    with db() as c:
        c.execute(
            "INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month,active) VALUES(?,?,?,?,?,?,?,1)",
            (chat, title, typ, run_at_utc, hour, mmnt, daym)
        )
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    t = get_task(tid)
    try:


await schedule(ctx.application, t)
        if t.type == "once":
            await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — {fmt(datetime.fromisoformat(t.run_at_utc))}")
        elif t.type == "daily":
            await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — каждый день в {t.hour:02d}:{t.minute:02d}")
        else:
            await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — каждый месяц, {t.day_of_month}-го в {t.hour:02d}:{t.minute:02d}")
    except Exception:
        logging.exception("schedule failed")
        await update.message.reply_text("⚠️ Задачу сохранил, но возникла ошибка при планировании. Попробую ещё раз через минуту.")
        # запасной рескейджул через минуту
        ctx.application.job_queue.run_once(lambda c: asyncio.create_task(reschedule_all(ctx.application)), when=timedelta(minutes=1))

# -------------------- KEEP-ALIVE HTTP --------------------
async def handle_root(request):
    return web.Response(text="alive")

async def run_web():
    app = web.Application()
    app.add_routes([web.get('/', handle_root)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("HTTP keep-alive running on port %s", PORT)

# -------------------- СТАРТ --------------------
async def on_startup(app: Application):
    # убрать возможный старый webhook на всякий
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)
    try:
        await reschedule_all(app)
    except Exception:
        logging.exception("Reschedule failed")

async def set_commands(app: Application):
    cmds = [
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("maintenance_on", "Техработы: включить (только админ)"),
        BotCommand("maintenance_off", "Техработы: выключить (только админ)"),
        BotCommand("issue_key", "Выдать новый ключ (только админ)"),
        BotCommand("keys_left", "Статистика ключей (только админ)"),
        BotCommand("keys_free", "Свободные ключи (число, только админ)"),
        BotCommand("keys_used", "Использованные ключи (только админ)"),
        BotCommand("keys_reset", "Пополнить пул ключей до 1000"),
    ]
    await app.bot.set_my_commands(cmds)

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render → Environment.")
    init_db()
    ensure_keys_pool(1000)

    # веб-сервер для пингов
    asyncio.create_task(run_web())

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

    app.post_init = on_startup

    # запускаем
    await set_commands(app)
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
