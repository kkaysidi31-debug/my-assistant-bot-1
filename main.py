# coding: utf-8
import os
import re
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Dict, Any

from zoneinfo import ZoneInfo
from telegram import Update, BotCommand
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ContextTypes, filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = ZoneInfo("Europe/Kaliningrad")  # локальная TZ

WELCOME = (
    "👋 Привет, я твой бот-Ассистент.\n\n"
    "Помогу оптимизировать рутину, чтобы ты ничего не забывал.\n\n"
    "📌 Примеры, как задавать напоминания:\n"
    "• «через 5 минут поесть»\n"
    "• «сегодня в 18:30 тренировка»\n"
    "• «завтра в 09:00 позвонить маме»\n"
    "• «каждый день в 09:00 проверить почту»\n"
    "• «30 августа заплатить за кредит»\n"
    "• «30.08 в 15:30 созвон»\n"
)

# ---------- БАЗА ----------
DB_PATH = os.path.join(os.getcwd(), "bot.db")

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db() -> None:
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            type        TEXT    NOT NULL,        -- 'once' | 'daily'
            text        TEXT    NOT NULL,
            run_at_utc  INTEGER,                 -- для 'once': unix-UTC
            daily_hhmm  INTEGER,                 -- для 'daily': HH*100+MM
            active      INTEGER NOT NULL DEFAULT 1
        );
        """)
    log.info("DB ready")

# ---------- ПЛАНИРОВЩИК ----------
async def job_fire(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет текст напоминания. Для 'once' помечает как неактивное и снимает джобу."""
    data = ctx.job.data or {}
    chat_id = int(data["chat_id"])
    text = str(data["text"])
    task_id = int(data.get("task_id", 0))
    task_type = str(data.get("type", ""))

    # отправляем сообщение
    await ctx.bot.send_message(chat_id=chat_id, text=text)

    # если одноразовая — деактивируем в БД и снимаем джобу
    if task_type == "once" and task_id:
        with db() as con:
            con.execute("UPDATE tasks SET active=0 WHERE id=?", (task_id,))
        # текущая джоба уже отработала, но на всякий случай снимем по имени
        for j in ctx.application.job_queue.get_jobs_by_name(f"task:{task_id}"):
            j.schedule_removal()

def schedule_task(app: Application, row: sqlite3.Row) -> None:
    """Добавляет задачу из БД в JobQueue."""
    if not row["active"]:
        return

    jq = app.job_queue
    name = f"task:{row['id']}"
    base_data = {
        "chat_id": row["chat_id"],
        "text": row["text"],
        "task_id": row["id"],
        "type": row["type"],
    }

    if row["type"] == "once":
        when = datetime.fromtimestamp(int(row["run_at_utc"]), tz=ZoneInfo("UTC"))
        jq.run_once(job_fire, when=when, data=base_data, name=name)
    else:
        hhmm = int(row["daily_hhmm"])
        hh, mm = divmod(hhmm, 100)
        t = dtime(hour=hh, minute=mm, tz=TZ)
        jq.run_daily(job_fire, time=t, data=base_data, name=name)

async def reschedule_all(app: Application) -> None:
    """Поднимаем активные задачи после рестарта."""
    with db() as con:
        rows = con.execute("SELECT * FROM tasks WHERE active=1").fetchall()
    for r in rows:
        schedule_task(app, r)

# ---------- ПАРСЕР NATURAL LANGUAGE ----------
RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def parse_natural_ru(msg: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает:
      {'type': 'once',  'text': str, 'run_at_utc': int}      — одноразовое
      {'type': 'daily', 'text': str, 'hhmm': int}            — ежедневное
    Или None, если не распознано.
    """
    s = re.sub(r"\s+", " ", msg.strip().lower())

    # «через N сек/мин/час ТЕКСТ»
    m = re.match(r"^через\s+(\d+)\s*(секунд|сек|минут|мин|часов|час)\s+(.+)$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        text = m.group(3).strip()

        if unit in ("секунд", "сек"):
            delta = timedelta(seconds=n)
        elif unit in ("минут", "мин"):
            delta = timedelta(minutes=n)
        else:
            delta = timedelta(hours=n)

        run_at = datetime.now(TZ) + delta
        run_at_utc = int(run_at.astimezone(ZoneInfo("UTC")).timestamp())
        return {"type": "once", "text": text, "run_at_utc": run_at_utc}

    # «сегодня в HH:MM ТЕКСТ»
    m = re.match(r"^сегодня\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        text = m.group(3).strip()
        now = datetime.now(TZ)
        run_at = datetime(now.year, now.month, now.day, hh, mm, tzinfo=TZ)
        if run_at <= now:
            run_at += timedelta(days=1)
        run_at_utc = int(run_at.astimezone(ZoneInfo("UTC")).timestamp())
        return {"type": "once", "text": text, "run_at_utc": run_at_utc}

    # «завтра в HH:MM ТЕКСТ»
    m = re.match(r"^завтра\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        text = m.group(3).strip()
        now = datetime.now(TZ) + timedelta(days=1)
        run_at = datetime(now.year, now.month, now.day, hh, mm, tzinfo=TZ)
        run_at_utc = int(run_at.astimezone(ZoneInfo("UTC")).timestamp())
        return {"type": "once", "text": text, "run_at_utc": run_at_utc}

    # «каждый день в HH:MM ТЕКСТ»
    m = re.match(r"^каждый\s+день\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        text = m.group(3).strip()
        return {"type": "daily", "text": text, "hhmm": hh * 100 + mm}

    # «30 августа ТЕКСТ» (в 09:00 по умолчанию)
    m = re.match(r"^(\d{1,2})\s+([а-яё]+)\s+(.+)$", s)
    if m and m.group(2) in RU_MONTHS:
        day = int(m.group(1))
        month = RU_MONTHS[m.group(2)]
        text = m.group(3).strip()

        now = datetime.now(TZ)
        year = now.year
        run_at = datetime(year, month, day, 9, 0, tzinfo=TZ)
        if run_at <= now:
            run_at = datetime(year + 1, month, day, 9, 0, tzinfo=TZ)
        run_at_utc = int(run_at.astimezone(ZoneInfo("UTC")).timestamp())
        return {"type": "once", "text": text, "run_at_utc": run_at_utc}

    # «30.08 в HH:MM ТЕКСТ»
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\s+в\s+(\d{1,2}):(\d{2})\s+(.+)$", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        hh, mm = int(m.group(3)), int(m.group(4))
        text = m.group(5).strip()

        now = datetime.now(TZ)
        year = now.year
        run_at = datetime(year, month, day, hh, mm, tzinfo=TZ)
        if run_at <= now:
            run_at = datetime(year + 1, month, day, hh, mm, tzinfo=TZ)
        run_at_utc = int(run_at.astimezone(ZoneInfo("UTC")).timestamp())
        return {"type": "once", "text": text, "run_at_utc": run_at_utc}

    return None

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def human_when(row: sqlite3.Row) -> str:
    if row["type"] == "once":
        dt = datetime.fromtimestamp(int(row["run_at_utc"]), tz=ZoneInfo("UTC")).astimezone(TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    hhmm = int(row["daily_hhmm"])
    hh, mm = divmod(hhmm, 100)
    return f"каждый день в {hh:02d}:{mm:02d}"

# ---------- ХЕНДЛЕРЫ ----------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)

async def tasks_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with db() as con:
        rows = con.execute("SELECT id, type, text, run_at_utc, daily_hhmm, active "
            "FROM tasks WHERE chat_id=? ORDER BY id DESC LIMIT 50",
            (chat_id,)
        ).fetchall()

    if not rows:
        await update.message.reply_text("Активных напоминаний нет.")
        return

    lines = []
    for r in rows:
        if not r["active"]:
            continue
        when = human_when(r)
        lines.append(f"✅ ID:{r['id']} — {when} — «{r['text']}»")
    await update.message.reply_text("\n".join(lines) if lines else "Активных напоминаний нет.")

async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /cancel 12")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /cancel 12")
        return

    chat_id = update.effective_chat.id
    with db() as con:
        row = con.execute("SELECT chat_id, active FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            await update.message.reply_text("Такого ID не нашёл.")
            return
        if row["chat_id"] != chat_id:
            await update.message.reply_text("Этот ID принадлежит другому чату.")
            return
        if not row["active"]:
            await update.message.reply_text("Это напоминание уже отменено.")
            return

        con.execute("UPDATE tasks SET active=0 WHERE id=?", (tid,))

    # снять из планировщика
    for j in ctx.application.job_queue.get_jobs_by_name(f"task:{tid}"):
        j.schedule_removal()

    await update.message.reply_text("🛑 Напоминание отменено.")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Парсим фразу, сохраняем в БД, планируем джобу."""
    msg = (update.message.text or "").strip()
    parsed = parse_natural_ru(msg)
    if not parsed:
        await update.message.reply_text("Не понял. Примеры см. в /help")
        return

    chat_id = update.effective_chat.id
    if parsed["type"] == "once":
        with db() as con:
            con.execute(
                "INSERT INTO tasks(chat_id,type,text,run_at_utc,active) VALUES(?,?,?,?,1)",
                (chat_id, "once", parsed["text"], parsed["run_at_utc"])
            )
            task_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

        schedule_task(ctx.application, row)

        when_local = datetime.fromtimestamp(parsed["run_at_utc"], tz=ZoneInfo("UTC")).astimezone(TZ)
        await update.message.reply_text(
            f"✅ Отлично! Напомню {when_local.strftime('%d.%m.%Y %H:%M')} — «{parsed['text']}».\nID: {task_id}"
        )
    else:
        hhmm = int(parsed["hhmm"])
        with db() as con:
            con.execute(
                "INSERT INTO tasks(chat_id,type,text,daily_hhmm,active) VALUES(?,?,?,?,1)",
                (chat_id, "daily", parsed["text"], hhmm)
            )
            task_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

        schedule_task(ctx.application, row)
        hh, mm = divmod(hhmm, 100)
        await update.message.reply_text(
            f"✅ Отлично! Ежедневное напоминание создано: «{parsed['text']}», в {hh:02d}:{mm:02d}.\nID: {task_id}"
        )

# ---------- СТАРТ ----------
async def on_startup(app: Application) -> None:
    await reschedule_all(app)
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Начать"),
            BotCommand("help", "Помощь"),
            BotCommand("tasks", "Список напоминаний"),
            BotCommand("cancel", "Отменить напоминание по ID"),
        ])
    except Exception:
        log.exception("Startup error")

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пуст. Укажи его в переменной окружения.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # текст (натуральный язык)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # поднятие задач после запуска polling
    app.post_init = on_startup
    return app

def main() -> None:
    init_db()
    app = build_app()
    log.info("Bot started. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
