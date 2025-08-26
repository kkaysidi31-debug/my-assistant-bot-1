# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import random
import re
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional

from flask import Flask
from pytz import timezone
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes,
)

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reminder-bot")

# -------------------- КОНФИГ --------------------
TIMEZONE = timezone("Europe/Kaliningrad")
ADMIN_ID = 963586834  # <-- твой ID (как просил)

# Ключи доступа: VIP001..VIP100
ACCESS_KEYS: Dict[str, Optional[int]] = {f"VIP{str(i).zfill(3)}": None for i in range(1, 101)}
ALLOWED_USERS: set[int] = set()

# Файл БД
DB_FILE = "db.json"

# Техработы
MAINTENANCE = False
PENDING_CHATS: set[int] = set()

# -------------------- МИНИ-FLASK для /health (держим порт открытым) --------------------
flask_app = Flask(__name__)

@flask_app.get("/health")
def health():
    return "ok", 200

def run_flask():
    port = int(os.getenv("PORT", "8000"))
    log.info(f"Serving Flask app 'main' on 0.0.0.0:{port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# -------------------- МОДЕЛИ --------------------
@dataclass
class Task:
    kind: str                    # "once" | "daily"
    text: str
    when_iso: Optional[str] = None   # для разовых: iso-время
    hh: Optional[int] = None         # для daily
    mm: Optional[int] = None
    job_id: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "once":
            dt = datetime.fromisoformat(self.when_iso)
            dt_local = dt.astimezone(TIMEZONE)
            return f"{dt_local:%d.%m.%Y %H:%M} — {self.text}"
        else:
            return f"каждый день в {self.hh:02d}:{self.mm:02d} — {self.text}"

# -------------------- ХРАНИЛИЩЕ --------------------
# users -> list[Task]
STORE: Dict[str, List[Task]] = {}

def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump({uid: [asdict(t) for t in tasks] for uid, tasks in STORE.items()}, f, ensure_ascii=False, indent=2)

def load_db():
    global STORE, ALLOWED_USERS
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        STORE = {uid: [Task(**t) for t in tasks] for uid, tasks in raw.items()}
        # все пользователи, у кого есть задачи, сразу «разрешены»
        ALLOWED_USERS |= {int(uid) for uid in STORE.keys()}
    else:
        STORE = {}

# -------------------- ПАРСЕР РУССКИХ ФРАЗ --------------------
RE_TIME = r'(?P<h>\d{1,2}):(?P<m>\d{2})'
MONTHS = {
    "января":1, "февраля":2, "марта":3, "апреля":4, "мая":5, "июня":6,
    "июля":7, "августа":8, "сентября":9, "октября":10, "ноября":11, "декабря":12,
    "январь":1, "февраль":2, "март":3, "апрель":4, "июнь":6, "июль":7, "август":8,
    "сентябрь":9, "октябрь":10, "ноябрь":11, "декабрь":12,
}

def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def parse_command(s: str):
    s = s.strip().lower()

    # 1) через N минут/часов ...
    m = re.match(r'^через\s+(?P<n>\d+)\s*(минут|минуты|м|час|часа|часов|ч)\s+(?P<text>.+)$', s)
    if m:
        n = int(m.group('n'))
        unit = m.group(2)
        delta = timedelta(minutes=n) if unit.startswith(('м','мин')) else timedelta(hours=n)
        when = now_local() + delta
        return {"once_at": when, "text": m.group('text').strip()}

    # 2) сегодня в HH:MM ...
    m = re.match(rf'^сегодня\s+в\s+{RE_TIME}\s+(?P<text>.+)$', s)
    if m:
        h, mm = int(m.group('h')), int(m.group('m'))
        when = now_local().replace(hour=h, minute=mm, second=0, microsecond=0)
        return {"once_at": when, "text": m.group('text').strip()}

    # 3) завтра в HH:MM ...
    m = re.match(rf'^завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$', s)
    if m:
        h, mm = int(m.group('h')), int(m.group('m'))
        base = now_local().replace(hour=h, minute=mm, second=0, microsecond=0)
        when = base + timedelta(days=1)
        return {"once_at": when, "text": m.group('text').strip()}

    # 4) каждый день в HH:MM ...
    m = re.match(rf'^каждый\s+д(ень|н)\s+в\s+{RE_TIME}\s+(?P<text>.+)$', s)
    if m:
        h, mm = int(m.group('h')), int(m.group('m'))
        return {"daily_at": time(h, mm, tzinfo=TIMEZONE), "text": m.group('text').strip()}

    # 5) DD <месяц> [в HH:MM] ...
    m = re.match(rf'^(?P<dd>\d{{1,2}})\s+(?P<month>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$', s)
    if m:
        dd = int(m.group('dd'))
        month_name = m.group('month')
        month = MONTHS.get(month_name)
        if month:
            h = int(m.group('h')) if m.groupdict().get('h') else 9
            mm = int(m.group('m')) if m.groupdict().get('m') else 0
            year = now_local().year
            when = datetime(year, month, dd, h, mm, tzinfo=TIMEZONE)
            return {"once_at": when, "text": m.group('text').strip()}

    return None

# -------------------- УТИЛЫ --------------------
def ensure_user(uid: int):
    STORE.setdefault(str(uid), [])

async def schedule_once(context: ContextTypes.DEFAULT_TYPE, uid: int, task: Task):
    # when_iso (локальное со встроенным tz); PTB понимает tz-aware datetime
    when_dt = datetime.fromisoformat(task.when_iso)
    job = context.job_queue.run_once(remind_cb, when=when_dt, data={"chat_id": uid, "text": task.text})
    task.job_id = job.id
    save_db()

async def schedule_daily(context: ContextTypes.DEFAULT_TYPE, uid: int, task: Task):
    t = time(task.hh, task.mm, tzinfo=TIMEZONE)
    job = context.job_queue.run_daily(remind_cb, t, data={"chat_id": uid, "text": task.text})
    task.job_id = job.id
    save_db()

async def remind_cb(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    chat_id = data["chat_id"]
    text = data["text"]
    try:
        await ctx.bot.send_message(chat_id, f"✅ Напоминание: «{text}»")
    except Exception as e:
        log.exception(e)

def examples_text() -> str:
    return (
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

async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("maintenance_on", "Техработы: включить (только админ)"),
        BotCommand("maintenance_off", "Техработы: выключить (только админ)"),
    ])

# -------------------- ХЭНДЛЕРЫ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # приватность
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await update.message.reply_text(examples_text())

async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE

    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1) Если НЕ авторизован — пробуем принять ключ
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r'VIP\d{3}', msg):
            # ключ формально верный по формату, проверяем в пуле
            if msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
                ACCESS_KEYS[msg] = uid
                ALLOWED_USERS.add(uid)
                save_db()
                await update.message.reply_text(
                    "Ключ принят ✅. Теперь можно ставить напоминания."
                )
            else:
                await update.message.reply_text("Ключ недействителен ❌.")
            return
        # ключ не прислали — просим ввести
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # 2) Техработы (пускаем только админа)
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text(
            "⚠️ Уважаемый пользователь, в данный момент ведутся технические работы.\n"
            "Как только бот снова заработает, мы вам сообщим."
        )
        return

    # 3) Парсинг естественного языка и постановка напоминаний
    await handle_key_or_text(update, context)
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

    ensure_user(uid)
    tasks = STORE[str(uid)]

    if "once_at" in parsed:
        when = parsed["once_at"]
        text = parsed["text"]
        # нормализуем в ISO с tz
        when_iso = when.isoformat()
        t = Task(kind="once", text=text, when_iso=when_iso)
        tasks.append(t)
        await schedule_once(context, uid, t)
        when_local = when.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")
        await update.message.reply_text(f"✅ Ок, напомню {when_local} — «{text}». (TZ: Europe/Kaliningrad)")
    elif "daily_at" in parsed:
        tt: time = parsed["daily_at"]
        text = parsed["text"]
        t = Task(kind="daily", text=text, hh=tt.hour, mm=tt.minute)
        tasks.append(t)
        await schedule_daily(context, uid, t)
        await update.message.reply_text(f"✅ Ок, буду напоминать каждый день в {tt.hour:02d}:{tt.minute:02d} — «{text}».")
    save_db()

async def list_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    ensure_user(uid)
    tasks = STORE.get(str(uid), [])
    if not tasks:
        await update.message.reply_text("Пока нет дел.")
        return

    # формируем нумерованный список
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(tasks, start=1):
        lines.append(f"{i}. {t.describe()}")
    await update.message.reply_text("\n".join(lines))

async def delete_affair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    if not context.args:
        await update.message.reply_text("Укажи номер: /affairs_delete 3")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом: /affairs_delete 3")
        return

    ensure_user(uid)
    tasks = STORE.get(str(uid), [])
    if not (1 <= idx <= len(tasks)):
        await update.message.reply_text("Неверный номер.")
        return

    # снимаем джобу, если была
    task = tasks[idx-1]
    if task.job_id:
        try:
            context.job_queue.scheduler.remove_job(task.job_id)
        except Exception:
            pass
    tasks.pop(idx-1)
    save_db()
    await update.message.reply_text("Готово. Удалил.")

# ----------- ТЕХРАБОТЫ -----------
async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    await update.message.reply_text("⚙️ Техработы включены.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE, PENDING_CHATS
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Техработы выключены.")
    # уведомим ожидавших
    for chat_id in list(PENDING_CHATS):
        try:
            await context.bot.send_message(chat_id, "✅ Бот снова работает.")
        except Exception:
            pass
    PENDING_CHATS.clear()

# ----------- ГОЛОСОВЫЕ -----------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если есть OPENAI_API_KEY — распознаём голос и пускаем через тот же парсер."""
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        await update.message.reply_text("Распознавание речи не настроено (нет OPENAI_API_KEY).")
        return

    # качаем файл ogg
    file = await update.message.voice.get_file()
    path = "voice.ogg"
    await file.download_to_drive(path)

    # распознаём через OpenAI Whisper (простая обёртка)
    try:
        import openai  # pip install openai
        openai.api_key = api_key
        with open(path, "rb") as f:
            res = openai.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                language="ru"
            )
        text = res.strip()
        if not text:
            await update.message.reply_text("Не удалось распознать голос.")
            return
        # отправим как обычный текст в обработчик
        fake_update = update
        fake_update.message.text = text
        await handle_key_or_text(fake_update, context)
    except Exception as e:
        log.exception(e)
        await update.message.reply_text("Ошибка распознавания. Проверьте OPENAI_API_KEY.")

# -------------------- ВОССТАНОВЛЕНИЕ ДЖОБ --------------------
async def restore_jobs(app: Application):
    for uid_str, tasks in STORE.items():
        uid = int(uid_str)
        for t in tasks:
            try:
                if t.kind == "once":
                    dt = datetime.fromisoformat(t.when_iso)
                    if dt > now_local():
                        job = app.job_queue.run_once(remind_cb, when=dt, data={"chat_id": uid, "text": t.text})
                        t.job_id = job.id
                else:
                    job = app.job_queue.run_daily(
                        time(hour=t.hh, minute=t.mm, tzinfo=TIMEZONE),
                        data={"chat_id": uid, "text": t.text}
                    )
                    t.job_id = job.id
            except Exception as e:
                log.exception(e)
    save_db()

# -------------------- MAIN --------------------
def main():
    import os  # в рендер-логах у тебя был NameError — на всякий случай.
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет BOT_TOKEN в переменных окружения.")

    load_db()

    # поднимем Flask /health в отдельном потоке (чтобы Web Service не «засыпал»)
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", list_affairs))
    app.add_handler(CommandHandler("affairs_delete", delete_affair))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # Текст / Ключи
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))
    # Голос
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    async def on_startup(app_: Application):
        await set_commands(app_)
        await restore_jobs(app_)

    app.post_init = on_startup  # v21: корутина вызовется после инициализации

    log.info("Starting bot with polling...")
    app.run_polling(close_loop=False)  # один экземпляр! не запускай копии локально

if __name__ == "__main__":
    main()
