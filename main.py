group("m"))
        target = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"once_at": target, "text": m.group("text").strip()}

    # 3) завтра в HH:MM …
    m = re.match(rf"завтра\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        base = now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = base + timedelta(days=1)
        return {"once_at": target, "text": m.group("text").strip()}

    # 4) каждый день в HH:MM …
    m = re.match(rf"(каждый|ежедн(евно)?)\s*день\s+в\s+{RE_TIME}\s+(?P<text>.+)$", t)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        return {"daily_at": time(hh, mm, tzinfo=TIMEZONE), "text": m.group("text").strip()}

    # 5) «30 августа …» (опционально «в HH:MM …»)
    #    если время не указано — по умолчанию 09:00
    m = re.match(
        rf"(?P<day>\d{{1,2}})\s+(?P<month>[а-я]+)(?:\s+в\s+{RE_TIME})?\s+(?P<text>.+)$", t
    )
    if m and m.group("month") in MONTHS:
        day = int(m.group("day"))
        month = MONTHS[m.group("month")]
        hh = int(m.group("h")) if m.group("h") else 9
        mm = int(m.group("m")) if m.group("m") else 0
        year = now_local().year
        target = datetime(year, month, day, hh, mm, tzinfo=TIMEZONE)
        # если дата уже прошла в этом году — перенесём на следующий
        if target < now_local():
            try:
                target = target.replace(year=year + 1)
            except ValueError:
                pass
        return {"once_at": target, "text": m.group("text").strip()}

    return None

# --------- ДОСТУП ПО КЛЮЧУ ---------
async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if uid in ALLOWED_USERS:
        return True

    text = (update.message.text or "").strip()
    if re.fullmatch(r"VIP\d{3}", text, flags=re.IGNORECASE):
        key = text.upper()
        if key in ACCESS_KEYS and ACCESS_KEYS[key] is None:
            # активируем ключ
            ACCESS_KEYS[key] = uid
            ALLOWED_USERS.add(uid)
            save_json(KEYS_FILE, ACCESS_KEYS)
            save_json(ACCESS_FILE, list(ALLOWED_USERS))
            await update.message.reply_text("✅ Доступ подтверждён. Можешь писать напоминания.\n\n" + WELCOME)
            return True
        else:
            await update.message.reply_text("❌ Ключ недействителен или уже использован.")
            return False

    await update.message.reply_text(
        "🔒 Этот бот приватный. Пришли одноразовый ключ доступа вида: VIP001 … VIP100."
    )
    return False

# --------- ХЭНДЛЕРЫ ---------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await require_access(update, context):
        await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return

    cmd = parse_command(update.message.text or "")
    if not cmd:
        await update.message.reply_text(
            "❓ Не понял формат. Используй, например:\n"
            "• через 5 минут …\n"
            "• сегодня в HH:MM …\n"
            "• завтра в HH:MM …\n"
            "• каждый день в HH:MM …\n"
            "• 30 августа …"
        )
        return

    chat_id = update.effective_chat.id
    app = context.application

    if "after" in cmd:
        when = now_local() + cmd["after"]
        schedule_once(app, when, chat_id, cmd["text"])
        await update.message.reply_text(
            f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{cmd['text']}». (TZ: {TIMEZONE.zone})"
        )
        return

    if "once_at" in cmd:
        when = cmd["once_at"]
        if when < now_local():
            await update.message.reply_text("⛔ Это время уже прошло. Укажи время в будущем.")
            return
        schedule_once(app, when, chat_id, cmd["text"])
        await update.message.reply_text(
            f"✅ Ок, напомню {when.strftime('%Y-%m-%d %H:%M')} — «{cmd['text']}».(TZ: {TIMEZONE.zone})"
        )
        return

    if "daily_at" in cmd:
        schedule_daily(app, cmd["daily_at"], chat_id, cmd["text"])
        hhmm = f"{cmd['daily_at'].hour:02d}:{cmd['daily_at'].minute:02d}"
        await update.message.reply_text(
            f"✅ Ок, буду напоминать каждый день в {hhmm} — «{cmd['text']}». (TZ: {TIMEZONE.zone})"
        )
    return

# --------- ЗАПУСК (WEBHOOK) ---------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет переменной окружения BOT_TOKEN")

    public_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not public_url:
        # На самом первом деплое URL может быть пуст — сделай повторный деплой
        log.warning("RENDER_EXTERNAL_URL пуст. Перезапусти деплой после первого старта.")
        raise SystemExit(1)

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # путь вебхука = токен (удобно и уникально)
    url_path = BOT_TOKEN
    webhook_url = f"{public_url.rstrip('/')}/{url_path}"
    log.info("Ставлю вебхук: %s", webhook_url)

    # В PTB v21 указываем url=, НЕ webhook_url=
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)

    # Встроенный aiohttp-сервер PTB
    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        # чтобы PTB не пытался закрывать активный event loop при рестартах
        close_loop=False,
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
