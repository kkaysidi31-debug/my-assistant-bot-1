import os
import re
import json
import logging
from datetime import datetime, timedelta, time as dtime

from pytz import timezone
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# -------------------- НАСТРОЙКИ --------------------
ADMIN_ID = 963586834  # твой id
TZ = timezone("Europe/Kaliningrad")
APP_URL = os.getenv("APP_URL", "").rstrip("/")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))

# Ключи доступа VIP001..VIP100
ACCESS_KEYS = {f"VIP{str(i).zfill(3)}": None for i in range(1, 101)}
ALLOWED_USERS = set()           # user_id, которые уже авторизовались
PENDING_CHATS = set()           # кто писал во время техработ
MAINTENANCE = False

DB_PATH = "db.json"             # файл "базы"

# Месяцы для русского парсинга
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("bot")

# -------------------- УТИЛИТЫ --------------------
def now_local() -> datetime:
    return datetime.now(TZ)

def load_db():
    """Читаем дела из файла."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # ключи json — строки, приведём id к str для единообразия
        return {str(k): v for k, v in data.items()}
    except Exception:
        return {}

def save_db():
    """Пишем дела в файл."""
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(DB, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("save_db error: %s", e)

# Структура DB: { str(user_id): [ {id, when_iso, text, periodic}, ... ] }
DB = load_db()

def add_task(uid: int, when_dt: datetime, text: str, periodic: bool, job_id: str):
    u = str(uid)
    DB.setdefault(u, [])
    DB[u].append({
        "id": job_id,
        "when_iso": when_dt.isoformat(),
        "text": text,
        "periodic": periodic
    })
    DB[u].sort(key=lambda x: x["when_iso"])
    save_db()

def remove_task(uid: int, job_id: str) -> bool:
    u = str(uid)
    if u not in DB:
        return False
    before = len(DB[u])
    DB[u] = [t for t in DB[u] if t["id"] != job_id]
    if len(DB[u]) != before:
        save_db()
        return True
    return False

def list_tasks(uid: int):
    return DB.get(str(uid), [])

# -------------------- ПАРСЕР РУССКОГО ТЕКСТА --------------------
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"
RE_NUM = r"(?P<n>\d{1,3})"
RE_MONTH = r"(?P<d>\d{1,2})\s+(?P<month>[а-я]+)"
RE_TODAY = rf"^(?:сегодня)\s+в\s+{RE_TIME}\s+(?P<text>.+)$"
RE_TMRW = rf"^(?:завтра)\s+в\s+{RE_TIME}\s+(?P<text>.+)$"
RE_AFTER = rf"^(?:через)\s+{RE_NUM}\s+(?P<unit>минут[уы]?|час[ауов]?)\s+(?P<text>.+)$"
RE_DAILY = rf"^(?:каждый\s+день)\s+в\s+{RE_TIME}\s+(?P<text>.+)$"
RE_DATE = rf"^{RE_MONTH}(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$"

def parse_text_to_schedule(t: str):
    """
    Возвращает кортеж:
      ("once_at", datetime, text)  — одноразовое
      ("daily_at", time, text)     — каждый день
    либо None если не распознано.
    """
    t = " ".join(t.split()).strip().lower()

    # через N минут/часов
    m = re.match(RE_AFTER, t)
    if m:
        n = int(m.group("n"))
        unit = m.group("unit")
        delta = timedelta(minutes=n) if unit.startswith("минут") else timedelta(hours=n)
        when = now_local() + delta
        return ("once_at", when.replace(second=0, microsecond=0), m.group("text").strip())

    # сегодня в HH:MM …
    m = re.match(RE_TODAY, t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return ("once_at", base, m.group("text").strip())

    # завтра в HH:MM …
    m = re.match(RE_TMRW, t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return ("once_at", base, m.group("text").strip())

    # каждый день в HH:MM …
    m = re.match(RE_DAILY, t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return ("daily_at", dtime(hh, mm, tzinfo=TZ), m.group("text").strip())

    # 30 августа [в 09:00] …
    m = re.match(RE_DATE, t)
    if m:
        d = int(m.group("d"))
        mon_name = m.group("month")
        mon = MONTHS.get(mon_name)
        if mon:
            hh = int(m.group("h")) if m.group("h") else 9
            mm = int(m.group("m")) if m.group("m") else 0
            y = now_local().year
            when = TZ.localize(datetime(y, mon, d, hh, mm))
            return ("once_at", when, m.group("text").strip())

    return None

# -------------------- ХЕНДЛЕРЫ --------------------
async def send_examples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча с Андреем\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• Сегодня в 14:00 (сигнал) напоминаю, встреча в 15:00 (само напоминание в 14:00)\n"
        f"(часовой пояс: {TZ})"
    )
    await update.message.reply_text(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await send_examples(update, context)

async def affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tasks = list_tasks(uid)
    if not tasks:
        await update.message.reply_text("Список дел пуст.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(tasks, 1):
        dt = datetime.fromisoformat(t["when_iso"])
        dt = TZ.normalize(dt.astimezone(TZ))
        lines.append(f"{i}. {dt:%d.%m.%Y %H:%M} — {t['text']}")
    await update.message.reply_text("\n".join(lines))

async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажите номер: /affairs_delete N")
        return
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом.")
        return

    user_tasks = list_tasks(uid)
    if not (1 <= num <= len(user_tasks)):
        await update.message.reply_text("Неверный номер.")
        return

    # снимаем job и удаляем из DB
    job_id = user_tasks[num - 1]["id"]
    for j in context.job_queue.get_jobs_by_name(job_id):
        j.schedule_removal()
    ok = remove_task(uid, job_id)
    await update.message.reply_text("Удалено ✅" if ok else "Не найдено.")

# ---- Техработы (только админ) ----
async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    global MAINTENANCE
    MAINTENANCE = True
    await update.message.reply_text("🟡 Технические работы включены.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    global MAINTENANCE
    MAINTENANCE = False
    await update.message.reply_text("🟢 Технические работы выключены.")
    # уведомим ожидавших
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# ---- Приход напоминания ----
async def remind_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data  # {uid, text, periodic}
    uid = data["uid"]
    txt = data["text"]
    await context.bot.send_message(uid, f"⏰ Напоминаю: «{txt}»")

    # если одноразовое — убрать из DB
    if not data.get("periodic"):
        remove_task(uid, context.job.name)

# ---- Тексты/ключи/логика ----
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE

    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1) Авторизация по ключу
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", msg):
            if msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
                ACCESS_KEYS[msg] = uid
                ALLOWED_USERS.add(uid)
                save_db()
                await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
                await send_examples(update, context)
            else:
                await update.message.reply_text("Ключ недействителен.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # 2) Техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("🟡 Уважаемый пользователь! Сейчас ведутся технические работы. Сообщим как только бот снова заработает.")
        return

    # 3) Парсинг
    parsed = parse_text_to_schedule(msg)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Используй:\n"
            "— через N минут/часов …\n"
            "— сегодня в HH:MM …\n"
            "— завтра в HH:MM …\n"
            "— каждый день в HH:MM …\n"
            "— DD <месяц> [в HH:MM] …"
        )
        return

    kind, target, text = parsed

    # 4) Планирование
    if kind == "once_at":
        when = target
        job_id = f"once:{uid}:{int(when.timestamp())}"
        context.job_queue.run_once(
            remind_job,
            when - now_local(),
            name=job_id,
            data={"uid": uid, "text": text, "periodic": False},
            chat_id=uid,
        )
        add_task(uid, when, text, False, job_id)
        await update.message.reply_text(f"✅ Ок, напомню {when:%Y-%m-%d %H:%M} — «{text}». (TZ: {TZ})")

    else:  # daily_at
        t: dtime = target
        # ближайшее срабатывание
        now = now_local()
        first = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if first <= now:
            first += timedelta(days=1)

        job_id = f"daily:{uid}:{t.hour:02d}{t.minute:02d}"
        context.job_queue.run_daily(
            remind_job,
            time=t,
            name=job_id,
            data={"uid": uid, "text": text, "periodic": True},
            chat_id=uid,
            first=first
        )
        add_task(uid, first, text, True, job_id)
        await update.message.reply_text(f"✅ Ежедневно в {t.hour:02d}:{t.minute:02d} — «{text}».")

# -------------------- ИНИЦИАЛИЗАЦИЯ --------------------
async def set_commands(app: Application):
    cmds = [
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("maintenance_on", "Включить техработы (админ)"),
        BotCommand("maintenance_off", "Выключить техработы (админ)"),
    ]
    try:
        await app.bot.set_my_commands(cmds)
    except Exception as e:
        log.warning("set_my_commands warn: %s", e)

def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # Тексты
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app

async def post_init(app: Application):
    await set_commands(app)

def main():
    app = build_application()
    app.post_init = post_init  # зарегистрируем команды после старта

    # Webhook
    if not APP_URL:
        raise RuntimeError("APP_URL не задан. Укажи внешний URL Render, напр.: https://<service>.onrender.com")

    webhook_path = f"/{BOT_TOKEN}"
    webhook_url = f"{APP_URL}{webhook_path}"

    log.info("Starting webhook on port %s", PORT)
    # PTB сам поднямет aiohttp-сервер и привяжется к PORT
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
