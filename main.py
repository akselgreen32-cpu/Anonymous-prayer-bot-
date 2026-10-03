import os
import time
import random
import asyncio
import logging
from datetime import datetime, timezone

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
RECIPIENT_IDS = [i.strip() for i in os.environ["MY_USER_ID"].split(",") if i.strip()]
PORT = int(os.environ.get("PORT", 8080))

# On Render, RENDER_EXTERNAL_URL is set automatically (https://yourapp.onrender.com).
# WEBHOOK_URL is optional and only needed to override it.
WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
if not WEBHOOK_URL:
    raise RuntimeError("Set WEBHOOK_URL (Render sets RENDER_EXTERNAL_URL itself).")

# How many seconds a person must wait between prayer requests
COOLDOWN_SECONDS = 10

# In-memory dictionary tracking the last time each user sent a request.
# It lives only in the program's running memory, so it resets if the bot
# restarts. That's fine for a simple spam guard.
_last_message_time = {}


# ---------------------------------------------------------------------------
# Bible verses — World English Bible (WEB), public domain
# ---------------------------------------------------------------------------
VERSES = [
    (
        "Psalm 34:18",
        "The LORD is near to those who have a broken heart, and saves those who have a crushed spirit.",
    ),
    (
        "Philippians 4:6-7",
        "In nothing be anxious, but in everything, by prayer and petition with thanksgiving, let your requests be made known to God. And the peace of God, which surpasses all understanding, will guard your hearts.",
    ),
    (
        "Matthew 11:28",
        "Come to me, all you who labor and are heavily burdened, and I will give you rest.",
    ),
    (
        "Isaiah 41:10",
        "Don't be afraid, for I am with you. Don't be dismayed, for I am your God. I will strengthen you.",
    ),
    (
        "Jeremiah 29:11",
        "For I know the thoughts that I think toward you, says the LORD, thoughts of peace, and not of evil, to give you hope and a future.",
    ),
    ("Psalm 23:1", "The LORD is my shepherd; I shall lack nothing."),
    (
        "Romans 8:28",
        "We know that all things work together for good for those who love God.",
    ),
    ("Psalm 46:1", "God is our refuge and strength, a very present help in trouble."),
    (
        "John 14:27",
        "Peace I leave with you. My peace I give to you; not as the world gives, give I to you. Don't let your heart be troubled, neither let it be fearful.",
    ),
    ("Psalm 55:22", "Cast your burden on the LORD, and he will sustain you."),
    (
        "Isaiah 40:31",
        "But those who wait for the LORD will renew their strength. They will mount up with wings like eagles.",
    ),
    (
        "Matthew 6:34",
        "Therefore don't be anxious for tomorrow, for tomorrow will be anxious for itself.",
    ),
    (
        "2 Corinthians 12:9",
        "My grace is sufficient for you, for my power is made perfect in weakness.",
    ),
    (
        "Psalm 34:17",
        "The righteous cry, and the LORD hears, and delivers them out of all their troubles.",
    ),
    ("Psalm 27:1", "The LORD is my light and my salvation. Whom shall I fear?"),
    (
        "Proverbs 3:5-6",
        "Trust in the LORD with all your heart, and don't lean on your own understanding.",
    ),
    ("Psalm 30:5", "Weeping may stay for the night, but joy comes in the morning."),
    (
        "Matthew 7:7",
        "Ask, and it will be given you. Seek, and you will find. Knock, and it will be opened for you.",
    ),
    ("Psalm 147:3", "He heals the broken in heart, and binds up their wounds."),
    (
        "James 1:5",
        "If any of you lacks wisdom, let him ask of God, who gives to all liberally and without reproach.",
    ),
    (
        "Deuteronomy 31:6",
        "Be strong and courageous. Don't be afraid... for the LORD your God himself goes with you.",
    ),
    ("1 Peter 5:7", "Casting all your worries on him, because he cares for you."),
    (
        "Lamentations 3:22-23",
        "It is because of the LORD's loving kindnesses that we are not consumed... They are new every morning.",
    ),
    (
        "Zephaniah 3:17",
        "The LORD, your God, is among you, a mighty one who will save. He will rejoice over you with joy.",
    ),
]


def get_random_verse() -> str:
    reference, text = random.choice(VERSES)
    return f'✨ {reference}\n"{text}"'


# ---------------------------------------------------------------------------
# Static text blocks
# ---------------------------------------------------------------------------
WELCOME_TEXT = (
    "🙏 Welcome to Anonymous Prayer.\n\n"
    "Send me your prayer request as a text, photo, or voice message, and it "
    "will be shared anonymously so someone can pray for you.\n\n"
    "Commands:\n"
    "/help — see this message again\n"
    "/lordsprayer — read the Lord's Prayer (English & Amharic)"
)

HELP_TEXT = (
    WELCOME_TEXT  # identical for now; kept as a separate command for convenience
)

LORDS_PRAYER_TEXT = (
    "🙏 The Lord's Prayer\n\n"
    "English:\n"
    "Our Father, who art in heaven, hallowed be thy name.\n"
    "Thy kingdom come, thy will be done, on earth as it is in heaven.\n"
    "Give us this day our daily bread.\n"
    "And forgive us our trespasses, as we forgive those who trespass against us.\n"
    "And lead us not into temptation, but deliver us from evil.\n"
    "For thine is the kingdom, and the power, and the glory, forever. Amen.\n\n"
    "አማርኛ:\n"
    "በሰማያት የምትኖር አባታችን ሆይ፤ ስምህ ይቀደስ።\n"
    "መንግሥትህ ትምጣ፤ ፈቃድህ በሰማይ እንደሆነች እንዲሁም በምድር ትሁን።\n"
    "የዕለት እንጀራችንን ዛሬ ስጠን።\n"
    "እኛም የበደሉንን ይቅር እንደምንል፥ በደላችንን ይቅር በለን።\n"
    "ከፈተና አታግባን እንጂ፥ ከክፉ አድንህ እንጂ።\n"
    "መንግሥት ያንተ ናትና፥ ኃይልም ክብርም ለዘላለም አሜን።"
)


def thank_you_message() -> str:
    verse = get_random_verse()
    return (
        "Thank you for sharing your prayer request. Your request has been shared anonymously "
        "so someone can pray for you. 🙏\n\n"
        f"{verse}"
    )


# ---------------------------------------------------------------------------
# Cooldown check — shared by text and media handlers
# ---------------------------------------------------------------------------
def is_on_cooldown(user_id: int) -> bool:
    now = time.time()
    last_time = _last_message_time.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return True
    _last_message_time[user_id] = now
    return False


def current_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def handle_lords_prayer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LORDS_PRAYER_TEXT)


# ---------------------------------------------------------------------------
# Prayer request handlers
# ---------------------------------------------------------------------------
async def handle_prayer_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_on_cooldown(user_id):
        await update.message.reply_text(
            "🙏 Please wait a few seconds before sending another request."
        )
        return

    prayer_text = update.message.text
    timestamp = current_timestamp()

    for recipient_id in RECIPIENT_IDS:
        await context.bot.send_message(
            chat_id=recipient_id,
            text=f"🙏 New prayer request\nTime: {timestamp}\n\n{prayer_text}",
        )

    await update.message.reply_text(thank_you_message())


async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_on_cooldown(user_id):
        await update.message.reply_text(
            "🙏 Please wait a few seconds before sending another request."
        )
        return

    # Caption is any text typed alongside a photo/voice message.
    # It's None if the person didn't add one, so we fall back to "".
    caption = update.message.caption or ""
    timestamp = current_timestamp()

    if update.message.photo:
        file_id = update.message.photo[-1].file_id  # largest available size
        forward_caption = f"🙏 New prayer request (photo)\nTime: {timestamp}"
        if caption:
            forward_caption += f"\n\n{caption}"
        for recipient_id in RECIPIENT_IDS:
            await context.bot.send_photo(
                chat_id=recipient_id, photo=file_id, caption=forward_caption
            )
    elif update.message.voice:
        file_id = update.message.voice.file_id
        forward_caption = f"🙏 New prayer request (voice)\nTime: {timestamp}"
        if caption:
            forward_caption += f"\n\n{caption}"
        for recipient_id in RECIPIENT_IDS:
            await context.bot.send_voice(
                chat_id=recipient_id, voice=file_id, caption=forward_caption
            )
    else:
        await update.message.reply_text(
            "🙏 I can accept text, photos, or voice messages for prayer requests."
        )
        return

    await update.message.reply_text(thank_you_message())


# ---------------------------------------------------------------------------
# Web server (Render) — Telegram webhook + /health for UptimeRobot
# ---------------------------------------------------------------------------
async def main():
    # updater(None): we receive updates through our own web server below
    application = ApplicationBuilder().token(BOT_TOKEN).updater(None).build()

    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CommandHandler("lordsprayer", handle_lords_prayer))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prayer_request)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VOICE, handle_media_request)
    )

    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}/{BOT_TOKEN}", allowed_updates=Update.ALL_TYPES
    )

    async def telegram_webhook(request: Request) -> Response:
        data = await request.json()
        await application.update_queue.put(Update.de_json(data, application.bot))
        return Response()

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    web_app = Starlette(
        routes=[
            Route(f"/{BOT_TOKEN}", telegram_webhook, methods=["POST"]),
            Route("/health", health, methods=["GET", "HEAD"]),
            Route("/", health, methods=["GET", "HEAD"]),
        ]
    )

    server = uvicorn.Server(
        uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="info")
    )

    logging.info("Bot is starting with webhook at %s", WEBHOOK_URL)
    async with application:
        await application.start()
        await server.serve()
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
