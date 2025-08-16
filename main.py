import os
import asyncio
import logging
from telegram.ext import Application, CommandHandler, ContextTypes

# === окружение ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
_render_url_env = "RENDER_EXTERNAL_URL"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
log = logging.getLogger("bot")

# === handlers ===
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен ✅")

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    return app

async def set_webhook_when_ready(app: Application, url_path: str):
    tries = 0
    public_url = os.getenv(_render_url_env, "").strip()
    while not public_url and tries < 120:  # ждём до 2 минут
        await asyncio.sleep(1)
        public_url = os.getenv(_render_url_env, "").strip()
        tries += 1

    if not public_url:
        log.warning("Не дождался RENDER_EXTERNAL_URL — вебхук не поставлен (бот всё равно слушает порт).")
        return

    webhook_url = f"{public_url.rstrip('/')}/{url_path}"
    try:
        await app.bot.set_webhook(url=webhook_url, allowed_updates=[])
        log.info(f"Webhook set to: {webhook_url}")
    except Exception as e:
        log.exception(f"Не удалось поставить вебхук: {e}")

async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан"); raise SystemExit(1)

    app = build_app()
    url_path = BOT_TOKEN

    # запускаем фоновую задачу постановки вебхука
    asyncio.create_task(set_webhook_when_ready(app, url_path))

    log.info("Starting HTTP server (webhook handler)…")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=None,   # ставим вручную позже
        close_loop=False,   # чтобы PTB не закрыл event loop
    )

if __name__ == "__main__":
    asyncio.run(main())   # правильный способ (один цикл событий)
