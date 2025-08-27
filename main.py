import logging, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = "8492146866:AAHR_lrK9o18dGI0-ngfkVZUhbPQ4YSmr48"

# healthcheck для Render
class Health(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

def start_health():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health)
    Thread(target=HTTPServer(("0.0.0.0", port), Health).serve_forever, daemon=True).start()

logging.basicConfig(level=logging.INFO)

async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Бот жив. Напиши любой текст — я повторю.")

async def echo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Эхо: " + (u.message.text or ""))

def main():
    start_health()
    app = Application.builder().token(BOT_TOKEN).build()
    async def on_start(app_):
        await app_.bot.delete_webhook(drop_pending_updates=True)
        import telegram, sys
        logging.info("PTB=%s Python=%s", getattr(telegram,"__version__","?"), sys.version)
    app.post_init = on_start
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.run_polling()

if __name__ == "__main__":
    main()
