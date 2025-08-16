import os
import asyncio
import logging
from telegram.ext import Application, CommandHandler, ContextTypes

# === окружение ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))                   # Render даёт порт сюда
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()  # Render задаёт после билда

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
log = logging.getLogger("bot")

# === handlers ===
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен ✅")

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    return app

async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан"); raise SystemExit(1)

    if not PUBLIC_URL:
        # на самом первом деплое URL может быть пуст — просто перезапусти деплой
        log.warning("RENDER_EXTERNAL_URL пока пуст. Перезапусти деплой после первого билда.")
        await asyncio.sleep(5); raise SystemExit(1)

    app = build_app()

    # делаем путь вебхука равным токену
    url_path = BOT_TOKEN
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/{url_path}"
    log.info(f"Ставлю вебхук: {webhook_url}")

    # ВАЖНО: в PTB 21.x используем url= (НЕ webhook_url=)
    await app.bot.set_webhook(url=webhook_url, allowed_updates=[])

    # встроенный aiohttp-сервер PTB
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")