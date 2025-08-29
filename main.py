import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import List, Optional, Tuple, Set
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# =========================
# НАСТРОЙКИ
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Укажи его в переменных окружения Render.")

ADMIN_ID = 963586834  # твой Telegram ID (админ)
TZ = ZoneInfo("Europe/Kaliningrad")

DB_PATH = os.getenv("DB_PATH", "data.sqlite3")

# Для UptimeRobot/Render free — поднимем health-сервер
PORT = int(os.getenv("PORT", "10000"))  # Render отдаёт порт через переменную PORT

WELCOME_TEXT = (
    "Привет, я твой личный ассистент. Я помогу тебе оптимизировать все твои рутинные задачи, "
    "чтобы ты сосредоточился на самом главном и ничего не забыл.\n\n"
    "Примеры:\n"
    "• через 2 минуты поесть / через 30 секунд позвонить\n"
    "• сегодня в 18:30 попить воды\n"
    "• завтра в 09:00 сходить в зал\n"
    "• каждый день в 07:45 чистить зубы\n"
    "• 30 числа в 10:00 оплатить кредит\n\n"
    "❗️ Напоминание «за N минут»: просто поставь время на N минут раньше."
)

PRIVATE_PROMPT = "Этот бот приватный. Введите ключ доступа в формате ABC123."

# =========================
# ИНИЦИАЛИЗАЦИЯ БД
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                type TEXT NOT NULL,            -- once | daily | monthly
                run_at_utc TEXT,               -- ISO для once
                hour INTEGER, minute INTEGER,  -- для daily/monthly
                day_of_month INTEGER           -- для monthly
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS keys(
                key TEXT PRIMARY KEY,
                chat_id INTEGER,
                used_at TEXT
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_state(
                chat_id INTEGER PRIMARY KEY,
                authed INTEGER NOT NULL DEFAULT 0
            );
        """)
        con.commit()

    # Инициализируем VIP001..VIP100
    with db() as con:
        for i in range(1, 101):
            k = f"VIP{i:03d}"
            con.execute("INSERT OR IGNORE INTO keys(key, chat_id, used_at) VALUES(?, NULL, NULL)", (k,))
        con.commit()

def set_authed(chat_id: int, ok: bool):
    with db() as con:
        con.execute("INSERT INTO user_state(chat_id, authed) VALUES(?, ?) ON CONFLICT(chat_id) DO UPDATE SET authed=excluded.authed",
                    (chat_id, 1 if ok else 0))
        con.commit()

def is_auth(chat_id: int) -> bool:
    if chat_id == ADMIN_ID:
        return True
    with db() as con:
        r = con.execute("SELECT authed FROM user_state WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r["authed"])

def try_use_key(chat_id: int, text: str) -> bool:
    k = text.strip().upper()
    if not re.fullmatch(r"VIP\d{3}", k):
        return False
    with db() as con:
        row = con.execute("SELECT key, chat_id FROM keys WHERE key=?", (k,)).fetchone()
        if not row:
            return False
        # если ключ уже назначен этому же чату — просто подтверждаем
        if row["chat_id"] == chat_id:
            set_authed(chat_id, True)
            return True
        # если ключ не занят — назначаем
        if row["chat_id"] is None:
            con.execute("UPDATE keys SET chat_id=?, used_at=? WHERE key=?", (chat_id, datetime.utcnow().isoformat(), k))
            con.commit()
            set_authed(chat_id, True)
            return True
        # ключ занят другим
        return False

def keys_left() -> int:
    with db() as con:
        r = con.execute("SELECT COUNT(*) AS c FROM keys WHERE chat_id IS NULL").fetchone()
        return r["c"] if r else 0

# =========================
# МОДЕЛЬ/ХЕЛПЕРЫ
# =========================
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

def row_to_task(row: sqlite3.Row) -> Task:
    run = datetime.fromisoformat(row["run_at_utc"]).replace(tzinfo=timezone.utc) if row["run_at_utc"] else None
    return Task(
        id=row["id"], chat_id=row["chat_id"], title=row["title"], type=row["type"],
        run_at_utc=run, hour=row["hour"], minute=row["minute"], day_of_month=row["day_of_month"]
    )

def add_task(chat_id: int, title: str, type_: str,
             run_at_utc: Optional[datetime],
             h: Optional[int], m: Optional[int], d: Optional[int]) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month) VALUES(?,?,?,?,?,?,?)",
            (chat_id, title, type_, run_at_utc.isoformat() if run_at_utc else None, h, m, d))
        con.commit()
        return cur.lastrowid

def get_task(tid: int) -> Optional[Task]:
    with db() as con:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return row_to_task(row) if row else None

def delete_task(tid: int) -> bool:
    with db() as con:
        cur = con.execute("DELETE FROM tasks WHERE id=?", (tid,))
        con.commit()
        return cur.rowcount > 0

def list_active_tasks(chat_id: int) -> List[Task]:
    with db() as con:
        rows = con.execute("SELECT * FROM tasks WHERE chat_id=?", (chat_id,)).fetchall()
        return [row_to_task(r) for r in rows]

# =========================
# ПАРСИНГ КОМАНД ПОЛЬЗОВАТЕЛЯ
# =========================
@dataclass
class ParsedTask:
    type: str                 # once | daily | monthly
    title: str
    run_utc: Optional[datetime]  # для once
    h: Optional[int]
    m: Optional[int]
    d: Optional[int]          # для monthly

# относительное «через N ...»
RELATIVE_RE = re.compile(
    r"(?i)^\s*через\s+(\d{1,4})\s*(секунд(?:ы|у)?|сек|с|минут(?:ы|у)?|мин|m|час(?:а|ов)?|ч)\s+(.+?)\s*$"
)
TODAY_RE = re.compile(r"(?i)^\s*сегодня\s+в\s+(\d{1,2}):(\d{2})\s+(.+?)\s*$")
TOMORROW_RE = re.compile(r"(?i)^\s*завтра\s+в\s+(\d{1,2}):(\d{2})\s+(.+?)\s*$")
DAILY_RE = re.compile(r"(?i)^\s*каждый\s+день\s+в\s+(\d{1,2}):(\d{2})\s+(.+?)\s*$")
MONTHLY_RE = re.compile(r"(?i)^\s*(\d{1,2})\s*(?:числ[оа]?)?\s+в\s+(\d{1,2}):(\d{2})\s+(.+?)\s*$")

def parse_user_text_to_task(text: str, now_tz: datetime) -> Optional[ParsedTask]:
    txt = text.strip()

    m = RELATIVE_RE.match(txt)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        title = m.group(3).strip()
        if "сек" in unit or unit in ("с",):
            delta = timedelta(seconds=amount)
        elif "мин" in unit or unit == "m":
            delta = timedelta(minutes=amount)
        elif "час" in unit or unit == "ч":
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(minutes=amount)
        run_local = now_tz + delta
        run_utc = run_local.astimezone(timezone.utc)
        return ParsedTask("once", title, run_utc, None, None, None)

    m = TODAY_RE.match(txt)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if run_local <= now_tz:
            run_local = run_local + timedelta(days=1)  # на завтра, если время уже прошло
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = TOMORROW_RE.match(txt)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        run_local = (now_tz + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return ParsedTask("once", title, run_local.astimezone(timezone.utc), None, None, None)

    m = DAILY_RE.match(txt)
    if m:
        h, mi, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        return ParsedTask("daily", title, None, h, mi, None)

    m = MONTHLY_RE.match(txt)
    if m:
        d, h, mi, title = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        return ParsedTask("monthly", title, None, h, mi, d)

    return None

# =========================
# JOB QUEUE / ПЛАНИРОВАНИЕ
# =========================
async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        tid = ctx.job.data["id"]
        t = get_task(tid)
        if not t:
            return
        await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: «{t.title}»")
        # одноразовые удаляем после отправки
        if t.type == "once":
            delete_task(t.id)
    except Exception as e:
        logging.exception("Ошибка в job_once: %s", e)

async def schedule_task(app: Application, t: Task):
    jq = app.job_queue
    # уберём предыдущие job'ы с тем же именем
    name = f"task_{t.id}"
    for j in jq.get_jobs_by_name(name):
        j.schedule_removal()

    now_utc = datetime.now(timezone.utc)

    if t.type == "once":
        run = t.run_at_utc or now_utc + timedelta(seconds=2)
        if run <= now_utc:
            run = now_utc + timedelta(seconds=2)
        jq.run_once(job_once, when=run, name=name, data={"id": t.id}, chat_id=t.chat_id)
    elif t.type == "daily":
        fire = dtime(hour=t.hour, minute=t.minute, tzinfo=TZ)
        jq.run_daily(job_once, time=fire, name=name, data={"id": t.id}, chat_id=t.chat_id)
    elif t.type == "monthly":
        fire = dtime(hour=t.hour, minute=t.minute, tzinfo=TZ)

        async def monthly(ctx: ContextTypes.DEFAULT_TYPE):
            try:
                tid = ctx.job.data["id"]
                tt = get_task(tid)
                if not tt:
                    return
                if datetime.now(TZ).day == tt.day_of_month:
                    await ctx.bot.send_message(tt.chat_id, f"🔔 Напоминание: «{tt.title}»")
            except Exception as e:
                logging.exception("Ошибка в monthly: %s", e)

        jq.run_daily(monthly, time=fire, name=name, data={"id": t.id}, chat_id=t.chat_id)

async def reschedule_all(app: Application):
    with db() as con:
        rows = con.execute("SELECT * FROM tasks").fetchall()
        for r in rows:
            t = row_to_task(r)
            # пропускаем одноразовые из прошлого
            if t.type == "once" and t.run_at_utc and t.run_at_utc < datetime.now(timezone.utc) - timedelta(minutes=5):
                continue
            await schedule_task(app, t)

# =========================
# ТЕХРАБОТЫ
# =========================
MAINTENANCE = False
MAINTENANCE_WAITERS: Set[int] = set()

def guard_maintenance(update: Update) -> bool:
    global MAINTENANCE
    if not MAINTENANCE:
        return False
    chat_id = update.effective_chat.id
    MAINTENANCE_WAITERS.add(chat_id)
    return True

# =========================
# УТИЛИТЫ
# =========================
def fmt(dt_utc: datetime) -> str:
    return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

# =========================
# КОМАНДЫ
# =========================
LAST_LIST_INDEX: dict[int, List[int]] = {}

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        await update.message.reply_text("⚠ Уважаемые пользователи, проводятся технические работы.")
        return
    await update.message.reply_text(PRIVATE_PROMPT)

async def affairs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        await update.message.reply_text("⚠ Уважаемые пользователи, проводятся технические работы.")
        return
    chat = update.effective_chat.id
    if not is_auth(chat):
        await update.message.reply_text(PRIVATE_PROMPT)
        return
    tasks = list_active_tasks(chat)
    if not tasks:
        await update.message.reply_text("Твоих дел пока нет.")
        LAST_LIST_INDEX[chat] = []
        return

    now_local = datetime.now(TZ)

    def next_run(t: Task) -> datetime:
        if t.type == "once" and t.run_at_utc:
            return t.run_at_utc.astimezone(TZ)
        if t.type == "daily":
            cand = now_local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if cand <= now_local:
                cand += timedelta(days=1)
            return cand
        if t.type == "monthly":
            d = t.day_of_month or 1
            cand = now_local.replace(day=min(d, 28), hour=t.hour or 0, minute=t.minute or 0, second=0, microsecond=0)
            # корректируем день (28..31)
            while True:
                try:
                    cand = cand.replace(day=d)
                    break
                except ValueError:
                    d -= 1
            if cand <= now_local:
                # следующий месяц
                month = cand.month + 1
                year = cand.year + (1 if month > 12 else 0)
                month = 1 if month > 12 else month
                nd = min(t.day_of_month or 1, 28)
                cand = cand.replace(year=year, month=month, day=nd)
                while True:
                    try:
                        cand = cand.replace(day=t.day_of_month or nd)
                        break
                    except ValueError:
                        cand = cand.replace(day=cand.day - 1)
            return cand
        return now_local

    tasks_sorted = sorted(tasks, key=next_run)[:20]
    LAST_LIST_INDEX[chat] = [t.id for t in tasks_sorted]
    lines = []
    for i, t in enumerate(tasks_sorted, 1):
        if t.type == "once" and t.run_at_utc:
            when = fmt(t.run_at_utc)
        elif t.type == "daily":
            when = f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else:
            when = f"{t.day_of_month} числа в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {when}")
    await update.message.reply_text("Твои дела:\n" + "\n".join(lines))

# Админ: техработы ON/OFF
async def maintenance_on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    global MAINTENANCE
    MAINTENANCE = True
    await update.message.reply_text("⚠ Уважаемые пользователи, проводятся технические работы.")

async def maintenance_off_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    global MAINTENANCE
    MAINTENANCE = False
    # оповестим тех, кто писал во время работ
    for cid in list(MAINTENANCE_WAITERS):
        try:
            await ctx.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass
    MAINTENANCE_WAITERS.clear()
    await update.message.reply_text("Техработы выключены.")

# Админ: сколько ключей осталось
async def keys_left_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Команда только для админа.")
        return
    await update.message.reply_text(f"Свободных ключей: {keys_left()}")

# =========================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# =========================
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if guard_maintenance(update):
        await update.message.reply_text("⚠ Уважаемые пользователи, проводятся технические работы.")
        return

    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # 1) Всегда сначала проверяем ввод ключа
    if re.fullmatch(r"(?i)\s*vip\d{3}\s*", text):
        ok = try_use_key(chat_id, text)
        if ok:
            await update.message.reply_text("✅ Ключ принят.")
            await update.message.reply_text(WELCOME_TEXT)
        else:
            await update.message.reply_text("❌ Неверный ключ доступа.")
        return

    # 2) Если ещё не авторизован — просим ключ
    if not is_auth(chat_id):
        await update.message.reply_text(PRIVATE_PROMPT)
        return

    # 3) Удаление по тексту: "affairs delete 3"
    m = re.fullmatch(r"(?i)\s*affairs\s+delete\s+(\d+)\s*", text)
    if m:
        idx = int(m.group(1))
        ids = LAST_LIST_INDEX.get(chat_id)
        if not ids or idx < 1 or idx > len(ids):
            await update.message.reply_text("Сначала открой /affairs.")
            return
        tid = ids[idx - 1]
        t = get_task(tid)
        if t and delete_task(t.id):
            await update.message.reply_text(f"🗑 Удалено: «{t.title}»")
        else:
            await update.message.reply_text("Это дело уже удалено.")
        return

    # 4) Добавление напоминаний
    now_local = datetime.now(TZ)
    parsed = parse_user_text_to_task(text, now_local)
    if not parsed:
        await update.message.reply_text("⚠ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    # Подтверждение пользователю
    if parsed.type == "once":
        when_str = parsed.run_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
        confirm = f"Отлично, напомню: «{parsed.title}» — {when_str}"
    elif parsed.type == "daily":
        confirm = f"Отлично, напомню: каждый день в {parsed.h:02d}:{parsed.m:02d} — «{parsed.title}»"
    else:
        confirm = f"Отлично, напомню: каждое {parsed.d} число в {parsed.h:02d}:{parsed.m:02d} — «{parsed.title}»"
    await update.message.reply_text(confirm)

    # Сохраняем и планируем
    tid = add_task(chat_id, parsed.title, parsed.type, parsed.run_utc, parsed.h, parsed.m, parsed.d)
    t = get_task(tid)
    await schedule_task(ctx.application, t)

# =========================
# AIOHTTP HEALTH SERVER (для Render/UptimeRobot)
# =========================
async def start_health_server():
    app = web.Application()

    async def root(_):
        return web.Response(text="alive")

    async def health(_):
        return web.Response(text="OK")

    app.add_routes([web.get("/", root), web.get("/health", health)])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Health server started on port %s", PORT)

# =========================
# MAIN
# =========================
async def on_startup(app: Application):
    # снимаем webhook, чтобы long-polling не конфликтовал
    await app.bot.delete_webhook(drop_pending_updates=True)
    # запускаем health-сервер
    asyncio.create_task(start_health_server())
    # пересchedule задач из БД
    await reschedule_all(app)
    logging.info("Bot started. Timezone=%s", TZ)

def main():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s | %(message)s",
        level=logging.INFO
    )
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))   # админ

    # Текст (один раз!)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
