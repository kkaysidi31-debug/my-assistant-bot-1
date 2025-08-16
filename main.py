import os
import logging
from telegram.ext import Application, CommandHandler, ContextTypes

# --- окружение ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))                     # Render даёт порт сюда
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()   # Render заполняет автоматически

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger("bot")

# --- handlers ---
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен ✅")

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    return app

if name == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в переменных окружения")

    app = build_app()

    # путь вебхука — токен (быстро и безопасно)
    url_path = BOT_TOKEN

    # если Render уже подставил внешний URL — ставим вебхук сразу
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/{url_path}" if PUBLIC_URL else None
    if webhook_url:
        log.info(f"Запускаю с вебхуком: {webhook_url}")
    else:
        log.warning("RENDER_EXTERNAL_URL пуст — запускаю сервер без установки вебхука. "
                    "После первого билда сделай ещё один Deploy, чтобы URL появился.")

    # ВАЖНО: здесь НЕТ asyncio.run/await — это синхронный, устойчивый вариант
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,   # может быть None на самом первом деплое
        close_loop=False,          # не трогать внешний event loop у Render
    )
