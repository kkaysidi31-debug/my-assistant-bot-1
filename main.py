# main.py
import os
import re
import sqlite3
import logging
import string
import random
import threading
from dataclasses import dataclass
from typing import Optional, List, Tuple
from datetime import datetime, timedelta, time as dtime, timezone

from zoneinfo import ZoneInfo
from aiohttp import web

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

# ------------------------ НАСТРОЙКИ ------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s"
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")

TZ_NAME = os.environ.get("TZ", "Europe/Kaliningrad")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = timezone.utc

DB_PATH = os.environ.get("DB_PATH", "db.sqlite3")

# Для пингов UptimeRobot (бесплатный Render)
PORT = int(os.environ.get("PORT", "10000"))

WELCOME_TEXT = (
    "Привет, я твой личный ассистент. Помогу тебе оптимизировать рутинные задачи, "
    "чтобы ты ничего не забыл.\n\n"
    "Примеры:\n"
    "• через 2 минуты поесть / через 30 секунд позвонить\n"
    "• сегодня в 18:30 попить воды\n"
    "• завтра в 09:00 сходить в зал\n"
    "• каждый день в 07:45 чистить зубы\n"
    "• 30 августа в 10:00 оплатить кредит\n\n"
    "❗Напоминание «за N минут»: просто поставь время на N минут раньше."
)

# ------------------------ БАЗА ДАННЫХ ------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS auth(
            chat_id INTEGER PRIMARY KEY,
            ok INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL,              -- once | daily | monthly
            run_at_utc TEXT,                 -- ISO для once
            hour INTEGER,                    -- для daily/monthly
            minute INTEGER,
            day_of_month INTEGER,            -- для monthly
            active INTEGER NOT NULL DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS keys(
            key TEXT PRIMARY KEY,
            used INTEGER NOT NULL DEFAULT 0,
            used_by INTEGER,
            used_at TEXT
        )""")
        c.commit()

# 1000 случайных 5-символьных ключей, только если таблица пуста
def ensure_keys_pool(n: int = 1000) -> None:
    with db() as c:
        cnt = c.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        if cnt >= n:
            return
        logging.info("Generating %d keys...", n - cnt)
        alphabet = string.ascii_letters + string.digits
        have = {row["key"] for row in c.execute("SELECT key FROM keys").fetchall()}
        new_keys = set()
        while len(new_keys) < (n - cnt):
            k = "".join(random.choices(alphabet, k=5))
            if k not in have and k not in new_keys:
                new_keys.add(k)
        c.executemany("INSERT OR IGNORE INTO keys(key, used) VALUES(?,0)", [(k,) for k in new_keys])
        c.commit()
        logging.info("Keys generated: %d", len(new_keys))

# ------------------------ УТИЛИТЫ ВРЕМЕНИ ------------------------

def now_tz() -> datetime:
    return datetime.now(TZ)

def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=TZ)
    return dt_local.astimezone(timezone.utc)

def fmt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

# ------------------------ АУТЕНТИФИКАЦИЯ ------------------------

def is_auth(chat_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT ok FROM auth WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row["ok"])
def set_auth(chat_id: int, ok: bool = True) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO auth(chat_id, ok) VALUES(?, ?)", (chat_id, 1 if ok else 0))
        c.commit()

def try_use_key(text: str, chat_id: int) -> bool:
    """Вернёт True, если ключ подошёл и помечён использованным."""
    k = text.strip()
    if not (5 <= len(k) <= 8) or not all(ch in (string.ascii_letters + string.digits) for ch in k):
        return False
    with db() as c:
        row = c.execute("SELECT key, used FROM keys WHERE key=?", (k,)).fetchone()
        if not row or row["used"]:
            return False
        c.execute("UPDATE keys SET used=1, used_by=?, used_at=? WHERE key=?",
                  (chat_id, datetime.utcnow().isoformat(), k))
        c.commit()
    set_auth(chat_id, True)
    return True

# ------------------------ КЛЮЧИ: КОМАНДЫ АДМИНА ------------------------

async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        free_cnt = c.execute("SELECT COUNT(*) FROM keys WHERE used=0").fetchone()[0]
        used_cnt = c.execute("SELECT COUNT(*) FROM keys WHERE used=1").fetchone()[0]
    await update.message.reply_text(f"Свободных ключей: {free_cnt}\nИспользованных: {used_cnt}")

async def issue_key_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выдать один свободный ключ админам — просто показать, но НЕ помечать как used, пока пользователь не применит."""
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        row = c.execute("SELECT key FROM keys WHERE used=0 ORDER BY RANDOM() LIMIT 1").fetchone()
    if not row:
        await update.message.reply_text("Свободных ключей не осталось.")
        return
    await update.message.reply_text(f"Ключ: `{row['key']}`", parse_mode="Markdown")

async def keys_free_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        rows = [r["key"] for r in c.execute("SELECT key FROM keys WHERE used=0 LIMIT 50")]
    if not rows:
        await update.message.reply_text("Свободных ключей нет.")
    else:
        await update.message.reply_text("Первые 50 свободных:\n" + ", ".join(rows))

async def keys_used_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        rows = [f"{r['key']} → {r['used_by']}" for r in c.execute("SELECT key,used_by FROM keys WHERE used=1 ORDER BY used_at DESC LIMIT 50")]
    if not rows:
        await update.message.reply_text("Использованных ключей пока нет.")
    else:
        await update.message.reply_text("Последние 50 использованных:\n" + "\n".join(rows))

async def keys_reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сбросить все ключи в свободные (на всякий случай). Только админ."""
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        c.execute("UPDATE keys SET used=0, used_by=NULL, used_at=NULL")
        c.commit()
    await update.message.reply_text("Все ключи сброшены в свободные.")

# ------------------------ ТАСКИ ------------------------

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
    active: int

def add_once_task(chat_id: int, title: str, run_local: datetime) -> int:
    run_utc = to_utc(run_local)
    with db() as c:
        c.execute("""INSERT INTO tasks(chat_id,title,type,run_at_utc,active)
                     VALUES(?,?,?,?,1)""",
                  (chat_id, title, "once", run_utc.isoformat()))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        return tid

def add_daily_task(chat_id: int, title: str, hour: int, minute: int) -> int:
    with db() as c:
        c.execute("""INSERT INTO tasks(chat_id,title,type,hour,minute,active)
                     VALUES(?,?,?,?,1)""",(chat_id, title, "daily", hour, minute))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        return tid

def add_monthly_task(chat_id: int, title: str, day: int, hour: int, minute: int) -> int:
    with db() as c:
        c.execute("""INSERT INTO tasks(chat_id,title,type,day_of_month,hour,minute,active)
                     VALUES(?,?,?,?,?,1)""",
                  (chat_id, title, "monthly", day, hour, minute))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        return tid

def list_active_tasks(chat_id: int) -> List[Task]:
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE active=1 AND chat_id=? ORDER BY id ASC", (chat_id,)).fetchall()
        return [
            Task(
                id=r["id"], chat_id=r["chat_id"], title=r["title"], type=r["type"],
                run_at_utc=datetime.fromisoformat(r["run_at_utc"]) if r["run_at_utc"] else None,
                hour=r["hour"], minute=r["minute"], day_of_month=r["day_of_month"], active=r["active"]
            ) for r in rows
        ]

def delete_task(tid: int) -> bool:
    with db() as c:
        row = c.execute("SELECT id FROM tasks WHERE id=? AND active=1", (tid,)).fetchone()
        if not row:
            return False
        c.execute("UPDATE tasks SET active=0 WHERE id=?", (tid,))
        c.commit()
        return True

# ------------------------ ПЛАНИРОВАНИЕ ------------------------

async def job_once(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = ctx.job.data or {}
    chat_id = data.get("chat_id")
    title = data.get("title", "")
    tid = data.get("id")
    if not chat_id or not tid:
        return
    try:
        await ctx.bot.send_message(chat_id, f"🔔 Напоминание: «{title}»")
    finally:
        # деактивируем разовую задачу
        with db() as c:
            c.execute("UPDATE tasks SET active=0 WHERE id=?", (tid,))
            c.commit()

async def schedule_task(app: Application, t: Task) -> None:
    jq = app.job_queue
    if t.type == "once" and t.run_at_utc:
        delay = (t.run_at_utc - datetime.now(timezone.utc)).total_seconds()
        if delay < 0:
            return
        jq.run_once(job_once, when=delay, data={"id": t.id, "chat_id": t.chat_id, "title": t.title}, name=f"task_{t.id}")
    elif t.type == "daily":
        jq.run_daily(
            job_once,
            time=dtime(hour=t.hour or 0, minute=t.minute or 0, tzinfo=TZ),
            data={"id": t.id, "chat_id": t.chat_id, "title": t.title},
            name=f"task_{t.id}"
        )
    elif t.type == "monthly":
        async def monthly_alarm(ctx: ContextTypes.DEFAULT_TYPE):
            today = now_tz().day
            if today == (t.day_of_month or 1):
                await job_once(ctx)
        jq.run_daily(
            monthly_alarm,
            time=dtime(hour=t.hour or 0, minute=t.minute or 0, tzinfo=TZ),
            name=f"task_{t.id}"
        )

async def reschedule_all(app: Application) -> None:
    try:
        app.job_queue.scheduler.remove_all_jobs()
    except Exception:
        pass
    for t in list_active_tasks_for_all():
        try:
            await schedule_task(app, t)
        except Exception:
            logging.exception("Failed to schedule task id=%s", t.id)

def list_active_tasks_for_all() -> List[Task]:
    with db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE active=1").fetchall()
        res = []
        for r in rows:
            res.append(Task(
                id=r["id"], chat_id=r["chat_id"], title=r["title"], type=r["type"],
                run_at_utc=datetime.fromisoformat(r["run_at_utc"]) if r["run_at_utc"] else None,
                hour=r["hour"], minute=r["minute"], day_of_month=r["day_of_month"], active=r["active"]
            ))
        return res

# ------------------------ ПАРСИНГ ТЕКСТА ------------------------

RELATIVE_RE = re.compile(r"^\s*через\s+(\d+)\s*(секунд\w*|минут\w*|час\w*)\s+(.+)$", re.IGNORECASE)
TODAY_RE    = re.compile(r"^\s*сегодня\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", re.IGNORECASE)
TOMORROW_RE = re.compile(r"^\s*завтра\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", re.IGNORECASE)
DAILY_RE    = re.compile(r"^\s*каждый\s+день\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", re.IGNORECASE)
MONTHLY_RE  = re.compile(r"^\s*(\d{1,2})\s+август\w*\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", re.IGNORECASE)

@dataclass
class ParsedTask:
    type: str                # once|daily|monthly
    title: str
    run_local: Optional[datetime] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    day_of_month: Optional[int] = None

def parse_user_text_to_task(text: str, now_local: datetime) -> Optional[ParsedTask]:
    t = text.strip()

    m = RELATIVE_RE.match(t)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        title = m.group(3).strip()
        delta = timedelta()
        if unit.startswith("сек"):
            delta = timedelta(seconds=amount)
        elif unit.startswith("мин"):
            delta = timedelta(minutes=amount)
        else:
            delta = timedelta(hours=amount)
        return ParsedTask("once", title, run_local=now_local + delta)

    m = TODAY_RE.match(t)
    if m:
        hh, mm, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return ParsedTask("once", title, run_local=run_local)

    m = TOMORROW_RE.match(t)
    if m:
        hh, mm, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = (now_local + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        return ParsedTask("once", title, run_local=run_local)

    m = DAILY_RE.match(t)
    if m:
        hh, mm, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        return ParsedTask("daily", title, hour=hh, minute=mm)

    # пример простого «30 августа в 10:00 ...»
    m = MONTHLY_RE.match(t)
    if m:
        day = int(m.group(1))
        hh, mm = int(m.group(2)), int(m.group(3))
        title = m.group(4).strip()
        return ParsedTask("monthly", title, day_of_month=day, hour=hh, minute=mm)

    return None

# ------------------------ КОМАНДЫ ------------------------

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Этот бот приватный. Введите ключ доступа.")

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    if not is_auth(chat):
        await update.message.reply_text("Этот бот приватный. Введите ключ доступа.")
        return
    tasks = list_active_tasks(chat)
    if not tasks:
        await update.message.reply_text("Пока дел нет.")
        return
    lines = []
    for i, t in enumerate(tasks, 1):
        if t.type == "once" and t.run_at_utc:
            lines.append(f"{i}. {t.title} — {fmt_local(t.run_at_utc)} (#{t.id})")
        elif t.type == "daily":
            lines.append(f"{i}. {t.title} — каждый день в {t.hour:02d}:{t.minute:02d} (#{t.id})")
        else:
            lines.append(f"{i}. {t.title} — {t.day_of_month} числа в {t.hour:02d}:{t.minute:02d} (#{t.id})")
    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

async def affairs_delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    if not is_auth(chat):
        await update.message.reply_text("Этот бот приватный. Введите ключ доступа.")
        return
    m = re.fullmatch(r"(?:/?affairs\s*delete\s*|/affairs_delete\s*)(\d+)", update.message.text.strip(), re.IGNORECASE)
    if not m:
        await update.message.reply_text("Формат: /affairs_delete <id>")
        return
    tid = int(m.group(1))
    if delete_task(tid):
        await update.message.reply_text("✅ Удалено.")
    else:
        await update.message.reply_text("Это дело уже удалено или не найдено.")

# ------------------------ ТЕКСТ ------------------------

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

# попытка применить ключ, если не авторизован
    if not is_auth(chat_id):
        if try_use_key(text, chat_id):
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text(WELCOME_TEXT)
        else:
            await update.message.reply_text("❌ Неверный ключ доступа.")
        return

    # парс и создание задачи
    p = parse_user_text_to_task(text, now_tz())
    if not p:
        await update.message.reply_text("⚠️ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    if p.type == "once" and p.run_local:
        tid = add_once_task(chat_id, p.title, p.run_local)
        t = Task(tid, chat_id, p.title, "once", to_utc(p.run_local), None, None, None, 1)
        await schedule_task(ctx.application, t)
        await update.message.reply_text(f"✅ Отлично, напомню: «{p.title}» — {p.run_local.strftime('%d.%m.%Y %H:%M')}")
    elif p.type == "daily":
        tid = add_daily_task(chat_id, p.title, p.hour or 0, p.minute or 0)
        t = Task(tid, chat_id, p.title, "daily", None, p.hour, p.minute, None, 1)
        await schedule_task(ctx.application, t)
        await update.message.reply_text(f"✅ Отлично, напомню каждый день в {p.hour:02d}:{p.minute:02d}: «{p.title}».")
    else:
        tid = add_monthly_task(chat_id, p.title, p.day_of_month or 1, p.hour or 0, p.minute or 0)
        t = Task(tid, chat_id, p.title, "monthly", None, p.hour, p.minute, p.day_of_month, 1)
        await schedule_task(ctx.application, t)
        await update.message.reply_text(
            f"✅ Отлично, напомню {p.day_of_month} числа в {p.hour:02d}:{p.minute:02d}: «{p.title}»."
        )

# ------------------------ СТАРТОВЫЕ ХУКИ ------------------------

async def on_startup(app: Application):
    # на всякий случай убираем старый вебхук, чтобы polling не конфликтовал
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)
    # Перепланировать все задачи
    try:
        await reschedule_all(app)
    except Exception:
        logging.exception("Reschedule failed")

# ------------------------ HTTP "alive" для UptimeRobot ------------------------

async def http_handle(request):
    return web.Response(text="alive")

async def run_web_app():
    app = web.Application()
    app.add_routes([web.get("/", http_handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    # держим сервер в этом потоке
    while True:
        await asyncio.sleep(3600)

def start_web_in_thread():
    import asyncio
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_web_app())
    th = threading.Thread(target=_run, daemon=True)
    th.start()

# === вызывается при старте приложения ===
async def on_startup(app: Application):
    # снимаем вебхук, чтобы polling не конфликтовал
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)

    # пересоздаём все задачи в планировщике
    try:
        await reschedule_all(app)
    except Exception as e:
        logging.exception("Reschedule failed: %s", e)
        
# ====================== НИЖНИЙ БЛОК — ВСТАВЬ ЦЕЛИКОМ ======================

import asyncio
from aiohttp import web

# HTTP эндпоинт для UptimeRobot (GET / -> "alive")
async def _alive(request):
    return web.Response(text="alive")

async def run_web():
    app = web.Application()
    app.add_routes([web.get("/", _alive)])
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# Выполняется при старте приложения (после инициализации бота)
async def on_startup(app: Application):
    # на всякий случай снимаем старый вебхук, чтобы не было конфликтов с polling
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)

    # если есть функция пересоздания задач — дернём её (иначе просто пропустим)
    try:
        if "reschedule_all" in globals():
            await reschedule_all(app)
    except Exception:
        logging.exception("reschedule_all failed")

# --------- Команды обслуживания (заглушки, чтобы не было NameError) ---------
async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Режим обслуживания включен.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Режим обслуживания выключен.")

# ====================== НИЖНИЙ БЛОК ======================

import asyncio
from aiohttp import web

# HTTP эндпоинт для UptimeRobot (GET / -> "alive")
async def _alive(request):
    return web.Response(text="alive")

async def run_web():
    app = web.Application()
    app.add_routes([web.get("/", _alive)])
    port = int(os.environ.get("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# Выполняется при старте приложения
async def on_startup(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)

# --- заглушки для maintenance ---
async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Режим обслуживания включен.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Режим обслуживания выключен.")

# ---------------- MAIN ----------------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render → Environment.")

    init_db()
    if "ensure_keys_pool" in globals():
        ensure_keys_pool(1000)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))

    # Ключи (админ)
    app.add_handler(CommandHandler("issue_key", issue_key_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))
    app.add_handler(CommandHandler("keys_free", keys_free_cmd))
    app.add_handler(CommandHandler("keys_used", keys_used_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    # Maintenance
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))

    # Тексты
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = on_startup

    # ---- запускаем одновременно ----
    await asyncio.gather(
        run_web(),
        app.start(),    # вместо run_polling
    )

if __name__ == "__main__":
    asyncio.run(main())
# ====================== КОНЕЦ ======================
