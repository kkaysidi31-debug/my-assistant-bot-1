import os
import asyncio
import logging
from telegram.ext import Application, CommandHandler, ContextTypes

# --- переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8443"))                     # Render прокинет порт сюда
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()  # Render задаст этот URL

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
log = logging.getLogger("bot")

# --- handlers ---
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен ✅")

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    return app

# --- main ---
async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан")
        raise SystemExit(1)

    if not PUBLIC_URL:
        # На самом первом деплое URL может быть пуст — сделай повторный деплой.
        log.warning("RENDER_EXTERNAL_URL пока пуст. Перезапусти деплой после первого билда.")
        await asyncio.sleep(5)
        raise SystemExit(1)

    app = build_app()

    url_path = BOT_TOKEN  # делаем путь вебхука равным токену
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/{url_path}"
    log.info(f"Ставлю вебхук: {webhook_url}")

    # ВАЖНО: для PTB 21.x используем url= (НЕ webhook_url=)
    await app.bot.set_webhook(url=webhook_url, allowed_updates=[])

    # Встроенный aiohttp-сервер PTB
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,
        close_loop=False,  # чтобы не пытался закрыть активный event loop
    )

if name == "__main__":
    import asyncio
    asyncio.get_event_loop().run_until_complete(main())
