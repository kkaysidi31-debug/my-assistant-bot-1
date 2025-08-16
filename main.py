target = target + timedelta(days=1)
        return {"once_at": target, "text": text}

    return None

async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text or ""
    parsed = parse_message(msg)
    if not parsed:
        # тихо игнорим или подсказываем
        await update.message.reply_text("Не понял формат. Примеры: «сегодня в 16:00 купить молоко», "
                                        "«через 5 минут попить воды», «каждый день в 09:30 зарядка».")
        return

    if "after" in parsed:
        when_utc = to_utc(now_local() + parsed["after"])
        context.application.job_queue.run_once(
            remind_callback,
            when=when_utc,
            chat_id=update.effective_chat.id,
            data={"text": parsed["text"]},
        )
        human_time = (now_local() + parsed["after"]).strftime("%Y-%m-%d %H:%M")
        await update.message.reply_text(f"✅ Ок, напомню {human_time} — «{parsed['text']}». (TZ: {TZ_NAME})")
        return

    if "once_at" in parsed:
        when_utc = to_utc(parsed["once_at"])
        context.application.job_queue.run_once(
            remind_callback,
            when=when_utc,
            chat_id=update.effective_chat.id,
            data={"text": parsed["text"]},
        )
        await update.message.reply_text(
            f"✅ Ок, напомню {parsed['once_at'].strftime('%Y-%m-%d %H:%M')} — «{parsed['text']}». (TZ: {TZ_NAME})"
        )
        return

    if "daily_at" in parsed:
        context.application.job_queue.run_daily(
            remind_callback,
            time=parsed["daily_at"],
            chat_id=update.effective_chat.id,
            data={"text": parsed["text"]},
            name=f"daily-{update.effective_chat.id}-{parsed['daily_at'].strftime('%H%M')}",
        )
        await update.message.reply_text(
            f"✅ Ежедневное напоминание в {parsed['daily_at'].strftime('%H:%M')} — «{parsed['text']}». (TZ: {TZ_NAME})"
        )
        return

# ---------- приложение / вебхук ----------
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в переменных окружения")

    app = build_app()
    url_path = BOT_TOKEN
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/{url_path}" if PUBLIC_URL else None
    if webhook_url:
        log.info(f"Запускаю с вебхуком: {webhook_url}")
    else:
        log.warning("RENDER_EXTERNAL_URL пуст — сервер стартует без вебхука, сделай повторный Deploy после первого билда.")

    # Синхронный устойчивый запуск на Render
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,  # может быть None на самом первом запуске
        close_loop=False,
    )
