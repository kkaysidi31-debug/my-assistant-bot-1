# main.py
import os
import re
import json
import threading
import logging
from pathlib import Path
from datetime import datetime, timedelta, time

from zoneinfo import ZoneInfo
from flask import Flask, Response

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===== Логирование =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("assistant-bot")

# ===== Часовой пояс =====
TZ = ZoneInfo("Europe/Kaliningrad")

# ===== Путь к файлам-памяти =====
REM_FILE = Path("reminders.json")
KEYS_FILE = Path("access_keys.json")
PENDING_FILE = Path("pending_notify.json")

# ===== Режим обслуживания =====
MAINTENANCE = os.getenv("MAINTENANCE", "0") == "1"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# ===== Приватные ключи =====
def _default_keys():
    # 100 одноразовых ключей VIP001..VIP100
    return {f"VIP{n:03d}": None for n in range(1, 101)}

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default() if callable(default) else default
    return default() if callable(default) else default

def save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Can't save %s: %s", path, e)

ACCESS_KEYS: dict[str, int | None] = load_json(KEYS_FILE, _default_keys)   # value = user_id, если использован
ALLOWED_USERS: set[int] = {uid for uid in ACCESS_KEYS.values() if isinstance(uid, int)}

# ===== Список “ожидающих оповещения” во время техработ =====
PENDING_CHATS: set[int] = set(load_json(PENDING_FILE, []))

def save_pending():
    save_json(PENDING_FILE, list(PENDING_CHATS))

# ===== Напоминания (память) =====
# Структура: {str(chat_id): [{"id": str(job_id), "when": iso, "text": "..."}]}
REMINDERS: dict[str, list[dict]] = load_json(REM_FILE, {})

def persist_reminders():
    save_json(REM_FILE, REMINDERS)

# ===== Месяцы на русском =====
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "июнь": 6, "июль": 7,
    "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}

TIME_RE = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"

# ====== ВСПОМОГАТЕЛЬНОЕ ======
def now_local() -> datetime:
    return datetime.now(TZ)

def ensure_auth(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USERS)

async def ask_key(update: Update):
    await update.message.reply_text(
        "Бот приватный. Введите ключ доступа в формате ABC123."
    )

def append_reminder(chat_id: int, job_id: str, when: datetime, text: str):
    lst = REMINDERS.setdefault(str(chat_id), [])
    lst.append({"id": job_id, "when": when.isoformat(), "text": text})
    # сортировка по времени
    lst.sort(key=lambda x: x["when"])
    persist_reminders()

def remove_reminder_by_index(chat_id: int, idx: int) -> bool:
    lst = REMINDERS.get(str(chat_id), [])
    if 1 <= idx <= len(lst):
        item = lst.pop(idx - 1)
        persist_reminders()
        return True
    return False

# ====== РЕЖИМ ОБСЛУЖИВАНИЯ ======
async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id not in ADMIN_IDS:
        return
    MAINTENANCE = True
    await update.message.reply_text("⚠️ Режим обслуживания включён.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE, PENDING_CHATS
    if update.effective_user.id not in ADMIN_IDS:
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Режим обслуживания выключен. Уведомляю пользователей…")
    to_notify = list(PENDING_CHATS)
    PENDING_CHATS.clear()
    save_pending()
    for cid in to_notify:
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# ====== /start ======
START_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Напоминание за какое либо кол-во времени пишите так(Пример напоминания за 1 час): Сегодня в 14:00(Сигнал для бота - в какое время уведомить) напоминаю, встреча в 15:00(Это само напоминание которое бот отправит вам в указанное время - в данном случае в 14:00) Так можно делать с любой датой \n"    
    f"(часовой пояс: {TZ.key})"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_auth(update):
        await ask_key(update)
        return
    await update.message.reply_text(START_TEXT)

# ====== /affairs (список дел) ======
async def cmd_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_auth(update):
        await ask_key(update)
        return
    lst = REMINDERS.get(str(update.effective_chat.id), [])
    if not lst:
        await update.message.reply_text("Список пуст.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, it in enumerate(lst, 1):
        when = datetime.fromisoformat(it["when"]).astimezone(TZ)
        lines.append(f"{i}. {when:%d.%m.%Y %H:%M} — {it['text']}")
    await update.message.reply_text("\n".join(lines))

# ====== /affairs_delete N ======
async def cmd_affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_auth(update):
        await ask_key(update)
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажи номер: /affairs_delete 3")
        return
    idx = int(args[0])
    ok = remove_reminder_by_index(update.effective_chat.id, idx)
    if ok:
        await update.message.reply_text(f"✅ Дело №{idx} удалено из списка (если было запланировано — отменится при запуске).")
    else:
        await update.message.reply_text("Не нашёл дело с таким номером.")

# ====== Приватные ключи ======
async def handle_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Пробует принять ключ. Возвращает True, если это был ключ."""
    text = (update.message.text or "").strip()
    if not re.fullmatch(r"[A-Z]{3}\d{3}", text):
        return False
    if text in ACCESS_KEYS and ACCESS_KEYS[text] is None:
        user_id = update.effective_user.id
        ACCESS_KEYS[text] = user_id
        ALLOWED_USERS.add(user_id)
        save_json(KEYS_FILE, ACCESS_KEYS)
        await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.\n\n" + START_TEXT)
    else:
        await update.message.reply_text("Ключ недействителен или уже использован.")
    return True

# ====== Голосовые сообщения (OpenAI Whisper) ======
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    openai_client = None

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_auth(update):
        await ask_key(update)
        return
    if MAINTENANCE:
        cid = update.effective_chat.id
        PENDING_CHATS.add(cid)
        save_pending()
        await update.message.reply_text(
            "⚠️ Уважаемый пользователь!\n"
            "Сейчас ведутся технические работы. Как только бот снова заработает, мы сообщим вам."
        )
        return

    if not openai_client:
        await update.message.reply_text("Не настроен распознаватель (нет OPENAI_API_KEY).")
        return

    file = await context.bot.get_file(update.message.voice.file_id)
    ogg_path = f"/tmp/{file.file_id}.oga"
    await file.download_to_drive(ogg_path)

    try:
        with open(ogg_path, "rb") as f:
            tr = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                language="ru"
            )
        text = tr.strip()
        if not text:
            await update.message.reply_text("Не удалось распознать голос.")
            return
        # отправляем распознанный текст в общий обработчик
        fake_update = update
        fake_update.message.text = text
        await handle_text(fake_update, context)
    except Exception as e:
        log.exception("Whisper error: %s", e)
        await update.message.reply_text("Ошибка распознавания. Попробуйте ещё раз.")

# ====== Планирование напоминаний ======
async def remind_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "дело")
    try:
        await context.bot.send_message(chat_id, f"⏰ Напоминание: {text}")
    except Exception:
        pass

def schedule_once(app: Application, chat_id: int, when: datetime, text: str) -> str:
    job = app.job_queue.run_once(
        remind_callback,
        when.astimezone(TZ),
        chat_id=chat_id,
        name=f"{chat_id}:{when.isoformat()}",
        data={"chat_id": chat_id, "text": text},
        tzinfo=TZ
    )
    return job.name

def schedule_daily(app: Application, chat_id: int, at: time, text: str) -> str:
    job = app.job_queue.run_daily(
        remind_callback,
        at,
        chat_id=chat_id,
        name=f"{chat_id}:daily:{at.strftime('%H:%M')}-{text}",
        data={"chat_id": chat_id, "text": text},
        tzinfo=TZ
    )
    return job.name

# ====== Парсер фраз ======
def parse_user_text(t: str):
    """Возвращает dict типа:
       {"after": timedelta, "text": ...}
       или {"once_at": datetime, "text": ...}
       или {"daily_at": time, "text": ...}
    """
    s = t.strip().lower()

    # 1) через N минут/часов ...
    m = re.match(r"^через\s+(\d+)\s*(минут[уы]?|мин|час[аов]?)\s+(?P<text>.+)$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        return {"after": delta, "text": m.group("text").strip()}

    # 2) сегодня в HH:MM ...
    m = re.match(rf"^сегодня\s+в\s+{TIME_RE}\s+(?P<text>.+)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt < now_local():
            dt += timedelta(days=1)
        return {"once_at": dt, "text": m.group("text").strip()}

    # 3) завтра в HH:MM ...
    m = re.match(rf"^завтра\s+в\s+{TIME_RE}\s+(?P<text>.+)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return {"once_at": dt, "text": m.group("text").strip()}

    # 4) каждый день в HH:MM ...
    m = re.match(rf"^каждый\s+день\s+в\s+{TIME_RE}\s+(?P<text>.+)$", s)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return {"daily_at": time(hh, mm, tzinfo=TZ), "text": m.group("text").strip()}

    # 5) DD <месяц> [в HH:MM] ...
    m = re.match(
        rf"^(?P<d>\d{{1,2}})\s+(?P<mname>[а-я]+)(?:\s+в\s+{TIME_RE})?\s+(?P<text>.+)$", s
    )
    if m:
        d = int(m.group("d"))
        mname = m.group("mname")
        month = MONTHS.get(mname)
        text = m.group("text").strip()
        if month:
            year = now_local().year
            hh = int(m.group("h")) if m.group("h") else 9
            mm = int(m.group("m")) if m.group("m") else 0
            dt = datetime(year, month, d, hh, mm, tzinfo=TZ)
            if dt < now_local():
                # если дата в прошлом — считаем следующий год
                dt = datetime(year + 1, month, d, hh, mm, tzinfo=TZ)
            return {"once_at": dt, "text": text}

    return None

# ====== Обработчик текста ======
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # приватность
    if not ensure_auth(update):
        # попытка принять ключ
        if await handle_key(update, context):
            return
        await ask_key(update)
        return

    # техработы
    if MAINTENANCE:
        cid = update.effective_chat.id
        PENDING_CHATS.add(cid)
        save_pending()
        await update.message.reply_text(
            "⚠️ Уважаемый пользователь!\n"
            "Сейчас ведутся технические работы. Как только бот снова заработает, мы сообщим вам."
        )
        return

    text = update.message.text or ""
    # сначала — команды вида '/affairs delete' не перехватываем здесь

    # парсим естественные фразы
    parsed = parse_user_text(text)
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

    chat_id = update.effective_chat.id
    app: Application = context.application

    if "after" in parsed:
        when = now_local() + parsed["after"]
        job_id = schedule_once(app, chat_id, when, parsed["text"])
        append_reminder(chat_id, job_id, when, parsed["text"])
        await update.message.reply_text(
            f"✅ Ок, напомню {when:%Y-%m-%d %H:%M} — «{parsed['text']}». (TZ: {TZ.key})"
        )
        return

    if "once_at" in parsed:
        when = parsed["once_at"]
        job_id = schedule_once(app, chat_id, when, parsed["text"])
        append_reminder(chat_id, job_id, when, parsed["text"])
        await update.message.reply_text(
            f"✅ Ок, напомню {when:%Y-%m-%d %H:%M} — «{parsed['text']}». (TZ: {TZ.key})"
        )
        return

    if "daily_at" in parsed:
        at = parsed["daily_at"]
        job_id = schedule_daily(app, chat_id, at, parsed["text"])
        # для списка отображаем "ближайшее" время сегодня/завтра
        base = now_local().replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if base < now_local():
            base += timedelta(days=1)
        append_reminder(chat_id, job_id, base, parsed["text"])
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {at.strftime('%H:%M')} — «{parsed['text']}». (TZ: {TZ.key})"
        )
        return

# ====== Меню-команды ======
async def set_menu(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "помощь и примеры"),
        BotCommand("affairs", "список дел"),
        BotCommand("affairs_delete", "удалить дело по номеру"),
        BotCommand("maintenance_on", "включить техработы (админ)"),
        BotCommand("maintenance_off", "выключить техработы (админ)"),
    ])

# ====== Мини-Flask для Render (порт) ======
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return Response("ok", status=200)

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ====== main ======
def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # Flask-сервер, чтобы Render видел открытый порт
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(bot_token).build()

    # хендлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", cmd_affairs))
    app.add_handler(CommandHandler("affairs_delete", cmd_affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # голосовые
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def _post_init(_):
        await set_menu(app)

    app.post_init = _post_init  # PTB v21

    log.info("Starting bot with polling…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
