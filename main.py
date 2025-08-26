# -*- coding: utf-8 -*-
import os
import json
import re
import logging
from datetime import datetime, timedelta, time as dtime

import pytz
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------------------- БАЗОВЫЕ НАСТРОЙКИ ----------------------
TIMEZONE = pytz.timezone("Europe/Kaliningrad")
ADMIN_ID = 963586834  # <- твой ID (как просил)
DATA_FILE = "data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("reminder-bot")

# ---------------------- ПЕРСИСТЕНТНОЕ СОСТОЯНИЕ ----------------------
STATE = {
    "allowed_users": [],          # список user_id
    "keys_left": [],              # одноразовые ключи
    "tasks": {},                  # chat_id -> [{id, ts, text, kind, repeat, job_name}]
    "maintenance": False,         # флаг техработ
    "maintenance_waitlist": []    # список chat_id, кто писал во время работ
}

def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            STATE.update(data)
        except Exception as e:
            log.warning("Не удалось прочитать %s: %s", DATA_FILE, e)
    # если ключи не сгенерированы — создаём VIP001..VIP100
    if not STATE["keys_left"]:
        STATE["keys_left"] = [f"VIP{n:03d}" for n in range(1, 101)]
    # нормализуем типы
    STATE["allowed_users"] = list(set(STATE.get("allowed_users", [])))
    STATE["tasks"] = STATE.get("tasks", {})
    STATE["maintenance_waitlist"] = list(set(STATE.get("maintenance_waitlist", [])))

def save_state():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

load_state()

# ---------------------- ВСПОМОГАТЕЛЬНОЕ ----------------------
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

def now_local():
    return datetime.now(TIMEZONE)

def ensure_user(chat_id: int) -> None:
    """Инициализируем контейнер задач для чата."""
    if str(chat_id) not in STATE["tasks"]:
        STATE["tasks"][str(chat_id)] = []

def add_task(chat_id: int, when_dt: datetime, text: str, kind: str, repeat: bool, job_name: str):
    ensure_user(chat_id)
    STATE["tasks"][str(chat_id)].append({
        "id": job_name,
        "ts": int(when_dt.timestamp()),
        "text": text,
        "kind": kind,
        "repeat": repeat,
        "job_name": job_name,
    })
    save_state()

def remove_task(chat_id: int, job_name: str):
    ensure_user(chat_id)
    before = len(STATE["tasks"][str(chat_id)])
    STATE["tasks"][str(chat_id)] = [t for t in STATE["tasks"][str(chat_id)] if t["job_name"] != job_name]
    after = len(STATE["tasks"][str(chat_id)])
    if before != after:
        save_state()

def list_tasks(chat_id: int):
    ensure_user(chat_id)
    items = STATE["tasks"][str(chat_id)]
    # сортировка по времени
    items = sorted(items, key=lambda t: t["ts"])
    return items

# ---------------------- ДОСТУП ПО КЛЮЧУ ----------------------
def is_allowed(user_id: int) -> bool:
    return user_id in STATE["allowed_users"] or user_id == ADMIN_ID

def try_accept_key(user_id: int, text: str) -> bool:
    text = (text or "").strip()
    if text in STATE["keys_left"]:
        STATE["allowed_users"].append(user_id)
        STATE["keys_left"].remove(text)
        save_state()
        return True
    return False

# ---------------------- ПАРСИНГ ТЕКСТА ----------------------
TIME_RE = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default

def parse_text_to_schedule(text: str):
    """
    Возвращает словарь одной из форм:
    {"after": timedelta, "text": ...}
    {"once_at": datetime, "text": ...}
    {"daily_at": time, "text": ...}
    """
    t = (text or "").strip().lower()

    # 0) "сегодня в 14:00 ... встреча в 15:00"
    m = re.search(rf"сегодня\s+в\s+{TIME_RE}.*?встреча\s+в\s+{TIME_RE}", t)
    if m:
        h1, m1, h2, m2 = map(parse_int, [m.group("h"), m.group("m"), m.group(3), m.group(4)])
        remind_at = now_local().replace(hour=h1, minute=m1, second=0, microsecond=0)
        if remind_at < now_local():
            remind_at += timedelta(days=1)
        text_out = re.sub(r".*?встреча\s+в\s+\d{1,2}:\d{2}", "встреча в {:02d}:{:02d}".format(h2, m2), t)
        return {"once_at": remind_at, "text": text_out}

    # 1) "через N минут/часов ..."
    m = re.match(r"через\s+(\d+)\s*(минут|мин|m|mинуты)\b\s*(.+)?", t)
    if m:
        delta = timedelta(minutes=int(m.group(1)))
        return {"after": delta, "text": (m.group(3) or "").strip()}
    m = re.match(r"через\s+(\d+)\s*(час|часа|часов|h)\b\s*(.+)?", t)
    if m:
        delta = timedelta(hours=int(m.group(1)))
        return {"after": delta, "text": (m.group(3) or "").strip()}

    # 2) "сегодня в HH:MM ..."
    m = re.match(rf"сегодня\s+в\s+{TIME_RE}\s*(.+)?", t)
    if m:
        h, mnt = parse_int(m.group("h")), parse_int(m.group("m"))
        target = now_local().replace(hour=h, minute=mnt, second=0, microsecond=0)
        if target < now_local():
            target += timedelta(days=1)
        return {"once_at": target, "text": (m.group(3) or "").strip()}

    # 3) "завтра в HH:MM ..."
    m = re.match(rf"завтра\s+в\s+{TIME_RE}\s*(.+)?", t)
    if m:
        h, mnt = parse_int(m.group("h")), parse_int(m.group("m"))
        base = now_local().replace(hour=h, minute=mnt, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": (m.group(3) or "").strip()}

    # 4) "каждый день в HH:MM ..."
    m = re.match(rf"каждый\s+день\s+в\s+{TIME_RE}\s*(.+)?", t)
    if m:
        h, mnt = parse_int(m.group("h")), parse_int(m.group("m"))
        return {"daily_at": dtime(hour=h, minute=mnt), "text": (m.group(3) or "").strip()}

    # 5) "DD <месяц> [в HH:MM] ..."
    m = re.match(
        rf"(?P<d>\d{{1,2}})\s+(?P<mon>{'|'.join(MONTHS.keys())})(?:\s+в\s+{TIME_RE})?\s*(?P<text>.+)?",
        t
    )
    if m:
        d = parse_int(m.group("d"))
        mon = MONTHS[m.group("mon")]
        year = now_local().year
        hh = parse_int(m.group("h") or 9)
        mm = parse_int(m.group("m") or 0)
        target = TIMEZONE.localize(datetime(year, mon, d, hh, mm, 0))
        # если дата уже прошла в текущем году — переносим на следующий
        if target < now_local():
            target = TIMEZONE.localize(datetime(year + 1, mon, d, hh, mm, 0))
        return {"once_at": target, "text": (m.group("text") or "").strip()}

    # 6) "в HH:MM ..." (сегодня ближайшее)
    m = re.match(rf"в\s+{TIME_RE}\s*(.+)?", t)
    if m:
        h, mnt = parse_int(m.group("h")), parse_int(m.group("m"))
        target = now_local().replace(hour=h, minute=mnt, second=0, microsecond=0)
        if target < now_local():
            target += timedelta(days=1)
        return {"once_at": target, "text": (m.group(3) or "").strip()}

    return None

# ---------------------- JOBS ----------------------
async def job_fire(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    repeat = data.get("repeat", False)
    job_name = context.job.name

    if chat_id is None:
        return

    await context.bot.send_message(chat_id=chat_id, text=f"✅ Напоминание: «{text}».")
    if not repeat:
        remove_task(chat_id, job_name)

def schedule_once(app: Application, chat_id: int, when_dt: datetime, text: str):
    name = f"once-{chat_id}-{int(when_dt.timestamp())}-{abs(hash(text))%10_000}"
    app.job_queue.run_once(
        job_fire,
        when=when_dt,
        data={"chat_id": chat_id, "text": text, "repeat": False},
        name=name
    )
    add_task(chat_id, when_dt, text, kind="once", repeat=False, job_name=name)
    return name

def schedule_daily(app: Application, chat_id: int, at_time: dtime, text: str):
    name = f"daily-{chat_id}-{at_time.hour:02d}{at_time.minute:02d}-{abs(hash(text))%10_000}"
    # вычисляем первый запуск (сегодня/завтра)
    first = now_local().replace(hour=at_time.hour, minute=at_time.minute, second=0, microsecond=0)
    if first < now_local():
        first += timedelta(days=1)
    # периодический запуск раз в сутки
    app.job_queue.run_repeating(
        job_fire,
        interval=timedelta(days=1),
        first=first,
        data={"chat_id": chat_id, "text": text, "repeat": True},
        name=name
    )
    add_task(chat_id, first, text, kind="daily", repeat=True, job_name=name)
    return name

# ---------------------- КОМАНДЫ ----------------------
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
    "•Чтобы бот распознал какое либо кол-во минут - нужно писать всегда несклоняемо - МИНУТ (то есть не 2 минутЫ,а 2 минуТ)\n"
    "(часовой пояс: Europe/Kaliningrad)"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    await update.message.reply_text(HELP_TEXT)

async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    # техработы
    if STATE["maintenance"] and uid != ADMIN_ID:
        if chat_id not in STATE["maintenance_waitlist"]:
            STATE["maintenance_waitlist"].append(chat_id)
            save_state()
        await update.message.reply_text("⚠️ Уважаемый пользователь, ведутся технические работы. "
                                        "Мы сообщим, как только бот снова заработает.")
        return

    # если пользователь ещё не авторизован — пробуем принять ключ
    if not is_allowed(uid):
        if try_accept_key(uid, txt):
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.\n\n" + HELP_TEXT)
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # обычный текст — парсим
    parsed = parse_text_to_schedule(txt)
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Используй что-то из примеров:\n"
            "— через N минут/часов ...\n"
            "— сегодня в HH:MM ...\n"
            "— завтра в HH:MM ...\n"
            "— каждый день в HH:MM ...\n"
            "— DD <месяц> [в HH:MM] ..."
        )
        return

    task_text = parsed.get("text") or (txt or "")
    if "after" in parsed:
        when_dt = now_local() + parsed["after"]
        schedule_once(context.application, chat_id, when_dt, task_text)
        await update.message.reply_text(f"✅ Отлично, напомню через {parsed['after']} — «{task_text}».")
    elif "once_at" in parsed:
        when_dt = parsed["once_at"]
        schedule_once(context.application, chat_id, when_dt, task_text)
        await update.message.reply_text(
            f"✅ Отлично, напомню {when_dt.strftime('%Y-%m-%d %H:%M')} — «{task_text}». "
            f"(TZ: Europe/Kaliningrad)"
        )
    elif "daily_at" in parsed:
        at_time = parsed["daily_at"]
        schedule_daily(context.application, chat_id, at_time, task_text)
        await update.message.reply_text(
            f"✅ Отлично, буду напоминать каждый день в {at_time.strftime('%H:%M')} — «{task_text}»."
        )

async def affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    items = list_tasks(chat_id)
    if not items:
        await update.message.reply_text("Список дел пуст.")
        return
    out = ["Ваши ближайшие дела:"]
    for i, t in enumerate(items, 1):
        dt = datetime.fromtimestamp(t["ts"], tz=TIMEZONE)
        out.append(f"{i}. {dt.strftime('%d.%m.%Y %H:%M')} — {t['text']}")
    await update.message.reply_text("\n".join(out))

async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    # ожидаем одну цифру
    args = (update.message.text or "").strip().split()
    if len(args) < 2 or not args[1].isdigit():
        await update.message.reply_text("Использование: /affairs_delete N")
        return
    n = int(args[1])
    items = list_tasks(chat_id)
    if not (1 <= n <= len(items)):
        await update.message.reply_text("Нет дела с таким номером.")
        return
    job_name = items[n-1]["job_name"]
    # снять job из очереди
    job = context.application.job_queue.get_jobs_by_name(job_name)
    for j in job:
        j.schedule_removal()
    remove_task(chat_id, job_name)
    await update.message.reply_text(f"✅ Дело №{n} удалено.")

# ---------------------- ТЕХРАБОТЫ ----------------------
async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    STATE["maintenance"] = True
    save_state()
    await update.message.reply_text("🛠 Режим техработ включён.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    STATE["maintenance"] = False
    save_state()
    await update.message.reply_text("✅ Техработы завершены. Бот снова работает!")
    # уведомим тех, кто пытался писать
    if STATE["maintenance_waitlist"]:
        for cid in list(STATE["maintenance_waitlist"]):
            try:
                await context.bot.send_message(chat_id=int(cid), text="✅ Бот снова работает.")
            except Exception:
                pass
        STATE["maintenance_waitlist"] = []
        save_state()

# ---------------------- ГОЛОСОВЫЕ ----------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    if STATE["maintenance"] and uid != ADMIN_ID:
        if chat_id not in STATE["maintenance_waitlist"]:
            STATE["maintenance_waitlist"].append(chat_id)
            save_state()
        await update.message.reply_text("⚠️ Ведутся техработы. Сообщим, когда всё снова заработает.")
        return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        await update.message.reply_text("⚠️ Голосовые не настроены (нет OPENAI_API_KEY).")
        return

    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_path = f"/tmp/{file.file_id}.ogg"
        await file.download_to_drive(ogg_path)

        # Отправляем прямо в OpenAI без конверта (Whisper понимает ogg/opus)
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with open(ogg_path, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
                language="ru"
            )
        text = tr.strip()
        if not text:
            await update.message.reply_text("Не удалось распознать голос.")
            return
        # Скормим распознанный текст как обычную фразу
        fake_update = update
        fake_update.message.text = text
        await handle_key_or_text(fake_update, context)
    except Exception as e:
        log.exception("Ошибка распознавания голоса: %s", e)
        await update.message.reply_text("Произошла ошибка при распознавании голоса.")

# ---------------------- ИНИЦИАЛИЗАЦИЯ ----------------------
async def set_commands(application: Application):
    cmds = [
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
    ]
    # админские команды показывать не обязательно, но можно
    if True:
        cmds += [
            BotCommand("maintenance_on", "Включить техработы (админ)"),
            BotCommand("maintenance_off", "Выключить техработы (админ)"),
        ]
    await application.bot.set_my_commands(cmds)

def rebuild_jobs_on_start(application: Application):
    """Восстановление задач из файла."""
    for chat_id, items in STATE.get("tasks", {}).items():
        for t in items:
            # пересоздадим только будущие / повторяющиеся
            ts = datetime.fromtimestamp(t["ts"], tz=TIMEZONE)
            if t.get("repeat"):
                at = datetime.fromtimestamp(t["ts"], tz=TIMEZONE)
                # daily: просто пересоздаём как repeating (первый запуск — следующий день/сегодня)
                schedule_daily(application, int(chat_id), at.timetz(), t["text"])
            else:
                if ts > now_local():
                    schedule_once(application, int(chat_id), ts, t["text"])
                else:
                    # просроченное одноразовое — удалим
                    remove_task(int(chat_id), t["job_name"])

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", affairs))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    # важно: текстовые ПОСЛЕ команд
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    return app

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    application = build_application(token)
    # создадим меню команд внутри уже запущенного цикла — через post_init
    async def _post_init(app: Application):
        await set_commands(app)

    application.post_init = _post_init  # PTB вызовет это в run_polling
    rebuild_jobs_on_start(application)

    # ВАЖНО: используем синхронный метод run_polling() — он сам поднимет event loop.
    log.info("Starting bot with polling…")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
