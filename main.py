voice = update.message.voice
    if not voice:
        await update.message.reply_text("Не нашёл голосовое в сообщении 🤔")
        return

    tg_file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        with open(tmp_path, "rb") as f:
            tr = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        text = tr.strip() if isinstance(tr, str) else str(tr).strip()
        if not text:
            await update.message.reply_text("Не смог распознать речь. Попробуй ещё раз 🙏")
            return
        # Подменяем текст и пускаем в общий обработчик
        update.message.text = text
        await handle_text(update, context)
    except Exception as e:
        log.exception("Whisper error")
        await update.message.reply_text(f"Ошибка распознавания: {e}")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

# ----------------------------- MAIN ----------------------------
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    # поднимаем HTTP keep-alive (чтобы Render видел порт)
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(token).build()

    # Хэндлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Starting bot with polling…")
    # Если вдруг был второй инстанс — Telegram отдаст Conflict. Здесь просто упадём в логи,
    # поэтому следи, чтобы работал ОДИН сервис на этот токен.
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
