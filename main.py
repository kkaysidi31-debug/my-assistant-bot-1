import os
import threading
from datetime import datetime, timedelta
import pytz
from flask import Flask, request
import telebot

# ======================
# Настройки
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
ADMIN_ID = 963586834  # твой ID
TZ = pytz.timezone("Europe/Kaliningrad")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ======================
# Хранилище
# ======================
TASKS = {}
MAINTENANCE = False
PENDING_CHATS = set()


# ======================
# Функции
# ======================
def set_task(uid, text, delay_sec):
    run_at = datetime.now(TZ) + timedelta(seconds=delay_sec)

    def job():
        try:
            bot.send_message(uid, f"⏰ Напоминание: {text}")
        except Exception as e:
            print("Ошибка отправки:", e)

    t = threading.Timer(delay_sec, job)
    t.start()

    task_id = f"{uid}_{int(run_at.timestamp())}"
    TASKS[task_id] = {"uid": uid, "text": text, "time": run_at}
    return task_id, run_at


def remove_task(task_id):
    return TASKS.pop(task_id, None)


# ======================
# Команды
# ======================
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message,
        "Бот запущен ✅\n"
        "Примеры:\n"
        "• через 60 секунд выпить воды\n"
        "• через 300 секунд сходить в магазин\n"
        "• /delete ID — удалить задачу\n"
        "• /maintenance_on — включить тех. работы (только админ)\n"
        "• /maintenance_off — выключить тех. работы"
    )


@bot.message_handler(commands=["delete"])
def delete(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Укажи ID задачи для удаления")
        return
    task_id = parts[1]
    removed = remove_task(task_id)
    if removed:
        bot.reply_to(message, "Задача удалена ✅")
    else:
        bot.reply_to(message, "Задача не найдена ❌")


@bot.message_handler(commands=["maintenance_on"])
def maintenance_on(message):
    global MAINTENANCE
    if message.from_user.id != ADMIN_ID:
        return
    MAINTENANCE = True
    bot.reply_to(message, "🟡 Технические работы включены.")


@bot.message_handler(commands=["maintenance_off"])
def maintenance_off(message):
    global MAINTENANCE
    if message.from_user.id != ADMIN_ID:
        return
    MAINTENANCE = False
    bot.reply_to(message, "🟢 Технические работы выключены.")
    while PENDING_CHATS:
        cid = PENDING_CHATS.pop()
        bot.send_message(cid, "✅ Бот снова доступен!")


# ======================
# Парсинг напоминаний
# ======================
@bot.message_handler(func=lambda m: True)
def handle_message(message):
    global MAINTENANCE
    uid = message.from_user.id
    text = message.text.strip()

    if MAINTENANCE and uid != ADMIN_ID:
        PENDING_CHATS.add(uid)
        bot.reply_to(message, "⚠️ Бот на техобслуживании, попробуй позже.")
        return

    if text.startswith("через"):
        try:
            parts = text.split()
            delay = int(parts[1])  # секунды
            task_text = " ".join(parts[2:])
            task_id, run_at = set_task(uid, task_text, delay)
            bot.reply_to(message, f"✅ Задача сохранена (ID: {task_id}), напомню в {run_at.strftime('%H:%M:%S')}")
        except Exception:
            bot.reply_to(message, "Не понял формат. Пример: через 60 выпить воды")
    else:
        bot.reply_to(message, "Пример: 'через 300 сходить в магазин'")


# ======================
# Flask Webhook
# ======================
@app.route("/" + BOT_TOKEN, methods=["POST"])
def getMessage():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200


@app.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url="https://YOUR_RENDER_URL.onrender.com/" + BOT_TOKEN)
    return "Webhook set", 200


# ======================
# Запуск
# ======================
if name == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
