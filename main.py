import os
import asyncio
import logging
from telegram.ext import Application, CommandHandler, ContextTypes

# === окружение ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))  # Render подставляет свой порт сюда
# URL Render может подставиться с задержкой — будем ждать его в фоне
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
    """
    Фоновая задача: ждём, пока Render выдаст внешний URL,
    и тогда ставим вебхук. Повторяем попытки до 2 минут.
    """
    tries = 0
    public_url = os.getenv(_render_url_env, "").strip()
    while not public_url and tries < 120:  # до ~2 минут
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

    # делаем путь вебхука равным токену — просто и безопасно
    url_path = BOT_TOKEN

    # запускаем фоновую задачу: ждёт URL от Render и ставит вебхук
    asyncio.create_task(set_webhook_when_ready(app, url_path))

    # запускаем встроенный HTTP-сервер PTB, чтобы Render увидел открытый порт
    # ВАЖНО: НЕ передаём webhook_url здесь (пусть будет None), чтобы не падать без внешнего URL
    log.info("Starting HTTP server (webhook handler)…")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,             # обязательно из os.getenv("PORT")
        url_path=url_path,     # путь, на который Telegram будет постить апдейты
        webhook_url=None,      # вебхук поставим фоновой задачей
        close_loop=False,      # не закрывать активный event loop на Render
    )

if __name__ == "__main__":
    # Запускаем в текущем event loop, без asyncio.run (иначе конфликт на Render)
    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
