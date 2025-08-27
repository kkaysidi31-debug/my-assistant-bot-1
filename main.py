import os
import re
import json
from datetime import datetime, timedelta
import pytz

from flask import Flask, request, jsonify

from apscheduler.schedulers.background import BackgroundScheduler

import telebot
from telebot import types

# ------------------ Конфиг ------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "963586834"))
TZ = pytz.timezone(os.getenv("TZ", "Europe/Kaliningrad"))

ACCESS_KEYS_LIST = os.getenv("ACCESS_KEYS", "VIP001,VIP002,VIP003").split(",")
ACCESS_KEYS = {k.strip(): None for k in ACCESS_KEYS_LIST if k.strip()}

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", os.getenv("BASE_URL", "")).rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
HEALTH_PATH = "/healthz"

DATA_FILE = "db.json"
TASKS_FILE = "tasks.json"

# ------------------ Глобалы ------------------
ALLOWED_USERS = set()
PENDING_CHATS = set()
MAINTENANCE = False

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
app = Flask(__name__)
scheduler = BackgroundScheduler(timezone=TZ)

HELP_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Сегодня в 14:00 (сигнал) напоминаю, встреча в 15:00 (само напоминание в 14:00)\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

# ------------------ Хранение ------------------
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_db():
    global ALLOWED_USERS, ACCESS_KEYS
    db = _load(DATA_FILE, {"allowed": [], "keys": {k: None for k in ACCESS_KEYS}})
    ALLOWED_USERS = set(db.get("allowed", []))
    # приоритет ключей из env
    combined = {k: db.get("keys", {}).get(k) for k in ACCESS_KEYS}
    ACCESS_KEYS = combined

def save_db():
    _save(DATA_FILE, {"allowed": sorted(ALLOWED_USERS), "keys": ACCESS_KEYS})

def load_tasks():
    return _load(TASKS_FILE, {})  # {uid: [{id, text, run_at, chat_id}]}

def save_tasks(tasks):
    _save(TASKS_FILE, tasks)

# ------------------ Утилиты ------------------
def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def next_task_id(tasks, uid: str) -> int:
    used = {t["id"] for t in tasks.get(uid, [])}
    n = 1
    while n in used:
        n += 1
    return n

def schedule_task(uid: int, chat_id: int, task_id: int, when: datetime, text: str):
    job_id = f"{uid}-{task_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    scheduler.add_job(
        func=notify_due,
        trigger="date",
        run_date=when,
        args=[uid, chat_id, task_id, text],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60,
    )

def reschedule_all():
    tasks = load_tasks()
    for uid, arr in tasks.items():
        for t in arr:
            when = datetime.fromisoformat(t["run_at"])
            schedule_task(int(uid), t["chat_id"], t["id"], when, t["text"])

def parse_and_store(uid: int, chat_id: int, text: str):
    """Минимальный парсинг фраз на русском."""
    now = datetime.now(TZ)

    # через N минут/часов ...
    m = re.search(r"через\s+(\d+)\s*(минут[уы]?|час(?:а|ов)?)", text, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        run_at = now + delta
        todo = re.sub(r"через\s+\d+\s*(?:минут[уы]?|час(?:а|ов)?)", "", text, flags=re.I).strip() or "дело"
        return store_task(uid, chat_id, todo, run_at)

    # сегодня в HH:MM ...
    m = re.search(r"сегодня\s+в\s+(\d{1,2}):(\d{2})", text, re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        run_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_at < now:
            run_at = run_at + timedelta(days=1)
        todo = re.sub(r"сегодня\s+в\s+\d{1,2}:\d{2}", "", text, flags=re.I).strip() or "дело"
        return store_task(uid, chat_id, todo, run_at)

    # завтра в HH:MM ...
    m = re.search(r"завтра\s+в\s+(\d{1,2}):(\d{2})", text, re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        base = now + timedelta(days=1)
        run_at = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        todo = re.sub(r"завтра\s+в\s+\d{1,2}:\d{2}", "", text, flags=re.I).strip() or "дело"
        return store_task(uid, chat_id, todo, run_at)

    # DD.MM.YYYY в HH:MM ...
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+в\s+(\d{1,2}):(\d{2})", text, re.I)
    if m:
        dd, mm_, yyyy, hh, mi = map(int, m.groups())
        run_at = TZ.localize(datetime(yyyy, mm_, dd, hh, mi, 0)).astimezone(TZ)
        todo = re.sub(r"\d{1,2}\.\d{1,2}\.\d{4}\s+в\s+\d{1,2}:\d{2}", "", text, flags=re.I).strip() or "дело"
        return store_task(uid, chat_id, todo, run_at)

    return None

def store_task(uid: int, chat_id: int, todo: str, run_at: datetime):
    tasks = load_tasks()
    suid = str(uid)
    arr = tasks.get(suid, [])
    tid = next_task_id(tasks, suid)
    item = {"id": tid, "text": todo, "run_at": run_at.isoformat(), "chat_id": chat_id}
    arr.append(item)
    tasks[suid] = arr
    save_tasks(tasks)
    schedule_task(uid, chat_id, tid, run_at, todo)
    return item

# ------------------ Напоминание ------------------
def notify_due(uid: int, chat_id: int, task_id: int, text: str):
    try:
        bot.send_message(chat_id, f"⏰ Напоминание: «{text}»")
    except Exception:
        pass
    # после отправки удалим задачу
    tasks = load_tasks()
    suid = str(uid)
    tasks[suid] = [t for t in tasks.get(suid, []) if t["id"] != task_id]
    save_tasks(tasks)

# ------------------ Команды ------------------
@bot.message_handler(commands=["start"])
def start_cmd(msg: types.Message):
    if msg.from_user.id not in ALLOWED_USERS:
        bot.reply_to(msg, "Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    bot.reply_to(msg, HELP_TEXT)

@bot.message_handler(commands=["affairs"])
def affairs_cmd(msg: types.Message):
    if msg.from_user.id not in ALLOWED_USERS:
        bot.reply_to(msg, "Нет доступа.")
        return
    tasks = load_tasks()
    arr = sorted(tasks.get(str(msg.from_user.id), []), key=lambda t: t["run_at"])
    if not arr:
        bot.reply_to(msg, "Ближайших дел нет.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(arr, 1):
        when = datetime.fromisoformat(t["run_at"])
        lines.append(f"{i}. {fmt_dt(when)} — {t['text']}")
    bot.reply_to(msg, "\n".join(lines))

@bot.message_handler(commands=["affairs_delete"])
def affairs_delete_cmd(msg: types.Message):
    if msg.from_user.id not in ALLOWED_USERS:
        bot.reply_to(msg, "Нет доступа.")
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(msg, "Укажи номер: /affairs_delete N")
        return
    try:
        num = int(args[1].strip())
    except ValueError:
        bot.reply_to(msg, "Номер должен быть целым.")
        return
    tasks = load_tasks()
    arr = sorted(tasks.get(str(msg.from_user.id), []), key=lambda t: t["run_at"])
    if num < 1 or num > len(arr):
        bot.reply_to(msg, "Нет такого номера.")
        return
    task = arr[num - 1]
    # удалить из файла
    arr = [t for t in arr if t["id"] != task["id"]]
    tasks[str(msg.from_user.id)] = arr
    save_tasks(tasks)
    # и из планировщика
    try:
        scheduler.remove_job(f"{msg.from_user.id}-{task['id']}")
    except Exception:
        pass
    bot.reply_to(msg, "✅ Задача удалена.")

@bot.message_handler(commands=["maintenance_on"])
def maintenance_on(msg: types.Message):
    global MAINTENANCE
    if msg.from_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    bot.reply_to(msg, "🟡 Технические работы включены.")

@bot.message_handler(commands=["maintenance_off"])
def maintenance_off(msg: types.Message):
    global MAINTENANCE
    if msg.from_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    bot.reply_to(msg, "🟢 Технические работы выключены.")
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# ------------------ Голос (опционально) ------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

def transcribe_ogg_bytes(data: bytes) -> str | None:
    if not OPENAI_API_KEY:
        return None
    try:
        from openai import OpenAI
        import io
        client = OpenAI(api_key=OPENAI_API_KEY)
        bio = io.BytesIO(data); bio.name = "audio.ogg"
        r = client.audio.transcriptions.create(model="whisper-1", file=bio)
        return (r.text or "").strip()
    except Exception:
        return None

@bot.message_handler(content_types=["voice"])
def voice_handler(msg: types.Message):
    if msg.from_user.id not in ALLOWED_USERS:
        bot.reply_to(msg, "Нет доступа.")
        return
    if MAINTENANCE and msg.from_user.id != ADMIN_ID:
        PENDING_CHATS.add(msg.chat.id)
        bot.reply_to(msg, "⚠️ Уважаемый пользователь! В данный момент ведутся технические работы. Попробуйте позже.")
        return
    try:
        file = bot.get_file(msg.voice.file_id)
        data = bot.download_file(file.file_path)
        text = transcribe_ogg_bytes(data) or ""
        if not text:
            bot.reply_to(msg, "Не получилось распознать голос (или ключ OpenAI не настроен).")
            return
        # прокинем в текстовый обработчик
        fake = types.Message(
            message_id=msg.message_id,
            date=msg.date,
            chat=msg.chat,
            from_user=msg.from_user,
            content_type="text",
            options=None,
            json_string=None
        )
        fake.text = text
        handle_text(fake)
    except Exception:
        bot.reply_to(msg, "Ошибка обработки голосового.")

# ------------------ Текст ------------------
@bot.message_handler(content_types=["text"])
def handle_text(msg: types.Message):
    global MAINTENANCE
    uid = msg.from_user.id
    chat_id = msg.chat.id
    text = (msg.text or "").strip()

    # приватность
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", text):
            if text in ACCESS_KEYS and ACCESS_KEYS[text] is None:
                ACCESS_KEYS[text] = uid
                ALLOWED_USERS.add(uid)
                save_db()
                bot.reply_to(msg, "Ключ принят ✅. Теперь можно ставить напоминания.")
                bot.send_message(chat_id, HELP_TEXT)
            else:
                bot.reply_to(msg, "Ключ недействителен.")
        else:
            bot.reply_to(msg, "Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        bot.reply_to(msg, "⚠️ Уважаемый пользователь! В данный момент ведутся технические работы. Попробуйте позже.")
        return

    # парсинг
    item = parse_and_store(uid, chat_id, text)
    if item:
        when = datetime.fromisoformat(item["run_at"])
        bot.reply_to(msg, f"✅ Ок, напомню {fmt_dt(when)} — «{item['text']}». (TZ: {TZ})")
    else:
        bot.reply_to(msg, "❓ Не понял формат. Примеры смотри в /start")

# ------------------ Flask/Webhook ------------------
@app.get(HEALTH_PATH)
def health():
    return jsonify({"ok": True})

@app.post(WEBHOOK_PATH)
def telegram_webhook():
    update = telebot.types.Update.de_json(request.get_json(force=True))
    bot.process_new_updates([update])
    return "OK", 200

# корень просто показать статус
@app.get("/")
def root():
    return "Telegram bot is alive", 200

def set_bot_commands():
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Помощь и примеры"),
            types.BotCommand("affairs", "Список дел"),
            types.BotCommand("affairs_delete", "Удалить дело по номеру"),
        ])
    except Exception:
        pass

def setup_webhook():
    if BASE_URL:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        url = f"{BASE_URL}{WEBHOOK_PATH}"
        bot.set_webhook(url, max_connections=40, allowed_updates=["message", "edited_message"])
        print("Webhook set to:", url)
    else:
        print("BASE_URL/RENDER_EXTERNAL_URL не задан — webhook не выставлен.")

def bootstrap():
    load_db()
    set_bot_commands()
    if not scheduler.running:
        scheduler.start()
    reschedule_all()
    setup_webhook()

# ------------------ Entrypoint ------------------
bootstrap()

# Экспорт переменной 'app' для waitress/gunicorn
# Стартовая команда на Render:
# waitress-serve --host=0.0.0.0 --port=$PORT main:app
