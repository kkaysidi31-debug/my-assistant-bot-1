import os
import re
import json
import uuid
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List

import pytz
from aiohttp import web
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------- Конфиг ----------
ADMIN_ID = 963586834                      # твой ID
TIMEZONE = pytz.timezone("Europe/Kaliningrad")
DB_FILE = "db.json"

# Ключи доступа VIP001..VIP100
ACCESS_KEYS = [f"VIP{str(i).zfill(3)}" for i in range(1, 101)]
ALLOWED_USERS = set()
USED_KEYS = set()

MAINTENANCE = False
PENDING_CHATS = set()  # кому отправить "бот снова работает"

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reminder-bot")

# ---------- Хранилище ----------
def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_FILE):
        return {"allowed_users": [], "used_keys": [], "tasks": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: Dict[str, Any]) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def boot_db():
    db = load_db()
    ALLOWED_USERS.update(db.get("allowed_users", []))
    USED_KEYS.update(db.get("used_keys", []))
    return db

DB = boot_db()

def persist_users():
    DB["allowed_users"] = sorted(list(ALLOWED_USERS))
    DB["used_keys"] = sorted(list(USED_KEYS))
    save_db(DB)

def user_tasks(uid: int) -> List[Dict[str, Any]]:
    DB.setdefault("tasks", {})
    return DB["tasks"].setdefault(str(uid), [])

def add_task(uid: int, task: Dict[str, Any]):
    tasks = user_tasks(uid)
    tasks.append(task)
    save_db(DB)

def remove_task(uid: int, job_id: str) -> bool:
    tasks = user_tasks(uid)
    before = len(tasks)
    DB["tasks"][str(uid)] = [t for t in tasks if t["job_id"] != job_id]
    save_db(DB)
    return len(DB["tasks"][str(uid)]) < before

# ---------- Утилиты времени ----------
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "июнь": 6, "июль": 7,
    "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}

RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_text(txt: str) -> Dict[str, Any] | None:
    """
    Возвращает:
      {"kind":"in","delta":timedelta,"text":...}
      {"kind":"today","dt":datetime,"text":...}
      {"kind":"tomorrow","dt":datetime,"text":...}
      {"kind":"daily","t":datetime.time,"text":...}
      {"kind":"date","dt":datetime,"text":...}
    """
    t = " ".join(txt.split()).lower()

    # 1) через N минут/часов ...
    m = re.match(r"^через\s+(?P<n>\d+)\s*(минут(?:ы)?|мин|час(?:а|ов)?)\s+(?P<text>.+)$", t)
    if m:
        n = int(m.group("n"))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"kind": "in", "delta": delta, "text": m.group("text").strip()}

    # 2) сегодня в HH:MM ...
    m = re.match(rf"^сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"kind": "today", "dt": base, "text": m.group("text").strip()}

    # 3) завтра в HH:MM ...
    m = re.match(rf"^завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return {"kind": "tomorrow", "dt": base, "text": m.group("text").strip()}

    # 4) каждый день в HH:MM ...
    m = re.match(rf"^каждый\s+день\s+в\s+{RE_TIME}\s*(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return {"kind": "daily", "t": dtime(hh, mm, tzinfo=TIMEZONE), "text": m.group("text").strip()}

    # 5) "30 августа [в 09:00] ..."
    m = re.match(
        rf"^(?P<d>\d{{1,2}})\s+(?P<mon>[а-я]+)(?:\s+в\s+{RE_TIME})?\s*(?P<text>.+)$",
        t
    )
    if m and m.group("mon") in MONTHS:
        day = int(m.group("d"))
        mon = MONTHS[m.group("mon")]
        hh = int(m.group("h")) if m.group("h") else 9
        mm = int(m.group("m")) if m.group("m") else 0
        year = now_local().year
        dt = TIMEZONE.localize(datetime(year, mon, day, hh, mm, 0))
        return {"kind": "date", "dt": dt, "text": m.group("text").strip()}

    return None

# ---------- Напоминалка ----------
async def remind_callback(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = ctx.job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "Напоминание")
    try:
        await ctx.bot.send_message(chat_id, f"⏰ {text}")
    finally:
        # для разовых задач удаляем из БД
        if data.get("one_time") and chat_id and ctx.job:
            remove_task(chat_id, ctx.job.name)

# ---------- Команды ----------
HELP_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Сегодня в 14:00 (сигнал) напоминаю, встреча в 15:00 (само напоминание в 15:00)\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("maintenance_on", "Техработы: включить (админ)"),
        BotCommand("maintenance_off", "Техработы: выключить (админ)"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await update.message.reply_text(HELP_TEXT)

async def affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tasks = user_tasks(uid)
    if not tasks:
        await update.message.reply_text("Пока пусто ✨")
        return
    # сортировка: сначала ближайший
    def _key(t):
        if t["type"] == "daily":
            return ("1", t["time"])  # daily после разовых
        return ("0", t["when"])
    tasks_sorted = sorted(tasks, key=_key)
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(tasks_sorted, 1):
        if t["type"] == "daily":
            lines.append(f"{i}. каждый день в {t['time']} — {t['text']}")
        else:
            lines.append(f"{i}. {t['when']} — {t['text']}")
    await update.message.reply_text("\n".join(lines))

async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажите номер: /affairs_delete 2")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом.")
        return

    tasks_sorted = sorted(user_tasks(uid), key=lambda t: (t["type"] != "once", t.get("when","9999")))
    if not (1 <= n <= len(tasks_sorted)):
        await update.message.reply_text("Нет задачи с таким номером.")
        return

    task = tasks_sorted[n-1]
    job_id = task["job_id"]
    # попытка отменить
    job = context.job_queue.get_jobs_by_name(job_id)
    for j in job:
        j.schedule_removal()
    removed = remove_task(uid, job_id)
    await update.message.reply_text("Удалено ✅" if removed else "Не нашёл такую задачу.")

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE   # перенесли наверх
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    MAINTENANCE = True
    await update.message.reply_text("🟡 Технические работы включены.")


async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE   # тоже наверх
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    MAINTENANCE = False
    await update.message.reply_text("🟢 Технические работы выключены.")

    # уведомим ожидавших
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает, можно продолжать.")
        except Exception:
            pass
            
# ---------- Голосовые ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(update.effective_chat.id)
        await update.message.reply_text("🟡 Ведутся техработы. Сообщим, когда бот будет доступен.")
        return
    if not OPENAI_API_KEY:
        await update.message.reply_text("Для распознавания речи не задан OPENAI_API_KEY.")
        return

    try:
        file = await update.message.voice.get_file()
        path = f"/tmp/{uuid.uuid4()}.oga"
        await file.download_to_drive(path)

        # OpenAI (whisper-1)
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                language="ru"
            )
        text = tr.strip()
        if not text:
            await update.message.reply_text("Не удалось распознать голосовое.")
            return
        # прогоняем как обычный текст
        update.message.text = text
        await handle_key_or_text(update, context)
    except Exception as e:
        log.exception("voice error: %s", e)
        await update.message.reply_text("Ошибка распознавания голосового.")

# ---------- Обработчик текста/ключей ----------
async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1) если не авторизован — пробуем ключ
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", msg) and msg in ACCESS_KEYS and msg not in USED_KEYS:
            USED_KEYS.add(msg)
            ALLOWED_USERS.add(uid)
            persist_users()
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
            await update.message.reply_text(HELP_TEXT)
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # 2) техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("🟡 Уважаемый пользователь, ведутся техработы. Напишем, когда бот снова заработает.")
        return

    # 3) парсинг естественного языка
    parsed = parse_text(msg)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял. Примеры:\n"
            "— через 5 минут попить воды\n"
            "— сегодня в 16:00 купить молоко\n"
            "— завтра в 9:15 встреча с Андреем\n"
            "— каждый день в 09:30 зарядка\n"
            "— 30 августа в 09:00 заплатить за кредит"
        )
        return

    jq = context.job_queue
    text = parsed["text"]
    job_id = f"job-{uuid.uuid4().hex}"

    if parsed["kind"] == "in":
        when = now_local() + parsed["delta"]
        await jq.run_once(
            remind_callback, when=when,
            data={"chat_id": chat_id, "text": text, "one_time": True},
            name=job_id
        )
        add_task(uid, {"type": "once", "when": when.strftime("%Y-%m-%d %H:%M"), "text": text, "job_id": job_id})
        await update.message.reply_text(f"✅ Ок, напомню через {parsed['delta']} — «{text}».")
        return

    if parsed["kind"] in ("today", "tomorrow", "date"):
        when = parsed["dt"]
        await jq.run_once(
            remind_callback, when=when,
            data={"chat_id": chat_id, "text": text, "one_time": True},
            name=job_id
        )
        add_task(uid, {"type": "once", "when": when.strftime("%Y-%m-%d %H:%M"), "text": text, "job_id": job_id})
        await update.message.reply_text(f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{text}». (TZ: {TIMEZONE})")
        return

    if parsed["kind"] == "daily":
        t: dtime = parsed["t"]
        await jq.run_daily(
            remind_callback, time=t,
            data={"chat_id": chat_id, "text": text, "one_time": False},
            name=job_id
        )
        add_task(uid, {"type": "daily", "time": t.strftime('%H:%M'), "text": text, "job_id": job_id})
        await update.message.reply_text(f"✅ Ежедневно в {t.strftime('%H:%M')} — «{text}».")
        return

# ---------- Сборка приложения ----------
def build_application() -> Application:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    app = Application.builder().token(token).post_init(set_commands).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    return app

# ---------- Webhook ----------
def main():
    app = build_application()

    # aiohttp-приложение для health-check
    aio = web.Application()
    async def health(_: web.Request):
        return web.Response(text="OK")
    aio.router.add_get("/", health)
    aio.router.add_get("/healthz", health)

    port = int(os.getenv("PORT", "10000"))
    public_url = os.getenv("WEBHOOK_URL", "").rstrip("/")
    if not public_url:
        raise SystemExit("Нет переменной окружения WEBHOOK_URL (https://<твой-сервис>.onrender.com)")

    # Telegram будет бить в корень "/", Render терминирует TLS ↔️ наш HTTP
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="",                        # слушаем POST на /
        webhook_url=public_url,             # внешний https-URL сервиса
        web_app=aio,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
