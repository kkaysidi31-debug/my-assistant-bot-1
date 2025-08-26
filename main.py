# main.py
# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, time, timezone as dt_timezone

import pytz
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters
)

# ------------------------ Конфиг ------------------------

ADMIN_ID = 963586834  # твой Telegram ID (админ)
TZ_NAME = "Europe/Kaliningrad"
TZ = pytz.timezone(TZ_NAME)

DATA_FILE = "data.json"
DATA_LOCK = threading.Lock()

# Сгенерированные ключи доступа VIP001..VIP100
ACCESS_KEYS = {f"VIP{str(i).zfill(3)}": None for i in range(1, 101)}

# ------------------------ Логирование -------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("reminder-bot")

# ------------------------ Хранилище ---------------------

def _load():
    if not os.path.exists(DATA_FILE):
        return {
            "allowed_users": [],
            "keys": ACCESS_KEYS,
            "tasks": [],     # будущие задачи
            "history": [],   # выполненные задачи
            "maintenance": False,
            "pending_chats": []  # кому написать, когда техработы закончатся
        }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def db_get():
    with DATA_LOCK:
        return _load()

def db_put(mutator):
    with DATA_LOCK:
        data = _load()
        mutator(data)
        _save(data)
        return data

# --------------------- Утилиты времени ------------------

def now_local():
    return datetime.now(TZ)

def to_utc(dt_local):
    """Получить aware-UTC datetime из локального aware-датавремени."""
    return dt_local.astimezone(dt_timezone.utc)

def fmt_dt_local(dt_local):
    return dt_local.strftime("%d.%m.%Y %H:%M")

# ------------------------ Доступ ------------------------

def user_allowed(user_id: int) -> bool:
    data = db_get()
    return user_id in data["allowed_users"]

async def ask_key(update: Update):
    await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")

async def try_accept_key(update: Update) -> bool:
    """Пробует принять ключ, если сообщение похоже на ключ. Возвращает True, если ключ принят/уже есть доступ."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip().upper()

    if user_allowed(user_id):
        return True

    if not re.fullmatch(r"[A-Z]{3}\d{3}", text):
        return False

    def mutate(d):
        # ключ валиден и свободен?
        if text in d["keys"] and (d["keys"][text] is None or d["keys"][text] == user_id):
            d["keys"][text] = user_id
            if user_id not in d["allowed_users"]:
                d["allowed_users"].append(user_id)

    db_put(mutate)
    if user_allowed(user_id):
        await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
        return True
    return False

# ------------------------ Техработы ----------------------

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    def mutate(d):
        d["maintenance"] = True
    db_put(mutate)
    await update.message.reply_text("🟡 Технические работы включены.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = db_put(lambda d: d.update({"maintenance": False}))
    # Оповестить тех, кто писал во время простоя
    pending = data.get("pending_chats", [])
    if pending:
        # Очистим список и сообщим
        def clear_mut(d):
            d["pending_chats"] = []
        db_put(clear_mut)
        for chat_id in set(pending):
            try:
                await context.bot.send_message(chat_id=chat_id, text="✅ Бот снова работает.")
            except Exception as e:log.warning(f"notify back failed for {chat_id}: {e}")
    await update.message.reply_text("🟢 Технические работы отключены.")

# ------------------------ Парсер RU ----------------------

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_request_ru(text: str):
    """
    Возвращает один из словарей:
    {"after": timedelta, "text": "..."}
    {"once_at": datetime_local, "text": "..."}
    {"daily_at": time_local, "text": "..."}
    """
    t = text.strip().lower()

    # 1) через N минут/часов <текст>
    m = re.match(r"^через\s+(?P<n>\d+)\s*(?P<u>минут[уы]?|час[аов]?)\s+(?P<txt>.+)$", t)
    if m:
        n = int(m.group("n"))
        unit = m.group("u")
        txt = m.group("txt").strip()
        delta = timedelta(minutes=n) if unit.startswith("минут") else timedelta(hours=n)
        return {"after": delta, "text": txt}

    # 2) сегодня в HH:MM <текст>
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<txt>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        txt = m.group("txt").strip()
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        # если время уже прошло, двигаем на завтра
        if dt < now_local():
            dt += timedelta(days=1)
        return {"once_at": dt, "text": txt}

    # 3) завтра в HH:MM <текст>
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<txt>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        txt = m.group("txt").strip()
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        dt = base + timedelta(days=1)
        return {"once_at": dt, "text": txt}

    # 4) каждый день в HH:MM <текст>
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s+(?P<txt>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        txt = m.group("txt").strip()
        return {"daily_at": time(hour=hh, minute=mm, tzinfo=TZ), "text": txt}

    # 5) DD <месяц> [в HH:MM] <текст>
    m = re.match(
        rf"^(?P<d>\d{{1,2}})\s+(?P<mon>{'|'.join(MONTHS.keys())})(?:\s+в\s+{RE_TIME})?\s+(?P<txt>.+)$",
        t
    )
    if m:
        d = int(m.group("d"))
        mon = MONTHS[m.group("mon")]
        txt = (m.group("txt") or "").strip()
        hh = int(m.group("h")) if m.group("h") else 9
        mm = int(m.group("m")) if m.group("m") else 0
        year = now_local().year
        dt = datetime(year, mon, d, hh, mm, tzinfo=TZ)
        # Если дата уже прошла в этом году, переносим на следующий
        if dt < now_local():
            dt = datetime(year + 1, mon, d, hh, mm, tzinfo=TZ)
        return {"once_at": dt, "text": txt}

    return None

# --------------------- Планировщик задач ----------------

def add_task(chat_id: int, when_dt_local: datetime | None, text: str, kind: str, job_name: str):
    """
    Сохранить задачу в БД.
    kind: "once" или "daily"
    when_dt_local: для once — aware локальный datetime; для daily — None (в tasks кладём HH:MM отдельно)
    """
    def mutate(d):
        if kind == "once":
            d["tasks"].append({
                "id": job_name,
                "chat_id": chat_id,
                "type": "once",
                "when": when_dt_local.isoformat(),
                "text": text
            })
        elif kind == "daily":
            d["tasks"].append({
                "id": job_name,
                "chat_id": chat_id,
                "type": "daily",
                "hhmm": when_dt_local.strftime("%H:%M"),
                "text": text
            })
    db_put(mutate)

def remove_task(job_name: str):
    def mutate(d):
        d["tasks"] = [t for t in d["tasks"] if t["id"] != job_name]
    db_put(mutate)

def push_history(chat_id: int, planned_local_iso: str, text: str):
    def mutate(d):
        d["history"].append({
            "chat_id": chat_id,
            "planned_for": planned_local_iso,
            "text": text,"done_at": now_local().isoformat()
        })
    db_put(mutate)

# ---------------------- Джоб-коллбеки -------------------

async def remind_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    payload = job.data or {}
    chat_id = payload.get("chat_id")
    text = payload.get("text")
    planned_local_iso = payload.get("planned_local_iso")  # строкой

    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    finally:
        # Переносим в историю и удаляем из активных
        if planned_local_iso and chat_id is not None and text:
            push_history(chat_id, planned_local_iso, text)
        remove_task(job.name)

# ------------------------ Хэндлеры ----------------------

HELP_EXAMPLES = (
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Напоминание за какое либо кол-во времени пишите так (пример на 1 час):\n"
    "  Сегодня в 14:00 напомни, встреча в 15:00\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_allowed(uid):
        await ask_key(update)
        return

    await update.message.reply_text("Бот запущен ✅\n\n" + HELP_EXAMPLES)

async def list_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_allowed(uid):
        if await try_accept_key(update):
            await start(update, context)
        else:
            await ask_key(update)
        return

    data = db_get()
    tasks = [t for t in data["tasks"] if t["chat_id"] == uid]

    # Сформируем человекочитаемое + сохраним карту индексов в память контекста
    items = []
    index_map = []
    for t in sorted(tasks, key=lambda x: x["when"] if x["type"] == "once" else x.get("hhmm", "")):
        if t["type"] == "once":
            dt_loc = datetime.fromisoformat(t["when"])
            items.append(f"{fmt_dt_local(dt_loc)} — {t['text']}")
        else:
            items.append(f"ежедневно в {t['hhmm']} — {t['text']}")
        index_map.append(t["id"])

    if not items:
        await update.message.reply_text("Список дел пуст.")
        return

    # запомним отображение индексов для удаления
    context.user_data["index_map"] = index_map
    pretty = "Ваши ближайшие дела:\n" + "\n".join(f"{i+1}. {it}" for i, it in enumerate(items))
    await update.message.reply_text(pretty)

async def delete_affair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_allowed(uid):
        if await try_accept_key(update):
            await start(update, context)
        else:
            await ask_key(update)
        return

    if not context.args:
        await update.message.reply_text("Укажите номер дела: /affairs_delete N")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом.")
        return

    index_map = context.user_data.get("index_map")
    if not index_map:
        await update.message.reply_text("Сначала выполните /affairs, чтобы увидеть нумерацию.")
        return
    if not (1 <= n <= len(index_map)):
        await update.message.reply_text("Неверный номер.")
        return

    job_name = index_map[n-1]
    # удаляем из БД
    def mutate(d):
        d["tasks"] = [t for t in d["tasks"] if t["id"] != job_name]
    db_put(mutate)

    # отменяем джоб, если есть
    try:
        context.job_queue.get_jobs_by_name(job_name)
        for j in context.job_queue.get_jobs_by_name(job_name):
            j.schedule_removal()
    except Exception:
        pass

    await update.message.reply_text("Удалено.")

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_allowed(uid):
        if await try_accept_key(update):
            await start(update, context)
        else:
            await ask_key(update)
        return

    data = db_get()
    items = [h for h in data["history"] if h["chat_id"] == uid]
    if not items:
        await update.message.reply_text("История пуста.")
        return
    # последние 20
    items = items[-20:]
    lines = []
    for h in items:
        dt_planned = datetime.fromisoformat(h["planned_for"])
        done_at = datetime.fromisoformat(h["done_at"])
        lines.append(f"{fmt_dt_local(dt_planned)} — {h['text']} (выполнено: {fmt_dt_local(done_at)})")
    await update.message.reply_text("История последних дел:\n" + "\n".join(lines))

# основной обработчик текста (постановка напоминаний)
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    user_id = update.effective_user.id
    data = db_get()

    # Техработы
    if data.get("maintenance") and user_id != ADMIN_ID:
        # запомним, чтобы уведомить позже
        def mutate(d):
            d["pending_chats"].append(update.effective_chat.id)
        db_put(mutate)
        await msg.reply_text("🟡 Уважаемый пользователь, проводятся технические работы. Как только бот снова заработает — оповестим вас.")
        return

    # Приватный доступ
    if not user_allowed(user_id):
        if await try_accept_key(update):
            await start(update, context)
        else:
            await ask_key(update)
        return

    parsed = parse_request_ru(msg.text)
    if not parsed:
        await msg.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов ...\n"
            "— сегодня в HH:MM ...\n"
            "— завтра в HH:MM ...\n"
            "— каждый день в HH:MM ...\n"
            "— DD <месяц> [в HH:MM] ..."
        )
        return

    # Планирование
    q = context.job_queue
    chat_id = update.effective_chat.id

    # разовые через delta
    if "after" in parsed:
        run_at_local = now_local() + parsed["after"]
        job_name = f"once_{user_id}_{int(datetime.now().timestamp())}"
        q.run_once(
            remind_job,
            when=to_utc(run_at_local),              # datetime aware (UTC)
            name=job_name,
            data={"chat_id": chat_id, "text": parsed["text"], "planned_local_iso": run_at_local.isoformat()},
        )
        add_task(chat_id, run_at_local, parsed["text"], "once", job_name)
        await msg.reply_text(f"✅ Ок, напомню через {parsed['after']} — «{parsed['text']}».")
        return

    # разовые к конкретному моменту
    if "once_at" in parsed:
        run_at_local = parsed["once_at"]
        job_name = f"once_{user_id}_{int(datetime.now().timestamp())}"
        q.run_once(
            remind_job,
            when=to_utc(run_at_local),
            name=job_name,
            data={"chat_id": chat_id, "text": parsed["text"], "planned_local_iso": run_at_local.isoformat()},
        )
        add_task(chat_id, run_at_local, parsed["text"], "once", job_name)
        await msg.reply_text(f"✅ Ок, напомню {fmt_dt_local(run_at_local)} — «{parsed['text']}». (TZ: {TZ_NAME})")
        return

    # ежедневные
    if "daily_at" in parsed:
        t_local: time = parsed["daily_at"]
        hhmm = time(hour=t_local.hour, minute=t_local.minute, tzinfo=TZ)
        job_name = f"daily_{user_id}_{int(datetime.now().timestamp())}"

        # ближайший запуск (сегодня/завтра), затем — раз в сутки
        first = now_local().replace(hour=hhmm.hour, minute=hhmm.minute, second=0, microsecond=0)
        if first <= now_local():
            first += timedelta(days=1)

        async def daily_wrapper(ctx: ContextTypes.DEFAULT_TYPE):
            await remind_job(ctx)

        q.run_repeating(
            daily_wrapper,
            interval=timedelta(days=1),
            first=to_utc(first),
            name=job_name,
            data={"chat_id": chat_id, "text": parsed["text"], "planned_local_iso": first.isoformat()},
        )
        add_task(chat_id, first, parsed["text"], "daily", job_name)
        await msg.reply_text(f"✅ Ок, буду напоминать каждый день в {hhmm.strftime('%H:%M')} — «{parsed['text']}».")
        return

# ------------------------ Команды -----------------------

async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("history", "История выполненных дел"),
        BotCommand("maintenance_on", "Техработы: вкл (админ)"),
        BotCommand("maintenance_off", "Техработы: выкл (админ)"),
    ])

# ------------------------ Запуск ------------------------

def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # убедимся, что файл БД и ключи существуют
    db_put(lambda d: d)  # lazy init

    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", list_affairs))
    app.add_handler(CommandHandler("affairs_delete", delete_affair))
    app.add_handler(CommandHandler("history", show_history))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # меню команд
    app.post_init = lambda _: app.create_task(set_commands(app))

    log.info("Starting bot with polling...")
    app.run_polling(close_loop=False)

import asyncio

if __name__ == "__main__":
    asyncio.run(main())
