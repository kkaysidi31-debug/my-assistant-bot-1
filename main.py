# -*- coding: utf-8 -*-
import os
import re
import json
import logging
import threading
from io import BytesIO
from datetime import datetime, timedelta, time, timezone as dt_timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from flask import Flask, Response

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    TypeHandler, ApplicationHandlerStop, filters
)

# =============== ЛОГИ ===============
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("assistant-bot")

# =============== НАСТРОЙКИ ===============
TIMEZONE = ZoneInfo("Europe/Kaliningrad")
PORT = int(os.getenv("PORT", "10000"))

# Админ (твой ID)
ADMIN_IDS = {963586834}

# Файлы-памяти (чтобы после перезапуска не терять ключи/дела/ожидающих)
DATA_DIR = Path(".")
KEYS_FILE = DATA_DIR / "access_keys.json"   # { "VIP001": user_id|null, ... }
TASKS_FILE = DATA_DIR / "tasks.json"        # { str(user_id): [ {kind, text, due_iso|hh:mm, job_name}, ... ] }
PENDING_FILE = DATA_DIR / "pending_chats.json"  # [ chat_id, ... ]

# Приватные ключи (100 одноразовых)
def _default_keys() -> dict[str, int | None]:
    return {f"VIP{n:03d}": None for n in range(1, 101)}

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default() if callable(default) else default

def _save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Save %s failed: %s", path, e)

ACCESS_KEYS: dict[str, int | None] = _load_json(KEYS_FILE, _default_keys)
ALLOWED_USERS: set[int] = {uid for uid in ACCESS_KEYS.values() if isinstance(uid, int)}
TASKS: dict[str, list[dict]] = _load_json(TASKS_FILE, dict)
PENDING_CHATS: set[int] = set(_load_json(PENDING_FILE, list))

def save_keys(): _save_json(KEYS_FILE, ACCESS_KEYS)
def save_tasks(): _save_json(TASKS_FILE, TASKS)
def save_pending(): _save_json(PENDING_FILE, list(PENDING_CHATS))

# Режим техработ
MAINTENANCE = False

# =============== ВСПОМОГАТЕЛЬНЫЕ ===============
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")

def to_utc(dt_local: datetime) -> datetime:
    # для run_once в PTB20 можно передать aware-datetime в UTC
    return dt_local.astimezone(dt_timezone.utc)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# =============== МЕСЯЦЫ (ru) ===============
MONTHS = {
    "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
    "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
    "январь":1,"февраль":2,"март":3,"апрель":4,"май":5,"июнь":6,"июль":7,
    "август":8,"сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12,
}

RE_TIME = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"

# =============== ПАРСЕР ТЕКСТА ===============
def parse_user_text(t: str):
    """
    Возвращает один из dict:
      {"after": timedelta, "text": "..."}
      {"once_at": datetime (aware local), "text": "..."}
      {"daily_at": time (aware local), "text": "..."}
      или None (если формат не распознан)
    """
    s = t.strip().lower().replace("ё", "е")

    # через N минут/часов …
    m = re.match(r"^через\s+(\d{1,3})\s*(минут(?:ы)?|мин|час(?:а|ов)?)\s+(.+)$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"after": delta, "text": m.group(3).strip()}

    # сегодня в HH:MM …
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(.+)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now_local():
            dt += timedelta(days=1)
        return {"once_at": dt, "text": m.group(3) if m.lastindex and m.lastindex >= 3 else s}

    # завтра в HH:MM …
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(.+)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return {"once_at": dt, "text": m.group(3) if m.lastindex and m.lastindex >= 3 else s}

    # каждый день в HH:MM …
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s*(.*)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        text = (m.group(3) or "").strip() or "Ежедневное напоминание"
        return {"daily_at": time(hh, mm, tzinfo=TIMEZONE), "text": text}

    # короткий вариант: "в HH:MM ..."
    m = re.match(rf"^в\s+{RE_TIME}\s+(.+)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now_local():
            dt += timedelta(days=1)
        return {"once_at": dt, "text": m.group(3)}

    # DD <месяц> [в HH:MM] ...
    m = re.match(rf"^(?P<d>\d{{1,2}})\s+(?P<mon>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<txt>.+)$", s)
    if m:
        day = int(m.group("d"))
        mon_name = m.group("mon")
        mon = MONTHS.get(mon_name)
        if mon:
            hh = int(m.group("h")) if m.group("h") else 9
            mm = int(m.group("m")) if m.group("m") else 0
            dt = datetime(now_local().year, mon, day, hh, mm, tzinfo=TIMEZONE)
            if dt <= now_local():
                dt = datetime(now_local().year + 1, mon, day, hh, mm, tzinfo=TIMEZONE)
            return {"once_at": dt, "text": m.group("txt").strip()}

    return None

# =============== ХРАНЕНИЕ ДЕЛ ===============
def list_tasks(uid: int) -> list[dict]:
    return TASKS.get(str(uid), [])

def add_task(uid: int, task: dict):
    lst = TASKS.get(str(uid), [])
    lst.append(task)
    # сортируем по due_iso (однократные) и по времени (ежедневные)
    def _key(x):
        return (0, x.get("due_iso", "9999-12-31T23:59:59")) if x["kind"] == "once" else (1, x.get("time","99:99"))
    lst.sort(key=_key)
    TASKS[str(uid)] = lst
    save_tasks()

def remove_task_by_index(uid: int, idx: int) -> dict | None:
    lst = TASKS.get(str(uid), [])
    if 1 <= idx <= len(lst):
        item = lst.pop(idx - 1)
        TASKS[str(uid)] = lst
        save_tasks()
        return item
    return None

# =============== ГОЛОС (опционально) ===============
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client ready (voice -> text)")
    except Exception as e:
        log.warning("OpenAI init failed: %s", e)
        openai_client = None

async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if not openai_client:
        await update.message.reply_text("Распознавание голоса недоступно (нет OPENAI_API_KEY).")
        return None
    try:
        f = await context.bot.get_file(update.message.voice.file_id)
        mem = BytesIO()
        await f.download_to_memory(out=mem)
        mem.seek(0)
        # можно "whisper-1" или "gpt-4o-mini-transcribe"
        res = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", mem, "audio/ogg"),
            response_format="text",
            language="ru"
        )
        text = (res or "").strip()
        return text if text else None
    except Exception as e:
        log.exception("Transcribe error: %s", e)
        await update.message.reply_text("Не удалось распознать голос.")
        return None

# =============== JOB CALLBACK ===============
async def remind_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    cid = data.get("chat_id")
    txt = data.get("text", "Напоминание")
    try:
        await context.bot.send_message(cid, f"⏰ {txt}")
    except Exception as e:
        log.warning("send msg failed: %s", e)

# =============== КОМАНДЫ ===============
HELP_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Напоминание за какое либо кол-во времени пишите так(Пример напоминания за 1 час): Сегодня в 14:00(Сигнал для бота - в какое время уведомить) напоминаю, встреча в 15:00(Это само напоминание которое бот отправит вам в указанное время - в данном случае в 14:00) Так можно делать с любой датой\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    # Поставим меню команд (один раз на пользователя — ок)
    try:
        await context.bot.set_my_commands([
            BotCommand("start", "Помощь и примеры"),
            BotCommand("affairs", "Список дел"),
            BotCommand("affairs_delete", "Удалить дело по номеру"),
            BotCommand("maintenance_on", "🔧 (админ) Включить техработы"),
            BotCommand("maintenance_off", "🔧 (админ) Выключить техработы"),
        ])
    except Exception:
        pass
    await update.message.reply_text(HELP_TEXT)

async def cmd_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    items = list_tasks(uid)
    if not items:
        await update.message.reply_text("Список пуст.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, it in enumerate(items, 1):
        if it["kind"] == "once":
            dt = datetime.fromisoformat(it["due_iso"]).astimezone(TIMEZONE)
            lines.append(f"{i}. {fmt_dt(dt)} — {it['text']}")
        else:
            lines.append(f"{i}. {it['time']} — {it['text']} (ежедневно)")
    await update.message.reply_text("\n".join(lines))

async def cmd_affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /affairs_delete N")
        return
    n = int(context.args[0])
    items = list_tasks(uid)
    if not (1 <= n <= len(items)):
        await update.message.reply_text("Нет такого номера.")
        return
    # Попробуем снять job (если имя сохранили)
    job_name = items[n-1].get("job_name")
    if job_name:
        for j in context.application.job_queue.get_jobs_by_name(job_name):
            j.schedule_removal()
    removed = remove_task_by_index(uid, n)
    if removed:
        if removed["kind"] == "once":
            await update.message.reply_text(f"🗑 Удалено: {fmt_dt(datetime.fromisoformat(removed['due_iso']))} — {removed['text']}")
        else:
            await update.message.reply_text(f"🗑 Удалено: каждый день {removed['time']} — {removed['text']}")
    else:
        await update.message.reply_text("Не удалось удалить (возможно, уже выполнено).")

# Техработы
async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Только администратор может включать техработы.")
        return
    MAINTENANCE = True
    PENDING_CHATS.clear(); save_pending()
    await update.message.reply_text("🟡 Технические работы включены. Бот временно не принимает задачи.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⚠️ Только администратор может выключать техработы.")
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Технические работы завершены. Рассылаю уведомления…")
    to_notify = list(PENDING_CHATS)
    PENDING_CHATS.clear(); save_pending()
    for cid in to_notify:
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# =============== ОБРАБОТЧИКИ СООБЩЕНИЙ ===============
async def maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный шлагбаум: во время техработ пропускаем только админа и команды техработ."""
    if not MAINTENANCE:
        return
    uid = update.effective_user.id if update.effective_user else None
    # Админу даём проход
    if uid in ADMIN_IDS:
        return
    # Остальным: жёлтое предупреждение и запомнить чат
    cid = update.effective_chat.id if update.effective_chat else None
    if cid and cid not in PENDING_CHATS:
        PENDING_CHATS.add(cid); save_pending()
    # Ответить только на текст/голос (чтобы не спамить на каждое обновление)
    try:
        if update.message:
            await context.bot.send_message(cid,
                "🟡 Уважаемый пользователь! Сейчас ведутся технические работы. "
                "Как только бот снова заработает, мы сообщим вам."
            )
    except Exception:
        pass
    raise ApplicationHandlerStop

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Приватный доступ: принимаем ключ в формате ABC123, но реальные — VIPxxx
    if uid not in ALLOWED_USERS:
        candidate = text.upper()
        if re.fullmatch(r"[A-Z]{3}\d{3}", candidate) and candidate in ACCESS_KEYS and ACCESS_KEYS[candidate] is None:
            ACCESS_KEYS[candidate] = uid
            ALLOWED_USERS.add(uid)
            save_keys()
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.\n\n" + HELP_TEXT)
            return
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    parsed = parse_user_text(text)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Примеры:\n"
            "— сегодня в 16:00 купить молоко\n"
            "— завтра в 9:15 встреча\n"
            "— в 22:30 позвонить маме\n"
            "— через 5 минут попить воды\n"
            "— каждый день в 09:30 зарядка\n"
            "— 30 августа в 09:00 заплатить за кредит"
        )
        return

    chat_id = update.effective_chat.id

    # 1) через N …
    if "after" in parsed:
        delta: timedelta = parsed["after"]
        job = context.application.job_queue.run_once(
            remind_callback, when=delta, chat_id=chat_id, data={"chat_id": chat_id, "text": parsed["text"]}
        )
        due = now_local() + delta
        add_task(uid, {"kind":"once","text":parsed["text"],"due_iso":due.isoformat(), "job_name": job.name})
        await update.message.reply_text(f"✅ Отлично, напомню {fmt_dt(due)} — «{parsed['text']}».")
        return

    # 2) один раз в момент
    if "once_at" in parsed:
        local_dt: datetime = parsed["once_at"]
        job = context.application.job_queue.run_once(
            remind_callback, when=to_utc(local_dt), chat_id=chat_id, data={"chat_id": chat_id, "text": parsed["text"]}
        )
        add_task(uid, {"kind":"once","text":parsed["text"],"due_iso":local_dt.isoformat(), "job_name": job.name})
        await update.message.reply_text(f"✅ Отлично, напомню {fmt_dt(local_dt)} — «{parsed['text']}».")
        return

    # 3) каждый день в HH:MM
    if "daily_at" in parsed:
        tm: time = parsed["daily_at"]  # с tzinfo=TIMEZONE
        job = context.application.job_queue.run_daily(
            remind_callback, time=tm, chat_id=chat_id, data={"chat_id": chat_id, "text": parsed["text"]}
        )
        add_task(uid, {"kind":"daily","text":parsed["text"],"time":tm.strftime("%H:%M"), "job_name": job.name})
        await update.message.reply_text(f"✅ Отлично, буду напоминать каждый день в {tm.strftime('%H:%M')} — «{parsed['text']}».")
        return

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пропускаем через распознавание и дальше — как текст
    txt = await transcribe_voice(update, context)
    if not txt:
        return
    update.message.text = txt
    await handle_text(update, context)

# =============== Мини-HTTP (Flask) для Render/UptimeRobot ===============
def run_flask():
    app = Flask(__name__)

    @app.get("/")
    def root():
        return Response("✅ Bot is running", mimetype="text/plain", status=200)

    app.run(host="0.0.0.0", port=PORT, debug=False)

# =============== MAIN ===============
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # поднимем HTTP в фоне
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()

    # Глобальный «шлагбаум» техработ — раньше всех
    application.add_handler(TypeHandler(Update, maintenance_guard), group=-100)

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("affairs", cmd_affairs))
    application.add_handler(CommandHandler("affairs_delete", cmd_affairs_delete))
    application.add_handler(CommandHandler("maintenance_on", maintenance_on))
    application.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # Голосовые
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice))
    # Текст
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Starting bot with polling …")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
