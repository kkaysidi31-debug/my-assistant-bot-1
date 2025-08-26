import logging
import re
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "ТОКЕН_ОТ_БОТФАЗЕРА"
ADMIN_ID = 123456789     # твой telegram id
TIMEZONE = pytz.timezone("Europe/Kaliningrad")
DATA_FILE = "data.json"

# Ключи доступа
ACCESS_KEYS = {"VIP001": None, "VIP002": None}
ALLOWED_USERS = set()

# Техработы
MAINTENANCE = False
PENDING_CHATS = set()

# ======= ЛОГИ =========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======= БАЗА =========
def load_db():
    global ALLOWED_USERS, ACCESS_KEYS
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ALLOWED_USERS = set(data.get("allowed_users", []))
        ACCESS_KEYS = data.get("access_keys", ACCESS_KEYS)
    except FileNotFoundError:
        save_db()

def save_db():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"allowed_users": list(ALLOWED_USERS),
                   "access_keys": ACCESS_KEYS}, f)

# ======= СТАРТ =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен ✅\n\nПримеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча с Андреем\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• Сегодня в 14:00 (сигнал) напоминалка\n\n"
        "(часовой пояс: Europe/Kaliningrad)"
    )

# ======= ТЕХРАБОТЫ =========
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
    # уведомим ожидавших
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        try:
            await context.bot.send_message(cid, "✅ Бот снова доступен!")
        except:
            pass

# ======= ДОБАВЛЕНИЕ НАПОМИНАНИЙ =========
TASKS = {}

def save_task(uid, chat_id, text, run_at):
    tid = str(len(TASKS) + 1)
    TASKS[tid] = {"uid": uid, "chat": chat_id, "text": text, "time": run_at}
    return tid

def remove_task(uid, task_id):
    if task_id in TASKS and TASKS[task_id]["uid"] == uid:
        del TASKS[task_id]
        return True
    return False

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
        await update.message.reply_text("⚠️ Задача не найдена.")

# ======= ОБРАБОТКА ТЕКСТА =========
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE
    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # Проверка доступа
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", msg):
            if msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
                ACCESS_KEYS[msg] = uid
                ALLOWED_USERS.add(uid)
                save_db()
                await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
            else:
                await update.message.reply_text("Ключ недействителен ❌.")
        else:
            await update.message.reply_text("Бот приватный.Введите ключ в формате ABC123.")
        return

    # Техработы
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("⚠️ Бот на техобслуживании, попробуйте позже.")
        return

    # Простейший парсинг: "через X минут ..."
    if "через" in msg and "минут" in msg:
        try:
            n = int(msg.split("через")[1].split("минут")[0].strip())
            run_at = datetime.now(TIMEZONE) + timedelta(minutes=n)
            tid = save_task(uid, chat_id, msg, run_at.isoformat())
            await update.message.reply_text(f"✅ Напоминание сохранено на {run_at.strftime('%H:%M')} (ID {tid})")
        except:
            await update.message.reply_text("Не понял время, попробуйте ещё раз.")
        return

    await update.message.reply_text("Принято ✅")

# ======= MAIN =========
def main():
    load_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))
    app.add_handler(CommandHandler("delete", delete_task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()

if __name__ == "__main__":
    main()
