import os
import re
import json
import logging
import threading
from datetime import datetime, timedelta
import pytz

from flask import Flask, Response
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("assistant-bot")

# ---------- КОНСТАНТЫ ----------
TIMEZONE = pytz.timezone("Europe/Kaliningrad")
ADMIN_ID = 963586834  # твой id
DB_PATH = "db.json"

# 100 одноразовых ключей VIP001..VIP100
ACCESS_KEYS = {f"VIP{n:03d}": None for n in range(1, 101)}  # None = ещё не активирован
ALLOWED_USERS: set[int] = set()

# Техработы
MAINTENANCE = False
PENDING_CHATS: set[int] = set()  # Чаты, которым надо сообщить «бот снова работает»

# ---------- УТИЛИТЫ ДЛЯ БАЗЫ ----------
def load_db():
    global ACCESS_KEYS, ALLOWED_USERS
    if not os.path.exists(DB_PATH):
        save_db()
        return
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ACCESS_KEYS.update(data.get("keys", {}))
        ALLOWED_USERS.update(data.get("allowed", []))
    except Exception as e:
        log.warning("DB load warning: %s", e)


def save_db():
    try:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"keys": ACCESS_KEYS, "allowed": list(ALLOWED_USERS)},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        log.error("DB save error: %s", e)


def now_local():
    return datetime.now(TIMEZONE)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------- МЕНЮ КОМАНД ----------
async def post_init(application: Application):
    # Общее меню
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Помощь и примеры"),
            BotCommand("affairs", "Список дел"),
            BotCommand("affairs_delete", "Удалить дело по номеру"),
        ],
        scope=BotCommandScopeDefault(),
    )
    # Админ-меню только тебе
    await application.bot.set_my_commands(
        [
            BotCommand("maintenance_on", "Включить техработы"),
            BotCommand("maintenance_off", "Выключить техработы"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )


# ---------- ТЕКСТ ПОДСКАЗОК ----------
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

PRIVATE_PROMPT = "Бот приватный. Введите ключ доступа в формате ABC123."

# ---------- ОБРАБОТЧИКИ КОМАНД ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text(PRIVATE_PROMPT)
        return
    await update.message.reply_text(HELP_TEXT)


async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    await update.message.reply_text("⚙️ Технические работы включены. Пользователи увидят предупреждение.")


async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE, PENDING_CHATS
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Технические работы выключены.")
    # Уведомить ожидавших
    for chat_id in list(PENDING_CHATS):
        try:
            await context.bot.send_message(chat_id, "✅ Бот снова работает.")
            except Exception as e:
            log.warning("Notify back error: %s", e)
    PENDING_CHATS.clear()


# ---------- РАСПОЗНАВАНИЕ ГОЛОСА (опционально) ----------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        await update.message.reply_text("🎤 Голосовое распознавание недоступно (нет OPENAI_API_KEY).")
        return
    try:
        voice = update.message.voice or update.message.audio or update.message.document
        if not voice:
            return
        file = await context.bot.get_file(voice.file_id)
        local_path = f"/tmp/{voice.file_unique_id}.ogg"
        await file.download_to_drive(local_path)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        with open(local_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru",
            )
        text = transcript.text.strip()
        if not text:
            await update.message.reply_text("Не удалось распознать речь.")
            return

        fake_update = update
        fake_update.message.text = text
        await handle_key_or_text(fake_update, context)

    except Exception as e:
        log.exception("Voice error")
        await update.message.reply_text(f"Ошибка распознавания: {e}")


# ---------- ПЛАНИРОВЩИК ----------
async def job_remind(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text", "Напоминание")
    try:
        await context.bot.send_message(chat_id, f"⏰ {text}")
    except Exception as e:
        log.warning("Send remind error: %s", e)


def schedule_once(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, when_dt: datetime, text: str):
    when_dt = when_dt.astimezone(TIMEZONE)
    name = f"once-{chat_id}-{int(when_dt.timestamp())}-{abs(hash(text))%10000}"
    ctx.job_queue.run_once(
        job_remind,
        when=when_dt,
        name=name,
        data={"chat_id": chat_id, "text": f"{fmt_dt(when_dt)} — «{text}»"},
        tzinfo=TIMEZONE,
    )
    return name


def schedule_in(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, delta: timedelta, text: str):
    when = now_local() + delta
    return schedule_once(ctx, chat_id, when, text)


def schedule_daily(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, hh: int, mm: int, text: str):
    name = f"daily-{chat_id}-{hh:02d}{mm:02d}-{abs(hash(text))%10000}"
    ctx.job_queue.run_daily(
        job_remind,
        time=datetime.now(TIMEZONE).replace(hour=hh, minute=mm, second=0, microsecond=0).timetz(),
        name=name,
        data={"chat_id": chat_id, "text": f"каждый день {hh:02d}:{mm:02d} — «{text}»"},
        tzinfo=TIMEZONE,
    )
    return name


# ---------- СПИСОК ДЕЛ ----------
def list_jobs_for_chat(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int):
    jobs = []
    for job in ctx.job_queue.jobs():
        data = job.data or {}
        if data.get("chat_id") == chat_id:
            when = job.next_t if hasattr(job, "next_t") else None
            when_str = fmt_dt(when.astimezone(TIMEZONE)) if when else data.get("text", "")
            jobs.append((job, when_str, data.get("text", "")))
    jobs.sort(key=lambda x: (x[0].next_t or datetime.max.replace(tzinfo=TIMEZONE)))
    return jobs


async def cmd_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ALLOWED_USERS:
        await update.message.reply_text(PRIVATE_PROMPT)
        return
    jobs = list_jobs_for_chat(context, update.effective_chat.id)
    if not jobs:
        await update.message.reply_text("Список дел пуст.")
        return
    lines = ["Ваши ближайшие дела:"]
    for i, (_, when_str, text) in enumerate(jobs, start=1):
        lines.append(f"{i}. {text}")
    await update.message.reply_text("\n".join(lines))


async def cmd_affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.idif uid not in ALLOWED_USERS:
        await update.message.reply_text(PRIVATE_PROMPT)
        return
    if not context.args:
        await update.message.reply_text("Укажи номер дела: /affairs_delete 2")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом.")
        return
    jobs = list_jobs_for_chat(context, update.effective_chat.id)
    if not jobs or not (1 <= idx <= len(jobs)):
        await update.message.reply_text("Нет дела с таким номером.")
        return
    job = jobs[idx - 1][0]
    job.schedule_removal()
    await update.message.reply_text("✅ Удалил.")


# ---------- ПАРСЕР ----------
RE_TIME = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

def parse_text(t: str):
    txt = t.strip().lower()
    m = re.match(rf"сегодня\s+в\s+{RE_TIME}(?:.*?)(?P<text>.+)$", txt)
    if m: return {"kind": "today", "h": int(m["h"]), "m": int(m["m"]), "text": m["text"].strip()}
    m = re.match(rf"завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", txt)
    if m: return {"kind": "tomorrow", "h": int(m["h"]), "m": int(m["m"]), "text": m["text"].strip()}
    m = re.match(rf"каждый\s+день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", txt)
    if m: return {"kind": "daily", "h": int(m["h"]), "m": int(m["m"]), "text": m["text"].strip()}
    m = re.match(r"через\s+(?P<n>\d+)\s*(?P<u>минут(?:у|ы)?|мин|час(?:а|ов)?|ч)\s+(?P<text>.+)$", txt)
    if m:
        n, u = int(m["n"]), m["u"]
        minutes = n if u.startswith("мин") else n * 60
        return {"kind": "in", "minutes": minutes, "text": m["text"].strip()}
    m = re.match(rf"(?P<d>\d{{1,2}})\s+(?P<mon>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$", txt)
    if m and m["mon"] in MONTHS:
        h = int(m["h"]) if m.groupdict().get("h") else 9
        mm = int(m["m"]) if m.groupdict().get("m") else 0
        return {"kind": "date","day": int(m["d"]),"month": MONTHS[m["mon"]],"h": h,"m": mm,"text": m["text"].strip()}
    return None


# ---------- ЕДИНЫЙ ОБРАБОТЧИК ----------
async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", msg):
            if msg in ACCESS_KEYS and (ACCESS_KEYS[msg] in (None, uid)):
                ACCESS_KEYS[msg] = uid; ALLOWED_USERS.add(uid); save_db()
                await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
                await update.message.reply_text(HELP_TEXT)
            else:
                await update.message.reply_text("Ключ недействителен или уже использован.")
        else:
            await update.message.reply_text(PRIVATE_PROMPT)
        return

    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("⚠️ Сейчас техработы. Мы уведомим, когда бот снова заработает.")
        return

    parsed = parse_text(msg)
    if not parsed:
        await update.message.reply_text("❓ Не понял формат.")
        return

    kind, text = parsed["kind"], parsed["text"]

    if kind == "in":
        schedule_in(context, chat_id, timedelta(minutes=parsed["minutes"]), text)
        await update.message.reply_text(f"✅ Ок, напомню через {parsed['minutes']} мин — «{text}»."); return
    if kind == "today":
        target = now_local().replace(hour=parsed["h"], minute=parsed["m"], second=0, microsecond=0)
        if target < now_local(): target += timedelta(days=1)
        schedule_once(context, chat_id, target, text)
        await update.message.reply_text(f"✅ Ок, напомню {fmt_dt(target)} — «{text}»."); return
    if kind == "tomorrow":
        base = now_local() + timedelta(days=1)
        target = base.replace(hour=parsed["h"], minute=parsed["m"], second=0, microsecond=0)
        schedule_once(context, chat_id, target, text)
        await update.message.reply_text(f"✅ Ок, завтра {fmt_dt(target)} — «{text}»."); return
    if kind == "daily":
        schedule_daily(context, chat_id, parsed["h"], parsed["m"], text)
        await update.message.reply_text(f"✅ Ок, каждый день {parsed['h']:02d}:{parsed['m']:02d} — «{text}»."); return
    if kind == "date":
        year = now_local().year
        target = TIMEZONE.localize(datetime(year, parsed["month"], parsed["day"], parsed["h"], parsed["m"]))
        if target < now_local(): target = target.replace(year=year + 1)
        schedule_once(context, chat_id, target, text)
        await update.message.reply_text(f"✅ Ок, {fmt_dt(target)} — «{text}»."); return


# ---------- HTTP ПРОБА ----------
def run_http_probe():
    app = Flask(__name__)
    @app.get("/") 
    def root(): return Response("ok", status=200)
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ---------- ЗАПУСК ----------
def main():
    load_db()
    token = os.getenv("BOT_TOKEN")
    if not token: raise SystemExit("Нет переменной окружения BOT_TOKEN")
    threading.Thread(target=run_http_probe, daemon=True).start()
    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("affairs", cmd_affairs))
    application.add_handler(CommandHandler("affairs_delete", cmd_affairs_delete))
    application.add_handler(CommandHandler("maintenance_on", maintenance_on))
    application.add_handler(CommandHandler("maintenance_off", maintenance_off))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
