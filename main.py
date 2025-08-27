import os
import json
import re
from datetime import datetime, timedelta
import pytz
from flask import Flask

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ==========================
# Конфиг
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ADMIN_ID = 963586834   # твой ID
TZ = pytz.timezone("Europe/Kaliningrad")

# Состояния
TASKS_FILE = "tasks.json"
MAINTENANCE = False
PENDING_CHATS = set()

# ==========================
# Flask-заглушка (для Render)
# ==========================
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "✅ Bot is running!"

# ==========================
# Работа с задачами
# ==========================
def load_tasks():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=4)

def add_task(uid, chat_id, text, run_at):
    tasks = load_tasks()
    if str(uid) not in tasks:
        tasks[str(uid)] = []
    task_id = len(tasks[str(uid)]) + 1
    tasks[str(uid)].append({
        "id": task_id,
        "chat_id": chat_id,
        "text": text,
        "time": run_at
    })
    save_tasks(tasks)
    return task_id

def remove_task(uid, task_id):
    tasks = load_tasks()
    uid = str(uid)
    if uid not in tasks:
        return False
    new_list = [t for t in tasks[uid] if str(t["id"]) != str(task_id)]
    if len(new_list) != len(tasks[uid]):
        tasks[uid] = new_list
        save_tasks(tasks)
        return True
    return False

# ==========================
# Хендлеры
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я твой бот-напоминалка ✅")

async def add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = update.message.text

    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("⚠️ Бот на техобслуживании. Попробуйте позже.")
        return

    if "через" in msg and "минут" in msg:
        try:
            n = int(msg.split("через")[1].split("минут")[0].strip())
            run_at = datetime.now(TZ) + timedelta(minutes=n)
            add_task(uid, chat_id, msg, run_at.isoformat())
            await update.message.reply_text(f"✅ Напоминание сохранено на {run_at.strftime('%H:%M')}")
        except:
            await update.message.reply_text("Не понял время, попробуй ещё раз.")
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
        await update.message.reply_text("Задача удалена ✅")
    else:
        await update.message.reply_text("Задача с таким ID не найдена ❌")

async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    MAINTENANCE = True
    await update.message.reply_text("🟡 Технические работы включены.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    MAINTENANCE = False
    await update.message.reply_text("🟢 Технические работы завершены.")

# ==========================
# MAIN
# ==========================
def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("delete", delete_task))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder))

    app.run_polling()

if __name__ == "__main__":
    import threading
    threading.Thread(target=run_bot).start()
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
