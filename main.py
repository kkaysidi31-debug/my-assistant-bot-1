import os
import asyncio
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message

# ======================
# Конфиг
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ADMIN_ID = 963586834  # твой Telegram ID
TZ = pytz.timezone("Europe/Kaliningrad")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ======================
# Хранилище
# ======================
TASKS = {}  # {task_id: {"uid":..., "text":..., "time":...}}
MAINTENANCE = False
PENDING_CHATS = set()


# ======================
# Функции
# ======================
async def set_task(uid, text, delay_sec):
    run_at = datetime.now(TZ) + timedelta(seconds=delay_sec)

    async def job():
        try:
            await bot.send_message(uid, f"⏰ Напоминание: {text}")
        except Exception as e:
            print("Ошибка отправки:", e)

    task_id = f"{uid}_{int(run_at.timestamp())}"
    TASKS[task_id] = {"uid": uid, "text": text, "time": run_at}

    asyncio.create_task(delayed_job(delay_sec, job))
    return task_id, run_at


async def delayed_job(delay, coro):
    await asyncio.sleep(delay)
    await coro()


def remove_task(task_id):
    return TASKS.pop(task_id, None)


# ======================
# Команды
# ======================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "Бот запущен ✅\n\n"
        "Пример: 'через 60 выпить воды'\n"
        "Команды:\n"
        "• /delete <ID> — удалить задачу\n"
        "• /maintenance_on — включить тех. работы\n"
        "• /maintenance_off — выключить тех. работы"
    )


@dp.message(Command("delete"))
async def cmd_delete(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Укажи ID задачи для удаления")
        return
    task_id = parts[1]
    removed = remove_task(task_id)
    if removed:
        await message.reply("Задача удалена ✅")
    else:
        await message.reply("Задача не найдена ❌")


@dp.message(Command("maintenance_on"))
async def cmd_maintenance_on(message: Message):
    global MAINTENANCE
    if message.from_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    await message.reply("🟡 Технические работы включены.")


@dp.message(Command("maintenance_off"))
async def cmd_maintenance_off(message: Message):
    global MAINTENANCE
    if message.from_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    await message.reply("🟢 Технические работы выключены.")
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            await bot.send_message(cid, "✅ Бот снова доступен!")
        except:
            pass


# ======================
# Обработка текста
# ======================
@dp.message()
async def handle_message(message: Message):
    global MAINTENANCE
    uid = message.from_user.id
    text = message.text.strip()

    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(uid)
        await message.reply("⚠️ Бот на техобслуживании, попробуй позже.")
        return

    if text.startswith("через"):
        try:
            parts = text.split()
            delay = int(parts[1])  # секунды
            task_text = " ".join(parts[2:])
            task_id, run_at = await set_task(uid, task_text, delay)
            await message.reply(
                f"✅ Задача сохранена (ID: {task_id}), напомню в {run_at.strftime('%H:%M:%S')}"
            )
        except Exception:
            await message.reply("Не понял формат. Пример: через 60 выпить воды")
    else:
        await message.reply("Пример: 'через 300 сходить в магазин'")


# ======================
# Запуск
# ======================
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
