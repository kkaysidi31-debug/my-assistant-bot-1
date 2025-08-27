import os
import json
import re
import asyncio
from datetime import datetime, timedelta
import pytz

from aiohttp import web

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, BotCommand, Update as TgUpdate

# ============ КОНФИГ ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ADMIN_ID = 963586834  # твой ID
TZ = pytz.timezone(os.getenv("TZ", "Europe/Kaliningrad"))

# приватные ключи доступа (через запятую)
ACCESS_KEYS_LIST = os.getenv("ACCESS_KEYS", "VIP001,VIP002,VIP003").split(",")
ACCESS_KEYS = {k.strip(): None for k in ACCESS_KEYS_LIST if k.strip()}

DATA_FILE = "db.json"
TASKS_FILE = "tasks.json"

# webhook
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", os.getenv("BASE_URL", "")).rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
HEALTH_PATH = "/healthz"
PORT = int(os.getenv("PORT", "10000"))

# ============ ГЛОБАЛЫ ============
ALLOWED_USERS = set()
PENDING_CHATS = set()
MAINTENANCE = False

scheduler = AsyncIOScheduler(timezone=TZ)
bot: Bot | None = None
dp = Dispatcher()

# ============ ХРАНИЛКИ ============
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_db():
    global ALLOWED_USERS, ACCESS_KEYS
    db = load_json(DATA_FILE, {"allowed": [], "keys": {k: None for k in ACCESS_KEYS}})
    ALLOWED_USERS = set(db.get("allowed", []))
    # оставляем ключи из env приоритетно
    env_keys = {k: db["keys"].get(k) for k in db.get("keys", {})}
    for k in ACCESS_KEYS:
        env_keys.setdefault(k, None)
    ACCESS_KEYS = env_keys

def save_db():
    save_json(DATA_FILE, {"allowed": sorted(ALLOWED_USERS), "keys": ACCESS_KEYS})

def load_tasks():
    return load_json(TASKS_FILE, {})  # {uid: [{id, text, run_at_iso}]}

def save_tasks(tasks):
    save_json(TASKS_FILE, tasks)

# ============ УТИЛЫ ============
def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def next_task_id(tasks, uid: str) -> int:
    used = {t["id"] for t in tasks.get(uid, [])}
    n = 1
    while n in used:
        n += 1
    return n

async def notify_due(uid: str, chat_id: int, task_id: int, text: str):
    try:
        await bot.send_message(chat_id, f"⏰ Напоминание: «{text}»")
    except Exception:
        pass
    # после отправки удаляем
    tasks = load_tasks()
    arr = tasks.get(str(uid), [])
    arr = [t for t in arr if t["id"] != task_id]
    tasks[str(uid)] = arr
    save_tasks(tasks)

def schedule_task(uid: int, chat_id: int, task_id: int, run_at: datetime, text: str):
    job_id = f"{uid}-{task_id}"
    # если есть старое — уберём
    for job in scheduler.get_jobs():
        if job.id == job_id:
            job.remove()
    scheduler.add_job(
        notify_due,
        "date",
        id=job_id,
        run_date=run_at.astimezone(TZ),
        args=[str(uid), chat_id, task_id, text],
        misfire_grace_time=60,
        replace_existing=True,
    )

def reschedule_all():
    tasks = load_tasks()
    for uid, arr in tasks.items():
        for t in arr:
            run_at = datetime.fromisoformat(t["run_at"])
            schedule_task(int(uid), t["chat_id"], t["id"], run_at, t["text"])

# ============ КОМАНДЫ ============
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

async def set_menu():
    commands = [BotCommand(command="start", description="Помощь и примеры"),
        BotCommand(command="affairs", description="Список дел"),
        BotCommand(command="affairs_delete", description="Удалить дело по номеру"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception:
        pass

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    if msg.from_user.id not in ALLOWED_USERS:
        await msg.answer("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await msg.answer(HELP_TEXT)

@dp.message(Command("affairs"))
async def cmd_affairs(msg: Message):
    if msg.from_user.id not in ALLOWED_USERS:
        await msg.answer("Нет доступа.")
        return
    tasks = load_tasks()
    arr = sorted(tasks.get(str(msg.from_user.id), []), key=lambda t: t["run_at"])
    if not arr:
        await msg.answer("Ближайших дел нет.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(arr, 1):
        dt = datetime.fromisoformat(t["run_at"])
        lines.append(f"{i}. {fmt_dt(dt)} — {t['text']}")
    await msg.answer("\n".join(lines))

@dp.message(Command("affairs_delete"))
async def cmd_affairs_delete(msg: Message, command: CommandObject):
    if msg.from_user.id not in ALLOWED_USERS:
        await msg.answer("Нет доступа.")
        return
    if not command.args:
        await msg.answer("Укажи номер дела: /affairs_delete N")
        return
    try:
        num = int(command.args.strip())
    except ValueError:
        await msg.answer("Номер должен быть целым.")
        return

    tasks = load_tasks()
    arr = sorted(tasks.get(str(msg.from_user.id), []), key=lambda t: t["run_at"])
    if num < 1 or num > len(arr):
        await msg.answer("Нет такого номера.")
        return
    task = arr[num - 1]
    # удалить из файла
    arr = [t for t in arr if t["id"] != task["id"]]
    tasks[str(msg.from_user.id)] = arr
    save_tasks(tasks)
    # и из планировщика
    for job in scheduler.get_jobs():
        if job.id == f"{msg.from_user.id}-{task['id']}":
            job.remove()
    await msg.answer("✅ Задача удалена.")

# ============ ТЕХРАБОТЫ (только админ) ============
@dp.message(Command("maintenance_on"))
async def maintenance_on(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    global MAINTENANCE
    MAINTENANCE = True
    await msg.answer("🟡 Технические работы включены.")

@dp.message(Command("maintenance_off"))
async def maintenance_off(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    global MAINTENANCE
    MAINTENANCE = False
    await msg.answer("🟢 Технические работы выключены.")
    # уведомим ожидавших пользователей
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            await bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass

# ============ ГОЛОС (опционально) ============
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

async def transcribe_ogg(data: bytes) -> str | None:
    if not OPENAI_API_KEY:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        # отправляем как файл в памяти
        import io
        f = io.BytesIO(data)
        f.name = "audio.ogg"
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f
        )
        return result.text.strip()
    except Exception:
        return None

@dp.message(F.voice)
async def on_voice(msg: Message):
    if msg.from_user.id not in ALLOWED_USERS:
        await msg.answer("Нет доступа.")
        return
    if MAINTENANCE and msg.from_user.id != ADMIN_ID:
        PENDING_CHATS.add(msg.chat.id)
        await msg.answer("⚠️ Уважаемый пользователь! В данный момент ведутся технические работы. Попробуйте позже.")
        return

    file = await bot.get_file(msg.voice.file_id)
    data = await bot.download_file(file.file_path)
    text = await transcribe_ogg(data.read())
    if not text:
        await msg.answer("Не получилось распознать голос (или ключ OpenAI не настроен).")
        return
    # прокинем в общий обработчик текста
    fake = TgUpdate(message=Message.model_validate(msg.model_dump()))
    fake.message.text = text
    await handle_text(fake.message)

# ============ ПРИЁМ КЛЮЧА/ТЕКСТА ============
@dp.message()
async def handle_text(msg: Message):
    uid = msg.from_user.id
    chat_id = msg.chat.id
    text = (msg.text or "").strip()

    # 1) приватный ключ
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", text):
            if text in ACCESS_KEYS and ACCESS_KEYS[text] is None:
                ACCESS_KEYS[text] = uid
                ALLOWED_USERS.add(uid)
                save_db()
                await msg.answer("Ключ принят ✅. Теперь можно ставить напоминания.")
                await msg.answer(HELP_TEXT)
                return
            else:
                await msg.answer("Ключ недействителен.")
                return
        else:
            await msg.answer("Бот приватный. Введите ключ доступа в формате ABC123.")
            return

    # 2) техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await msg.answer("⚠️ Уважаемый пользователь! В данный момент ведутся технические работы. Попробуйте позже.")
        return

    # 3) парсинг естественного языка (минимальный, как договаривались)

    # «через N минут/час(ов) ...»
    m = re.search(r"через\s+(\d+)\s*(минут[уы]?|час(а|ов)?)", text, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(minutes=n) if unit.startswith("мин") else timedelta(hours=n)
        run_at = datetime.now(TZ) + delta
        todo = re.sub(r"через\s+\d+\s*(?:минут[уы]?|час(?:а|ов)?)", "", text, flags=re.I).strip() or "дело"
        await store_task(uid, chat_id, todo, run_at)
        await msg.answer(f"✅ Ок, напомню через {str(delta)} — «{todo}».")
        return

    # «сегодня в HH:MM ...»
    m = re.search(r"сегодня\s+в\s+(\d{1,2}):(\d{2})", text, re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        run_at = TZ.localize(datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0))
        todo = re.sub(r"сегодня\s+в\s+\d{1,2}:\d{2}", "", text, flags=re.I).strip() or "дело"
        if run_at < datetime.now(TZ):
            run_at += timedelta(days=1)
        await store_task(uid, chat_id, todo, run_at)
        await msg.answer(f"✅ Ок, напомню {fmt_dt(run_at)} — «{todo}». (TZ: {TZ})")
        return

    # «завтра в HH:MM ...»
    m = re.search(r"завтра\s+в\s+(\d{1,2}):(\d{2})", text, re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        base = datetime.now(TZ) + timedelta(days=1)
        run_at = TZ.localize(base.replace(hour=hh, minute=mm, second=0, microsecond=0))
        todo = re.sub(r"завтра\s+в\s+\d{1,2}:\d{2}", "", text, flags=re.I).strip() or "дело"
        await store_task(uid, chat_id, todo, run_at)
        await msg.answer(f"✅ Ок, напомню {fmt_dt(run_at)} — «{todo}». (TZ: {TZ})")
        return

    # «DD.MM.YYYY в HH:MM ...»
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+в\s+(\d{1,2}):(\d{2})", text, re.I)
    if m:
        dd, mm_, yyyy, hh, mi = map(int, m.groups())
        run_at = TZ.localize(datetime(yyyy, mm_, dd, hh, mi, 0))
        todo = re.sub(r"\d{1,2}\.\d{1,2}\.\d{4}\s+в\s+\d{1,2}:\d{2}", "", text, flags=re.I).strip() or "дело"
        await store_task(uid, chat_id, todo, run_at)
        await msg.answer(f"✅ Ок, напомню {fmt_dt(run_at)} — «{todo}». (TZ: {TZ})")
        return

    # если ничего не поняли
    await msg.answer("❓ Не понял формат. Примеры смотри в /start")

async def store_task(uid: int, chat_id: int, text: str, run_at: datetime):
    tasks = load_tasks()
    arr = tasks.get(str(uid), [])
    task_id = next_task_id(tasks, str(uid))
    arr.append({"id": task_id, "text": text, "run_at": run_at.isoformat(), "chat_id": chat_id})
    tasks[str(uid)] = arr
    save_tasks(tasks)
    schedule_task(uid, chat_id, task_id, run_at, text)

# ============ AIOHTTP + WEBHOOK ============
async def on_startup(app: web.Application):
    # меню
    await set_menu()
    # восстановим БД и задачи
    load_db()
    reschedule_all()
    # регистрируем webhook
    if BASE_URL:
        url = f"{BASE_URL}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(url, drop_pending_updates=True)
        except Exception:
            pass

async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    scheduler.shutdown(wait=False)

def build_web_app() -> web.Application:
    application = web.Application()
    application.router.add_get(HEALTH_PATH, lambda r: web.json_response({"ok": True}))
    # aiogram webhook handler
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(application, path=WEBHOOK_PATH)
    setup_application(application, dp, bot=bot)
    application.on_startup.append(on_startup)
    application.on_shutdown.append(on_shutdown)
    return application

async def main():
    global bot
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN не задан.")
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
    # планировщик
    scheduler.start()
    # web app
    app = build_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Serving on 0.0.0.0:{PORT} | health: {HEALTH_PATH} | webhook: {WEBHOOK_PATH}")
    # держим процесс
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
