import logging
import re
import pytz
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# -------------------- НАСТРОЙКИ --------------------
BOT_TOKEN = "ТОКЕН_ТВОЕГО_БОТА"
ADMIN_ID = 123456789  # твой Telegram ID (чтобы управлять техобслуживанием)
TIMEZONE = pytz.timezone("Europe/Kaliningrad")

ALLOWED_USERS = set()
ACCESS_KEYS = {"VIP001": None, "VIP123": None}  # ключи доступа
PENDING_CHATS = set()
MAINTENANCE = False
TASKS = {}

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# -------------------- КОМАНДЫ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен ✅\n\nПримеры:\n"
        "• сегодня в 16:00 купить молоко\n"
        "• завтра в 9:15 встреча с Андреем\n"
        "• в 22:30 позвонить маме\n"
        "• через 5 минут попить воды\n"
        "• каждый день в 09:30 зарядка\n"
        "• 30 августа в 09:00 заплатить за кредит\n"
        "• сегодня в 14:00 (сигнал) встреча в 15:00\n"
        f"(часовой пояс: {TIMEZONE})"
    )

# -------------------- ТЕХОБСЛУЖИВАНИЕ --------------------
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
    await update.message.reply_text("🟢 Технические работы выключены.")

# -------------------- ОБРАБОТКА СООБЩЕНИЙ --------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE

    msg = (update.message.text or "").strip()
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1. Авторизация
    if uid not in ALLOWED_USERS:
        if re.fullmatch(r"VIP\d{3}", msg):
            if msg in ACCESS_KEYS and ACCESS_KEYS[msg] is None:
                ALLOWED_USERS.add(uid)
                ACCESS_KEYS[msg] = uid
                await update.message.reply_text("Ключ принят ✅. Теперь можно ставить напоминания.")
            else:
                await update.message.reply_text("❌ Ключ недействителен.")
        else:
            await update.message.reply_text("Бот приватный. Введите ключ доступа в формате VIP001.")
        return

    # 2. Проверка техобслуживания
    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(chat_id)
        await update.message.reply_text("⚠️ Бот на техобслуживании, попробуйте позже.")
        return

    # 3. Парсинг напоминаний
    if "через" in msg and "минут" in msg:
        try:
            n = int(msg.split("через")[1].split("минут")[0].strip())
            run_at = datetime.now(TIMEZONE) + timedelta(minutes=n)
            task_id = f"{uid}_{int(run_at.timestamp())}"
            TASKS[task_id] = (chat_id, msg, run_at)
            await update.message.reply_text(f"⏰ Напоминание сохранено на {run_at.strftime('%H:%M')}")
        except:
            await update.message.reply_text("❌ Не понял время, попробуйте ещё раз.")
    else:
        await update.message.reply_text("Принято ✅")

# -------------------- УДАЛЕНИЕ ЗАДАЧ --------------------
async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажи ID задачи для удаления.")
        return
    task_id = context.args[0]
    if task_id in TASKS:
        del TASKS[task_id]
        await update.message.reply_text("✅ Задача удалена.")
    else:
        await update.message.reply_text("❌ Задача не найдена.")

# -------------------- СБОРКА --------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("maintenance_on", maintenance_on))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off))
    app.add_handler(CommandHandler("delete", delete_task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()
