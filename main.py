import os
import json
import re
import asyncio
from datetime import datetime, timedelta

import pytz
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Update as TgUpdate,
    Message,
    BotCommand,
)

# -------------------- КОНФИГ --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ADMIN_ID = 963586834  # твой ID
TZ = pytz.timezone("Europe/Kaliningrad")

DATA_FILE = "data.json"   # тут храним пользователей/ключи/задачи/флаги
BASE_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")  # например https://your-service.onrender.com
PORT = int(os.getenv("PORT", "10000"))

# Ключи доступа (без подсказок пользователям!)
DEFAULT_ACCESS_KEYS = ["VIP001", "VIP002", "VIP003", "VIP100"]

# -------------------- ГЛОБАЛЬНЫЕ --------------------
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)

STATE = {
    "allowed_users": set(),
    "access_keys": {k: None for k in DEFAULT_ACCESS_KEYS},
    "maintenance": False,
    "pending_chats": set(),  # кто писал во время техработ
    "tasks": []              # список словарей: {id, uid, chat_id, text, run_at_iso, daily}
}


# -------------------- УТИЛИТЫ ХРАНИЛКИ --------------------
def load_db():
    if not os.path.exists(DATA_FILE):
        save_db()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    STATE["allowed_users"] = set(raw.get("allowed_users", []))
    STATE["access_keys"] = raw.get("access_keys", {k: None for k in DEFAULT_ACCESS_KEYS})
    STATE["maintenance"] = bool(raw.get("maintenance", False))
    STATE["pending_chats"] = set(raw.get("pending_chats", []))
    STATE["tasks"] = raw.get("tasks", [])


def save_db():
    raw = {
        "allowed_users": list(STATE["allowed_users"]),
        "access_keys": STATE["access_keys"],
        "maintenance": STATE["maintenance"],
        "pending_chats": list(STATE["pending_chats"]),
        "tasks": STATE["tasks"],
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def new_task_id(uid: int, when: datetime) -> str:
    return f"{uid}-{int(when.timestamp())}"


def now_tz() -> datetime:
    return datetime.now(TZ)


# -------------------- ПЛАНИРОВЩИК --------------------
async def fire_reminder(chat_id: int, text: str, task_id: str, daily: bool):
    try:
        await bot.send_message(chat_id, f"⏰ Напоминание: <b>{text}</b>")
    finally:
        if daily:
            # ежедневное задание — ничего не удаляем
            return
        # одноразовое — удаляем из базы
        STATE["tasks"] = [t for t in STATE["tasks"] if t["id"] != task_id]
        save_db()


def schedule_task(task: dict):
    """Поднять задачу в APScheduler (если в будущем)."""
    when = datetime.fromisoformat(task["run_at_iso"])
    if when.tzinfo is None:
        when = TZ.localize(when)
    if when > now_tz():
        scheduler.add_job(
            fire_reminder,
            DateTrigger(run_date=when),
            args=[task["chat_id"], task["text"], task["id"], task.get("daily", False)],
            id=task["id"],
            replace_existing=True,
            misfire_grace_time=60
        )


def reschedule_all():
    for job in list(scheduler.get_jobs()):
        job.remove()
    for t in STATE["tasks"]:
        schedule_task(t)


# -------------------- ПАРСИНГ РУССКИХ ФРАЗ --------------------
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}


def parse_when_and_text(msg: str):
    """Возвращает (when: datetime|None, text: str, daily: bool). None — если не распозналось."""
    m = msg.strip().lower()

    # через N минут
    m1 = re.search(r"через\s+(\d+)\s+минут", m)
    if m1:
        minutes = int(m1.group(1))
        rest = re.sub(r"через\s+\d+\s+минут", "", msg, flags=re.IGNORECASE).strip()
        when = now_tz() + timedelta(minutes=minutes)
        return when, (rest or "напомнить"), False

    # сегодня в HH:MM
    m2 = re.search(r"сегодня\s+в\s+(\d{1,2}):(\d{2})", m)
    if m2:
        hh, mm = int(m2.group(1)), int(m2.group(2))
        when = now_tz().replace(hour=hh, minute=mm, second=0, microsecond=0)
        rest = re.sub(r"сегодня\s+в\s+\d{1,2}:\d{2}", "", msg, flags=re.IGNORECASE).strip()
        return when, (rest or "напомнить"), False

    # завтра в HH:MM
    m3 = re.search(r"завтра\s+в\s+(\d{1,2}):(\d{2})", m)
    if m3:
        hh, mm = int(m3.group(1)), int(m3.group(2))
        when = now_tz().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        rest = re.sub(r"завтра\s+в\s+\d{1,2}:\d{2}", "", msg, flags=re.IGNORECASE).strip()
        return when, (rest or "напомнить"), False

    # каждый день в HH:MM
    m4 = re.search(r"каждый\s+день\s+в\s+(\d{1,2}):(\d{2})", m)
    if m4:
        hh, mm = int(m4.group(1)), int(m4.group(2))
        when = now_tz().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now_tz():
            when += timedelta(days=1)
        rest = re.sub(r"каждый\s+день\s+в\s+\d{1,2}:\d{2}", "", msg, flags=re.IGNORECASE).strip()
        return when, (rest or "напомнить"), True

    # DD.MM.YYYY в HH:MM
    m5 = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+в\s+(\d{1,2}):(\d{2})", m)
    if m5:
        d, mo, y, hh, mm = map(int, m5.groups())
        when = TZ.localize(datetime(y, mo, d, hh, mm))
        rest = re.sub(r"\d{1,2}\.\d{1,2}\.\d{4}\s+в\s+\d{1,2}:\d{2}", "", msg, flags=re.IGNORECASE).strip()
        return when, (rest or "напомнить"), False

    # DD <месяц> [в HH:MM]
    m6 = re.search(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+в\s+(\d{1,2}):(\d{2}))?", m)
    if m6:
        d = int(m6.group(1))
        mo = MONTHS[m6.group(2)]
        hh = int(m6.group(3)) if m6.group(3) else 9
        mm = int(m6.group(4)) if m6.group(4) else 0
        y = now_tz().year
        when = TZ.localize(datetime(y, mo, d, hh, mm))
        rest = re.sub(r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+в\s+\d{1,2}:\d{2})?", "", msg, flags=re.IGNORECASE).strip()
        return when, (rest or "напомнить"), False

    # в HH:MM <текст>
    m7 = re.search(r"\bв\s+(\d{1,2}):(\d{2})\b", m)
    if m7:
        hh, mm = int(m7.group(1)), int(m7.group(2))
        when = now_tz().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now_tz():
            when += timedelta(days=1)
        rest = re.sub(r"\bв\s+\d{1,2}:\d{2}\b", "", msg, flags=re.IGNORECASE).strip()
        return when, (rest or "напомнить"), False

    return None, "", False


# -------------------- ХЭНДЛЕРЫ --------------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await bot.set_my_commands([
        BotCommand(command="start", description="Помощь и примеры"),
        BotCommand(command="affairs", description="Список дел"),
        BotCommand(command="affairs_delete", description="Удалить дело по номеру"),
        BotCommand(command="maintenance_on", description="(админ) Включить техработы"),
        BotCommand(command="maintenance_off", description="(админ) Выключить техработы"),
    ])
    await m.answer(
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча с Андреем\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• Сегодня в 14:00 (сигнал) встреча в 15:00\n"
        f"(часовой пояс: Europe/Kaliningrad)\n\n"
        "Бот приватный. Введите ключ доступа в формате ABC123."
    )


@dp.message(Command("maintenance_on"))
async def maintenance_on(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    STATE["maintenance"] = True
    save_db()
    await m.answer("🟡 Технические работы включены.")


@dp.message(Command("maintenance_off"))
async def maintenance_off(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    STATE["maintenance"] = False
    save_db()
    await m.answer("🟢 Технические работы выключены.")
    # оповестим ожидавших
    while STATE["pending_chats"]:
        cid = STATE["pending_chats"].pop()
        try:
            await bot.send_message(cid, "✅ Бот снова работает.")
        except Exception:
            pass
    save_db()


@dp.message(Command("affairs"))
async def list_affairs(m: Message):
    uid = m.from_user.id
    items = [t for t in STATE["tasks"] if t["uid"] == uid]
    if not items:
        await m.answer("Пока дел нет.")
        return
    items_sorted = sorted(items, key=lambda t: t["run_at_iso"])
    lines = ["Ваши ближайшие дела:"]
    for i, t in enumerate(items_sorted, start=1):
        when = datetime.fromisoformat(t["run_at_iso"]).astimezone(TZ)
        dstr = when.strftime("%d.%m.%Y %H:%M")
        lines.append(f"{i}. {dstr} — {t['text']}")
    await m.answer("\n".join(lines))


@dp.message(Command("affairs_delete"))
async def affairs_delete(m: Message, command: CommandObject):
    uid = m.from_user.id
    if not command.args or not command.args.strip().isdigit():
        await m.answer("Укажи номер: /affairs_delete 2")
        return
    idx = int(command.args.strip())
    items = [t for t in STATE["tasks"] if t["uid"] == uid]
    if not items or idx < 1 or idx > len(items):
        await m.answer("Неверный номер.")
        return
    items_sorted = sorted(items, key=lambda t: t["run_at_iso"])
    task = items_sorted[idx - 1]
    # удалить из планировщика
    try:
        job = scheduler.get_job(task["id"])
        if job:
            job.remove()
    except Exception:
        pass
    # удалить из базы
    STATE["tasks"] = [t for t in STATE["tasks"] if t["id"] != task["id"]]
    save_db()
    await m.answer("✅ Дело удалено.")


@dp.message(F.text)
async def text_router(m: Message):
    text = (m.text or "").strip()
    uid = m.from_user.id
    chat_id = m.chat.id

    # авторизация
    if uid not in STATE["allowed_users"]:
        if re.fullmatch(r"[A-Z]{3}\d{3}", text):
            # ключ формально похож; принимаем только те, что в списке и ещё не заняты
            if text in STATE["access_keys"] and STATE["access_keys"][text] is None:
                STATE["access_keys"][text] = uid
                STATE["allowed_users"].add(uid)
                save_db()
                await m.answer("Ключ принят ✅. Теперь можно ставить напоминания.")
            else:
                await m.answer("❌ Ключ недействителен.")
        else:
            await m.answer("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # техработы
    if STATE["maintenance"] and uid != ADMIN_ID:
        STATE["pending_chats"].add(chat_id)
        save_db()
        await m.answer("⚠️ Уважаемый пользователь, идут технические работы. Попробуйте позже.")
        return

    # парсинг времени
    when, todo_text, daily = parse_when_and_text(text)
    if not when:
        await m.answer("❓ Не понял формат. Примеры: «сегодня в 16:00 позвонить», «через 5 минут выпить воды», «каждый день в 09:30 зарядка».")
        return

    # сохраняем и планируем
    task_id = new_task_id(uid, when)
    task = {
        "id": task_id,
        "uid": uid,
        "chat_id": chat_id,
        "text": todo_text,
        "run_at_iso": when.isoformat(),
        "daily": daily
    }
    STATE["tasks"].append(task)
    save_db()
    schedule_task(task)

    when_str = when.strftime("%Y-%m-%d %H:%M")
    if daily:
        await m.answer(f"✅ Ежедневное напоминание в {when.strftime('%H:%M')} — «{todo_text}».")
    else:
        await m.answer(f"✅ Ок, напомню {when_str} — «{todo_text}». (TZ: Europe/Kaliningrad)")


# -------------------- WEBHOOK СЕРВЕР --------------------
async def handle_webhook(request: web.Request):
    data = await request.json()
    await dp.feed_update(bot, TgUpdate(**data))
    return web.Response(text="ok")

async def on_startup(app: web.Application):
    load_db()
    scheduler.start()
    reschedule_all()

    if not BASE_URL:
        print("⚠️ WEBHOOK_URL/RENDER_EXTERNAL_URL не задан — работа не начнётся.")
        return
    url = f"{BASE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    await bot.set_webhook(url, drop_pending_updates=True)
    print("✅ Webhook set:", url)

async def on_cleanup(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=False)
    scheduler.shutdown(wait=False)
    save_db()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post(f"/webhook/{BOT_TOKEN}", handle_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


# -------------------- Точка входа --------------------
if __name__ == "__main__":
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError("Укажи BOT_TOKEN через переменную окружения.")

    web.run_app(build_app(), host="0.0.0.0", port=PORT)
