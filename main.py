# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple, Set

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

# =============== НАСТРОЙКИ ===============
ADMIN_ID = 963586834  # ваш ID
TZ = ZoneInfo("Europe/Kaliningrad")
DB_FILE = "db.json"

# Генерим ключи VIP001..VIP100
ACCESS_KEYS: Dict[str, Optional[int]] = {f"VIP{n:03d}": None for n in range(1, 101)}

# Список русских месяцев (в родительном падеже не требуем — для простоты)
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    # поддержим и именительный
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "июнь": 6, "июль": 7,
    "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}

# =============== ХРАНИЛКА ===============
def now_local() -> datetime:
    return datetime.now(TZ)

@dataclass
class Task:
    chat_id: int
    when_iso: str            # ISO время (локальное)
    text: str
    kind: str                # "once" | "daily"
    job_id: str

DB: Dict[str, Any] = {
    "allowed_users": [],          # list[int]
    "access_keys": ACCESS_KEYS,   # dict[str, Optional[int]]
    "tasks": [],                  # list[Task dict]
    "maintenance": False,
    "pending_chats": []           # list[int] — кому отписать после техработ
}

def load_db() -> None:
    global DB
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                DB.update(json.load(f))
        except Exception:
            pass
    else:
        save_db()

def save_db() -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

def allowed_users() -> Set[int]:
    return set(DB.get("allowed_users", []))

def is_allowed(uid: int) -> bool:
    return uid in allowed_users() or uid == ADMIN_ID

# =============== ПАРСИНГ ЕСТЕСТВЕННОГО ЯЗЫКА ===============
RE_TIME = r"(?P<h>\d{1,2}):(?P<m>\d{2})"

def parse_text(t: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает dict:
      {"type": "once", "dt": datetime, "text": "..."}  или
      {"type": "daily", "t": time, "text": "..."}
    Поддержка:
      — "через N минут/часов ..."
      — "сегодня в HH:MM ..."
      — "завтра в HH:MM ..."
      — "каждый день в HH:MM ..."
      — "DD <месяц> [в HH:MM] ..."
      — "сегодня в HH:MM ... в HH:MM ..." (первое время — сигнал, второе — само напоминание)
    """
    t = t.strip().lower()

    # 1) через N минут/часов ...
    m = re.match(r"через\s+(?P<n>\d+)\s*(мин|минут|минуты)\b\s*(?P<txt>.*)", t)
    if m:
        n = int(m.group("n"))
        dt = now_local() + timedelta(minutes=n)
        txt = m.group("txt").strip() or "дело"
        return {"type": "once", "dt": dt, "text": txt}

    m = re.match(r"через\s+(?P<n>\d+)\s*(час|часа|часов)\b\s*(?P<txt>.*)", t)
    if m:
        n = int(m.group("n"))
        dt = now_local() + timedelta(hours=n)
        txt = m.group("txt").strip() or "дело"
        return {"type": "once", "dt": dt, "text": txt}

    # 2) сегодня в HH:MM ...
    m = re.match(rf"сегодня\s*в\s*{RE_TIME}\s*(?P<txt>.+)", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        dt = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        # если время уже прошло — на завтра
        if dt <= now_local():
            dt += timedelta(days=1)
        return {"type": "once", "dt": dt, "text": m.group("txt").strip()}

    # 3) завтра в HH:MM ...
    m = re.match(rf"завтра\s*в\s*{RE_TIME}\s*(?P<txt>.+)", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        dt = base + timedelta(days=1)
        return {"type": "once", "dt": dt, "text": m.group("txt").strip()}

    # 4) каждый день в HH:MM ...
    m = re.match(rf"(каждый\s*день|ежедневно)\s*в\s*{RE_TIME}\s*(?P<txt>.+)", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return {"type": "daily", "t": time(hour=hh, minute=mm, tzinfo=TZ), "text": m.group("txt").strip()}

    # 5) DD <месяц> [в HH:MM] ...
    m = re.match(
        rf"(?P<d>\d{{1,2}})\s+(?P<mon>[а-я]+)(?:\s*в\s*{RE_TIME})?\s*(?P<txt>.+)",
        t
    )
    if m and m.group("mon") in MONTHS:
        d = int(m.group("d"))
        mon = MONTHS[m.group("mon")]
        hh = int(m.group("h")) if m.group("h") else 9
        mm = int(m.group("m")) if m.group("m") else 0
        y = now_local().year
        dt = datetime(y, mon, d, hh, mm, tzinfo=TZ)
        if dt <= now_local():
            # если дата прошла — считаем, что речь о следующем году
            dt = datetime(y + 1, mon, d, hh, mm, tzinfo=TZ)
        return {"type": "once", "dt": dt, "text": m.group("txt").strip()}

    # 6) «сигнал/предупреждение»: сегодня в 14:00 ... в 15:00 ...
    m = re.match(
        rf"сегодня\s*в\s*{RE_TIME}.*?\bв\s*(?P<h2>\d{{1,2}}):(?P<m2>\d{{2}})\s*(?P<txt>.+)",
        t
    )
    if m:
        # первое время игнорируем (сигнал) — ставим на второе
        hh2, mm2 = int(m.group("h2")), int(m.group("m2"))
        dt = now_local().replace(hour=hh2, minute=mm2, second=0, microsecond=0)
        if dt <= now_local():
            dt += timedelta(days=1)
        return {"type": "once", "dt": dt, "text": m.group("txt").strip()}

    return None

# =============== ПЛАНИРОВАНИЕ ===============
def make_job_id(chat_id: int, when: datetime, text: str) -> str:
    return f"{chat_id}:{int(when.timestamp())}:{abs(hash(text))%10_000_000}"

async def fire_reminder(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = ctx.job.data
    chat_id = data["chat_id"]
    text = data["text"]
    when_iso = data["when_iso"]
    try:
        await ctx.bot.send_message(chat_id, f"⏰ {text}")
    finally:
        # одноразовое удаляем из БД
        DB["tasks"] = [t for t in DB["tasks"] if not (t["chat_id"] == chat_id and t["when_iso"] == when_iso and t["text"] == text and t["kind"] == "once")]
        save_db()

def schedule_existing(app: Application) -> None:
    for t in DB.get("tasks", []):
        if t["kind"] == "once":
            dt = datetime.fromisoformat(t["when_iso"])
            if dt > now_local():
                app.job_queue.run_once(
                    fire_reminder,
                    when=dt - now_local(),
                    data={"chat_id": t["chat_id"], "text": t["text"], "when_iso": t["when_iso"]},
                    name=t["job_id"]
                )
        elif t["kind"] == "daily":
            hhmm = datetime.fromisoformat(t["when_iso"]).time()
            app.job_queue.run_daily(
                fire_reminder,
                time=hhmm,
                data={"chat_id": t["chat_id"], "text": t["text"], "when_iso": t["when_iso"]},
                name=t["job_id"]
            )

def add_task_once(app: Application, chat_id: int, dt: datetime, text: str) -> Task:
    job_id = make_job_id(chat_id, dt, text)
    app.job_queue.run_once(
        fire_reminder,
        when=dt - now_local(),
        data={"chat_id": chat_id, "text": text, "when_iso": dt.isoformat()},
        name=job_id
    )
    task = Task(chat_id=chat_id, when_iso=dt.isoformat(), text=text, kind="once", job_id=job_id)
    DB["tasks"].append(asdict(task))
    save_db()
    return task

def add_task_daily(app: Application, chat_id: int, t: time, text: str) -> Task:
    # when_iso храним как сегодня+это время (для удобного ISO)
    ref = now_local().replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    job_id = make_job_id(chat_id, ref, text)
    app.job_queue.run_daily(
        fire_reminder,
        time=t,
        data={"chat_id": chat_id, "text": text, "when_iso": ref.isoformat()},
        name=job_id
    )
    task = Task(chat_id=chat_id, when_iso=ref.isoformat(), text=text, kind="daily", job_id=job_id)
    DB["tasks"].append(asdict(task))
    save_db()
    return task

# =============== ГОЛОС ===============
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        await update.message.reply_text("🎙 Распознавание голоса не настроено (нет OPENAI_API_KEY). Отправьте текстом.")
        return

    # Скачиваем voice как ogg
    file = await context.bot.get_file(update.message.voice.file_id)
    b = await file.download_as_bytearray()

    # Отправляем в Whisper
    try:
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("file", b, filename="audio.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-1")
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                "https://api.openai.com/v1/audio/transcriptions",
                data=form,
                headers={"Authorization": f"Bearer {api_key}"}
            ) as resp:
                js = await resp.json()
        text = js.get("text", "").strip()
        if not text:
            await update.message.reply_text("Не удалось распознать голос. Попробуйте короче и чётче.")
            return
        # Скормим распознанный текст обработчику
        update.message.text = text
        await handle_key_or_text(update, context)
    except Exception as e:
        await update.message.reply_text(f"Ошибка распознавания: {e}")

# =============== ТЕХРАБОТЫ ===============
def maintenance_on() -> None:
    DB["maintenance"] = True
    save_db()

def maintenance_off() -> None:
    DB["maintenance"] = False
    save_db()

# =============== ХЕЛПЕРЫ ВЫВОДА ===============
HELP_TEXT = (
    "Бот запущен ✅\n\n"
    "Примеры:\n"
    "• сегодня в 16:00 купить молоко\n"
    "• завтра в 9:15 встреча с Андреем\n"
    "• в 22:30 позвонить маме\n"
    "• через 5 минут попить воды\n"
    "• каждый день в 09:30 зарядка\n"
    "• 30 августа в 09:00 заплатить за кредит\n"
    "• Сегодня в 14:00 (сигнал) напоминаю, встреча в 15:00 (само напоминание)\n"
    f"(часовой пояс: {TZ.key})"
)

def format_affairs(chat_id: int) -> str:
    items = []
    for i, t in enumerate(sorted([x for x in DB["tasks"] if x["chat_id"] == chat_id],
                                 key=lambda x: x["when_iso"])) :
        mark = "ежедневно" if t["kind"] == "daily" else datetime.fromisoformat(t["when_iso"]).strftime("%d.%m.%Y %H:%M")
        items.append(f"{i+1}. {mark} — {t['text']}")
    return "Ваши ближайшие дела:\n" + ("\n".join(items) if items else "пока пусто")

# =============== ХЕНДЛЕРЫ ===============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await update.message.reply_text(HELP_TEXT)

async def cmd_affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    await update.message.reply_text(format_affairs(update.effective_chat.id))

async def cmd_affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return
    if not context.args:
        await update.message.reply_text("Укажите номер: /affairs_delete 2")
        return
    try:
        idx = int(context.args[0]) - 1
    except:
        await update.message.reply_text("Номер должен быть целым.")
        return
    my_tasks = [t for t in DB["tasks"] if t["chat_id"] == update.effective_chat.id]
    if not (0 <= idx < len(my_tasks)):
        await update.message.reply_text("Неверный номер.")
        return
    victim = my_tasks[idx]
    # остановим job
    j = context.application.job_queue.get_jobs_by_name(victim["job_id"])
    for job in j:
        job.schedule_removal()
    # удалим из БД
    DB["tasks"] = [t for t in DB["tasks"] if t != victim]
    save_db()
    await update.message.reply_text("Удалил. ✅")

async def cmd_maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    maintenance_on()
    await update.message.reply_text("⚠️ Технические работы включены.")
async def cmd_maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    maintenance_off()
    await update.message.reply_text("✅ Технические работы выключены.")
    # уведомим ожидавших
    chats = set(DB.get("pending_chats", []))
    DB["pending_chats"] = []
    save_db()
    for cid in chats:
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает.")
        except:
            pass

async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1) Авторизация по ключу
    if not is_allowed(uid):
        if re.fullmatch(r"VIP\d{3}", msg):
            if msg in DB["access_keys"] and DB["access_keys"][msg] is None:
                DB["access_keys"][msg] = uid
                DB["allowed_users"] = sorted(list(allowed_users() | {uid}))
                save_db()
                await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
                await update.message.reply_text(HELP_TEXT)
            else:
                await update.message.reply_text("Ключ недействителен или уже использован.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # 2) Техработы
    if DB.get("maintenance", False) and uid != ADMIN_ID:
        s = "⚠️ Уважаемый пользователь, сейчас ведутся технические работы. Как только бот возобновит работу — мы сообщим."
        await update.message.reply_text(s)
        # запомним чат
        pend = set(DB.get("pending_chats", []))
        pend.add(chat_id)
        DB["pending_chats"] = list(pend)
        save_db()
        return

    # 3) Разбор естественного языка
    parsed = parse_text(msg.lower())
    if not parsed:
        await update.message.reply_text(
            "❓ Не понял формат. Примеры выше — нажмите /start"
        )
        return

    if parsed["type"] == "once":
        task = add_task_once(context.application, chat_id, parsed["dt"], parsed["text"])
        dt = datetime.fromisoformat(task.when_iso)
        await update.message.reply_text(
            f"✅ Ок, напомню {dt.strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: {TZ.key})"
        )
    else:
        task = add_task_daily(context.application, chat_id, parsed["t"], parsed["text"])
        t = datetime.fromisoformat(task.when_iso).strftime("%H:%M")
        await update.message.reply_text(f"✅ Ок, напомню каждый день в {t} — «{parsed['text']}».")
# =============== WEBHOOK ЗАПУСК ===============
async def set_bot_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру: /affairs_delete N"),
        BotCommand("maintenance_on", "Техработы: включить (админ)"),
        BotCommand("maintenance_off", "Техработы: выключить (админ)"),
    ])

def build_application() -> Application:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")
    app = Application.builder().token(token).build()

    # хендлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("affairs", cmd_affairs))
    app.add_handler(CommandHandler("affairs_delete", cmd_affairs_delete))
    app.add_handler(CommandHandler("maintenance_on", cmd_maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", cmd_maintenance_off))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    return app

def main():
    load_db()
    app = build_application()
    schedule_existing(app)

    # Установим команды (без await тут нельзя — используем post_init)
    async def post_init(_: Application):
        await set_bot_commands(app)

    app.post_init = post_init

    public_url = os.getenv("RENDER_EXTERNAL_URL")
    if not public_url:
        raise SystemExit("Нет переменной окружения RENDER_EXTERNAL_URL")
    port = int(os.getenv("PORT", "10000"))
    path = os.getenv("BOT_TOKEN")  # закрытый путь вебхука
    webhook_url = f"{public_url.rstrip('/')}/{path}"

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
