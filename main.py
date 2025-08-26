import logging
import re
import json
import os
from datetime import datetime, timedelta
import pytz
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------ НАСТРОЙКИ ------------------
TOKEN = os.getenv("BOT_TOKEN")  # токен бота из Render
ADMIN_ID = 963586834            # твой Telegram ID
TIMEZONE = pytz.timezone("Europe/Kaliningrad")

DB_FILE = "reminders.json"

ACCESS_KEYS = {"VIP001": None}
ALLOWED_USERS = set()

MAINTENANCE = False
PENDING_CHATS = set()

REMINDERS = []  # список напоминаний
# ------------------------------------------------


# ------------------ БАЗА ------------------
def save_db():
    data = {
        "reminders": REMINDERS,
        "allowed": list(ALLOWED_USERS)
    }
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_db():
    global REMINDERS, ALLOWED_USERS
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            REMINDERS = data.get("reminders", [])
            ALLOWED_USERS.update(data.get("allowed", []))
# ------------------------------------------------


# ------------------ КОМАНДЫ ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча с Андреем\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• Сегодня в 14:00 (сигнал) напоминаю, встреча в 15:00\n\n"
        "(часовой пояс: Europe/Kaliningrad)"
    )
    await update.message.reply_text(text)


async def affairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not REMINDERS:
        await update.message.reply_text("У вас пока нет дел ✅")
        return
    text = "Ваши ближайшие дела:\n"
    for i, r in enumerate(REMINDERS, start=1):
        text += f"{i}. {r['time']} — {r['text']}\n"
    await update.message.reply_text(text)


async def affairs_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите номер дела для удаления: /affairs_delete N")
        return
    try:
        idx = int(context.args[0]) - 1
        if 0 <= idx < len(REMINDERS):
            removed = REMINDERS.pop(idx)
            save_db()
            await update.message.reply_text(f"❌ Удалено: {removed['text']}")
        else:
            await update.message.reply_text("Неверный номер.")
    except ValueError:
        await update.message.reply_text("Нужно указать число.")


async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    await update.message.reply_text("⚠️ Бот переведён в режим технических работ.")


async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    await update.message.reply_text("✅ Бот снова работает.")
    for chat_id in list(PENDING_CHATS):
        try:
            await context.bot.send_message(chat_id, "✅ Бот снова доступен.")
        except:
            pass
    PENDING_CHATS.clear()
# ------------------------------------------------


# ------------------ ОСНОВНОЙ ОБРАБОТЧИК ------------------
async def handle_key_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1) Проверка ключа
    if uid not in ALLOWED_USERS:
        if msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
            ACCESS_KEYS[msg] = uid
            ALLOWED_USERS.add(uid)
            save_db()
     await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате ABC123.")
        return

    # 2) Проверка на техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("⚠️ Уважаемый пользователь, бот сейчас на техобслуживании.")
        return

    # 3) Парсинг напоминания
    when = None
    text = None

    # через N минут/часов
    m = re.match(r"через (\d+) минут[ы]?\s+(.*)", msg, re.I)
    if m:
        when = datetime.now(TIMEZONE) + timedelta(minutes=int(m.group(1)))
        text = m.group(2)

    m = re.match(r"через (\d+) час(а|ов)?\s+(.*)", msg, re.I)
    if m:
        when = datetime.now(TIMEZONE) + timedelta(hours=int(m.group(1)))
        text = m.group(3)

    # сегодня
    m = re.match(r"сегодня в (\d{1,2}):(\d{2})\s+(.*)", msg, re.I)
    if m:
        when = datetime.now(TIMEZONE).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        text = m.group(3)

    # завтра
    m = re.match(r"завтра в (\d{1,2}):(\d{2})\s+(.*)", msg, re.I)
    if m:
        when = (datetime.now(TIMEZONE) + timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        text = m.group(3)

    # конкретная дата
    m = re.match(r"(\d{1,2})\s+([а-я]+)\s+в\s+(\d{1,2}):(\d{2})\s+(.*)", msg, re.I)
    if m:
        day, month, hour, minute, txt = m.groups()
        months = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
            "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
        }
        when = datetime.now(TIMEZONE).replace(month=months[month.lower()], day=int(day),
                                              hour=int(hour), minute=int(minute), second=0, microsecond=0)
        text = txt

    if when and text:
        REMINDERS.append({"time": when.strftime("%Y-%m-%d %H:%M"), "text": text})
        save_db()
        await update.message.reply_text(f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{text}»")
        context.job_queue.run_once(remind, when, chat_id=chat_id, data=text)
    else:
        await update.message.reply_text("❓ Не понял формат. Попробуйте ещё раз.")
# ------------------------------------------------


# ------------------ ФУНКЦИЯ НАПОМИНАНИЯ ------------------
async def remind(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, f"🔔 Напоминаю: {job.data}")
# ------------------------------------------------


# ------------------ MAIN ------------------
def main():
    load_db()
    application = Application.builder().token(TOKEN).build()

    # команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("affairs", affairs))
    application.add_handler(CommandHandler("affairs_delete", affairs_delete))
    application.add_handler(CommandHandler("maintenance_on", maintenance_on))
    application.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # обработка текста
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    # меню команд
    commands = [
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("maintenance_on", "Техработы (вкл)"),
        BotCommand("maintenance_off", "Техработы (выкл)"),
    ]
    application.bot.set_my_commands(commands)

    application.run_polling()

if __name__ == "__main__":
    main()
