import os
import logging
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# === Конфигурация ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# === Глобальные переменные ===
MAINTENANCE = False
PENDING_CHATS = set()
ALLOWED_USERS = set()
ACCESS_KEYS = {"VIP001": None}

# === Логирование ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === База данных ===
def init_db():
    conn = sqlite3.connect("tasks.db")
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        text TEXT,
        run_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_task(user_id, chat_id, text, run_at):
    conn = sqlite3.connect("tasks.db")
    cur = conn.cursor()
    cur.execute("INSERT INTO tasks (user_id, chat_id, text, run_at) VALUES (?, ?, ?, ?)",
                (user_id, chat_id, text, run_at))
    conn.commit()
    conn.close()

def remove_task(user_id, task_id):
    conn = sqlite3.connect("tasks.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed

# === Хэндлеры ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен ✅\n\n"
        "Примеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• сегодня в 14:00 (сигнал) напоминание\n"
    )

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    await update.message.reply_text("🟡 Технические работы включены.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    if update.effective_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    await update.message.reply_text("🟢 Технические работы выключены.")
    # Уведомим ожидавших
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            await context.bot.send_message(cid, "✅ Бот снова работает!")
        except:
            pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_USERS
    msg = update.message.text.strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # Проверка приватности
    if uid not in ALLOWED_USERS:
        if msg in ACCESS_KEYS:
            ALLOWED_USERS.add(uid)
            await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа.")
        return

    # Проверка техработ
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("⚠️ Бот на техобслуживании, попробуйте позже.")
        return

    # Простейший парсинг времени (пример: "через 5 минут")
    if "через" in msg and "минут" in msg:
        try:
            n = int(msg.split("через")[1].split("минут")[0].strip())
            run_at = datetime.now() + timedelta(minutes=n)
            save_task(uid, chat_id, msg, run_at.isoformat())
            await update.message.reply_text(f"✅ Напоминание сохранено на {run_at.strftime('%H:%M:%S')}")
        except:
            await update.message.reply_text("Не понял время, попробуй еще раз.")
    else:
        await update.message.reply_text("Принято ✅")
async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажи ID задачи для удаления.")
        return
        
    task_id = context.args[0]
    removed = remove_task(uid, task_id)
    if removed:
        await update.message.reply_text("✅ Задача удалена.")
    else:
        await update.message.reply_text("❌ Задача не найдена.")

from telegram.ext import Application, CommandHandler, MessageHandler, filters

BOT_TOKEN = "ТВОЙ_ТОКЕН"

# Запуск бота
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("delete", delete_task))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))

    # обработка текста
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key_or_text))

    # запуск в режиме polling (или webhook, если настроен)
    app.run_polling()

if __name__ == "__main__":
    main()
