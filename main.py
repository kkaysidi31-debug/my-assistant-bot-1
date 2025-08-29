await schedule(ctx.application, t)
        if t.type == "once":
            await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — {fmt(datetime.fromisoformat(t.run_at_utc))}")
        elif t.type == "daily":
            await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — каждый день в {t.hour:02d}:{t.minute:02d}")
        else:
            await update.message.reply_text(f"✅ Отлично, напомню: «{t.title}» — каждый месяц, {t.day_of_month}-го в {t.hour:02d}:{t.minute:02d}")
    except Exception:
        logging.exception("schedule failed")
        await update.message.reply_text("⚠️ Задачу сохранил, но возникла ошибка при планировании. Попробую ещё раз через минуту.")
        # запасной рескейджул через минуту
        ctx.application.job_queue.run_once(lambda c: asyncio.create_task(reschedule_all(ctx.application)), when=timedelta(minutes=1))

# -------------------- KEEP-ALIVE HTTP --------------------
async def handle_root(request):
    return web.Response(text="alive")

async def run_web():
    app = web.Application()
    app.add_routes([web.get('/', handle_root)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("HTTP keep-alive running on port %s", PORT)

# -------------------- СТАРТ --------------------
async def on_startup(app: Application):
    # убрать возможный старый webhook на всякий
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning("delete_webhook failed: %s", e)
    try:
        await reschedule_all(app)
    except Exception:
        logging.exception("Reschedule failed")

async def set_commands(app: Application):
    cmds = [
        BotCommand("start", "Помощь и примеры"),
        BotCommand("affairs", "Список дел"),
        BotCommand("affairs_delete", "Удалить дело по номеру"),
        BotCommand("maintenance_on", "Техработы: включить (только админ)"),
        BotCommand("maintenance_off", "Техработы: выключить (только админ)"),
        BotCommand("issue_key", "Выдать новый ключ (только админ)"),
        BotCommand("keys_left", "Статистика ключей (только админ)"),
        BotCommand("keys_free", "Свободные ключи (число, только админ)"),
        BotCommand("keys_used", "Использованные ключи (только админ)"),
        BotCommand("keys_reset", "Пополнить пул ключей до 1000"),
    ]
    await app.bot.set_my_commands(cmds)

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render → Environment.")
    init_db()
    ensure_keys_pool(1000)

    # веб-сервер для пингов
    asyncio.create_task(run_web())

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))

    app.add_handler(CommandHandler("maintenance_on", maintenance_on_cmd))
    app.add_handler(CommandHandler("maintenance_off", maintenance_off_cmd))

    app.add_handler(CommandHandler("issue_key", issue_key_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))
    app.add_handler(CommandHandler("keys_free", keys_free_cmd))
    app.add_handler(CommandHandler("keys_used", keys_used_cmd))
    app.add_handler(CommandHandler("keys_reset", keys_reset_cmd))

    # текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = on_startup

    # запускаем
    await set_commands(app)
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
