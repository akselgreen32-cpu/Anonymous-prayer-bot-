import os
import hmac
import hashlib
import random
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    Update,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# ---------------------------------------------------------------------------
# Configuration (environment variables)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]  # Neon connection string
RECIPIENT_IDS = [int(i) for i in os.environ["MY_USER_ID"].split(",") if i.strip()]
# Admins can use /stats /block /unblock. Defaults to the recipients.
ADMIN_IDS = {
    int(i) for i in os.environ.get("ADMIN_IDS", "").split(",") if i.strip()
} or set(RECIPIENT_IDS)
PORT = int(os.environ.get("PORT", 8080))

# On Render, RENDER_EXTERNAL_URL is set automatically.
WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
if not WEBHOOK_URL:
    raise RuntimeError("Set WEBHOOK_URL (Render sets RENDER_EXTERNAL_URL itself).")

COOLDOWN_SECONDS = 15
EAT = timezone(timedelta(hours=3))  # Ethiopia (East Africa Time), no daylight saving
# 06:30 Ethiopian-standard time == 03:30 UTC
REMINDER_TIME_UTC = dtime(hour=3, minute=30, tzinfo=timezone.utc)


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

# Amharic verse text, keyed by the reference above, e.g.
#   "Psalm 23:1": "እግዚአብሔር እረኛዬ ነው፤ የሚጎድልብኝም የለም።",
# Fill these in from the Amharic Bible you trust. Amharic users get the English
# verse for any reference that is not listed here.
VERSES_AM = {}


def _verse_block(lang: str, ref: str, text: str) -> str:
    if lang == "am" and ref in VERSES_AM:
        text = VERSES_AM[ref]
    return f'✨ {ref}\n"{text}"'


def random_verse(lang: str) -> str:
    ref, text = random.choice(VERSES)
    return _verse_block(lang, ref, text)


def daily_verse(lang: str) -> str:
    """Same verse for everyone on a given day, rotating through the list."""
    index = datetime.now(EAT).date().toordinal() % len(VERSES)
    ref, text = VERSES[index]
    return _verse_block(lang, ref, text)


# ---------------------------------------------------------------------------
# Texts (English / Amharic)
# ---------------------------------------------------------------------------
LANG_PROMPT = "🌍 Choose your language\nቋንቋዎን ይምረጡ"

TEXTS = {
    "en": {
        "welcome": (
            "🙏 Welcome to Anonymous Prayer.\n\n"
            "Send me your prayer request as text, a photo, a voice message, a video "
            "or a file. It will be shared anonymously so someone can pray for you.\n\n"
            "Commands:\n"
            "/language — change language\n"
            "/subscribe — daily prayer reminder at 6:30 AM\n"
            "/unsubscribe — stop the reminder\n"
            "/lordsprayer — the Lord's Prayer\n"
            "/help — show this message"
        ),
        "ask_category": "📂 What is your request about? Choose a category:",
        "sent": (
            "Thank you for sharing your prayer request. It has been shared "
            "anonymously so someone can pray for you. 🙏\n\n{verse}"
        ),
        "cooldown": "🙏 Please wait {n} seconds before sending another request.",
        "blocked": "⚠️ Your messages can't be delivered right now.",
        "unsupported": (
            "🙏 I can accept text, photos, voice messages, videos, audio and files "
            "as prayer requests."
        ),
        "expired": "Sorry, I couldn't find your message. Please send your request again.",
        "send_failed": (
            "Sorry, something went wrong while sending your request. "
            "Please try again in a moment."
        ),
        "lang_set": "✅ Language set to English.",
        "subscribed": (
            "✅ You will get a prayer reminder with a verse every morning at 6:30 AM "
            "(Ethiopian time). Use /unsubscribe to stop."
        ),
        "unsubscribed": "✅ The daily reminder is off. Use /subscribe to turn it on again.",
        "reminder": (
            "🌅 Good morning! Take a moment to pray today.\n\n{verse}\n\n"
            "(/unsubscribe to stop these reminders)"
        ),
    },
    "am": {
        "welcome": (
            "🙏 እንኳን ወደ ስም አልባ ጸሎት በደህና መጡ።\n\n"
            "የጸሎት ጥያቄዎን በጽሑፍ፣ በፎቶ፣ በድምፅ፣ በቪዲዮ ወይም በፋይል ይላኩልኝ። "
            "ማንነትዎ ሳይታወቅ ይተላለፋል፤ ሌላ ሰውም ስለእርስዎ ይጸልያል።\n\n"
            "ትዕዛዞች፦\n"
            "/language — ቋንቋ ለመቀየር\n"
            "/subscribe — የጠዋት የጸሎት ማስታወሻ (ጠዋት 12:30)\n"
            "/unsubscribe — ማስታወሻውን ለማቆም\n"
            "/lordsprayer — የጌታ ጸሎት\n"
            "/help — ይህን መልእክት ለማየት"
        ),
        "ask_category": "📂 ጥያቄዎ ስለ ምንድን ነው? ምድብ ይምረጡ፦",
        "sent": (
            "የጸሎት ጥያቄዎን ስላካፈሉን እናመሰግናለን። ማንነትዎ ሳይታወቅ ተላልፏል፤ "
            "አንድ ሰው ስለእርስዎ ይጸልያል። 🙏\n\n{verse}"
        ),
        "cooldown": "🙏 እባክዎ ሌላ ጥያቄ ከመላክዎ በፊት {n} ሰከንድ ይጠብቁ።",
        "blocked": "⚠️ ለጊዜው መልእክትዎ ሊደርስ አይችልም።",
        "unsupported": (
            "🙏 የጸሎት ጥያቄ እንደ ጽሑፍ፣ ፎቶ፣ ድምፅ፣ ቪዲዮ፣ ኦዲዮ እና ፋይል መላክ ይችላሉ።"
        ),
        "expired": "ይቅርታ፣ መልእክትዎን ማግኘት አልቻልኩም። እባክዎ ጥያቄዎን እንደገና ይላኩ።",
        "send_failed": (
            "ይቅርታ፣ ጥያቄዎን በመላክ ላይ ችግር ተፈጠረ። እባክዎ ጥቂት ቆይተው እንደገና ይሞክሩ።"
        ),
        "lang_set": "✅ ቋንቋ ወደ አማርኛ ተቀይሯል።",
        "subscribed": (
            "✅ በየቀኑ ጠዋት 12:30 (የኢትዮጵያ ሰዓት) የጸሎት ማስታወሻ ከጥቅስ ጋር ይደርስዎታል። "
            "ለማቆም /unsubscribe ይጠቀሙ።"
        ),
        "unsubscribed": "✅ የዕለት ማስታወሻው ቆሟል። እንደገና ለመጀመር /subscribe ይጠቀሙ።",
        "reminder": (
            "🌅 እንደምን አደሩ! ዛሬ ለጸሎት ጥቂት ጊዜ ይውሰዱ።\n\n{verse}\n\n"
            "(ማስታወሻውን ለማቆም /unsubscribe)"
        ),
    },
}

ADMIN_HELP = (
    "\n\n🔧 Admin:\n"
    "/stats — usage statistics\n"
    "/block CODE — block a sender by the #CODE shown on a request\n"
    "/unblock CODE — unblock a sender"
)

CATEGORIES = ["health", "family", "school", "work", "spiritual", "other"]
CATEGORY_LABELS = {
    "en": {
        "health": "🩺 Health",
        "family": "👨‍👩‍👧 Family",
        "school": "📚 School / Exams",
        "work": "💼 Work / Money",
        "spiritual": "✝️ Spiritual",
        "other": "🙏 Other",
    },
    "am": {
        "health": "🩺 ጤና",
        "family": "👨‍👩‍👧 ቤተሰብ",
        "school": "📚 ትምህርት / ፈተና",
        "work": "💼 ሥራ / ገንዘብ",
        "spiritual": "✝️ መንፈሳዊ",
        "other": "🙏 ሌላ",
    },
}

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


def t(lang: str, key: str, **kwargs) -> str:
    return TEXTS[lang if lang in TEXTS else "en"][key].format(**kwargs)


# ---------------------------------------------------------------------------
# Ethiopian calendar and clock (used for timestamps when the sender chose Amharic)
# ---------------------------------------------------------------------------
ET_MONTHS = [
    "መስከረም", "ጥቅምት", "ኅዳር", "ታኅሣሥ", "ጥር", "የካቲት", "መጋቢት",
    "ሚያዝያ", "ግንቦት", "ሰኔ", "ሐምሌ", "ነሐሴ", "ጳጉሜን",
]  # fmt: skip


def gregorian_to_ethiopian(d: date) -> tuple[int, int, int]:
    """Return (year, month, day) in the Ethiopian calendar."""
    jdn = d.toordinal() + 1721425  # Julian Day Number
    era = 1723856  # JDN of Ethiopian 1/1/1
    r = (jdn - era) % 1461
    n = (r % 365) + 365 * (r // 1460)
    year = 4 * ((jdn - era) // 1461) + r // 365 - r // 1460
    return year, n // 30 + 1, n % 30 + 1


def ethiopian_clock(dt: datetime) -> str:
    """Ethiopian 12-hour clock: the day starts at 6:00 AM (6:00 AM = 12:00)."""
    hour = dt.hour
    eth_hour = (hour - 6) % 12 or 12
    if 6 <= hour < 12:
        label = "ጠዋት"
    elif 12 <= hour < 18:
        label = "ቀን"
    elif 18 <= hour < 24:
        label = "ማታ"
    else:
        label = "ሌሊት"
    return f"{label} {eth_hour}:{dt.minute:02d}"


def format_time(now: datetime, lang: str) -> str:
    if lang == "am":
        year, month, day = gregorian_to_ethiopian(now.date())
        return f"{ET_MONTHS[month - 1]} {day}, {year} ዓ.ም · {ethiopian_clock(now)}"
    return now.strftime("%Y-%m-%d %H:%M") + " (EAT)"


# ---------------------------------------------------------------------------
# Database (Neon Postgres). Render's free disk is wiped on every restart,
# so anything that must be remembered lives here.
# ---------------------------------------------------------------------------
pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         BIGINT PRIMARY KEY,
    anon_code       TEXT NOT NULL,
    lang            TEXT,
    subscribed      BOOLEAN NOT NULL DEFAULT FALSE,
    blocked         BOOLEAN NOT NULL DEFAULT FALSE,
    last_request_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_anon_code_idx ON users (anon_code);
CREATE TABLE IF NOT EXISTS requests (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    category   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    lang       TEXT NOT NULL
);
"""


def clean_dsn(url: str) -> str:
    """Neon strings can include channel_binding=..., which asyncpg rejects."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def db(method: str, query: str, *args):
    """Run a query, retrying once if Neon dropped an idle connection."""
    for attempt in (1, 2):
        try:
            return await getattr(pool, method)(query, *args)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)


def anon_code(user_id: int) -> str:
    """Stable anonymous ID shown to recipients so a spammer can be blocked
    without anyone seeing who they are."""
    digest = hmac.new(BOT_TOKEN.encode(), str(user_id).encode(), hashlib.sha256)
    return digest.hexdigest()[:8].upper()


async def get_user(user_id: int):
    return await db(
        "fetchrow",
        """
        INSERT INTO users (user_id, anon_code) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
        RETURNING lang, blocked, subscribed, anon_code
        """,
        user_id,
        anon_code(user_id),
    )


async def start_request_if_allowed(user_id: int) -> bool:
    """Persistent cooldown: True (and the clock restarts) only if the
    cooldown has passed. Survives bot restarts."""
    row = await db(
        "fetchrow",
        """
        UPDATE users SET last_request_at = now()
        WHERE user_id = $1
          AND (last_request_at IS NULL
               OR last_request_at <= now() - make_interval(secs => $2))
        RETURNING user_id
        """,
        user_id,
        float(COOLDOWN_SECONDS),
    )
    return row is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def detect_kind(m: Message) -> str:
    if m.photo:
        return "photo"
    if m.voice:
        return "voice"
    if m.video:
        return "video"
    if m.video_note:
        return "video_note"
    if m.audio:
        return "audio"
    if m.animation:
        return "animation"
    if m.document:
        return "document"
    return "text"


def category_keyboard(lang: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(CATEGORY_LABELS[lang][c], callback_data=f"cat:{c}")
        for c in CATEGORIES
    ]
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("English", callback_data="lang:en"),
                InlineKeyboardButton("አማርኛ", callback_data="lang:am"),
            ]
        ]
    )


def welcome_text(lang: str, user_id: int) -> str:
    text = t(lang, "welcome")
    if user_id in ADMIN_IDS:
        text += ADMIN_HELP
    return text


async def deliver(context, from_chat_id: int, message_id: int, header: str) -> int:
    """Send the header and a copy of the message to every recipient.
    copy_message does not reveal the sender. One failing recipient does not
    stop the others. Returns how many recipients got it."""
    delivered = 0
    for recipient_id in RECIPIENT_IDS:
        try:
            head = await context.bot.send_message(chat_id=recipient_id, text=header)
            await context.bot.copy_message(
                chat_id=recipient_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_parameters=ReplyParameters(message_id=head.message_id),
            )
            delivered += 1
        except TelegramError as e:
            logging.warning("Could not deliver to %s: %s", recipient_id, e)
    return delivered


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if user["lang"] is None:
        await update.message.reply_text(LANG_PROMPT, reply_markup=language_keyboard())
    else:
        await update.message.reply_text(
            welcome_text(user["lang"], update.effective_user.id)
        )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    await update.message.reply_text(
        welcome_text(user["lang"] or "en", update.effective_user.id)
    )


async def handle_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_user(update.effective_user.id)
    await update.message.reply_text(LANG_PROMPT, reply_markup=language_keyboard())


async def handle_lang_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = query.data.split(":", 1)[1]
    await query.answer()
    if lang not in TEXTS:
        return
    await get_user(query.from_user.id)
    await db(
        "execute", "UPDATE users SET lang = $2 WHERE user_id = $1", query.from_user.id, lang
    )
    await query.edit_message_text(t(lang, "lang_set"))
    await context.bot.send_message(
        chat_id=query.message.chat_id, text=welcome_text(lang, query.from_user.id)
    )


async def handle_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    await db(
        "execute",
        "UPDATE users SET subscribed = TRUE WHERE user_id = $1",
        update.effective_user.id,
    )
    await update.message.reply_text(t(user["lang"] or "en", "subscribed"))


async def handle_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    await db(
        "execute",
        "UPDATE users SET subscribed = FALSE WHERE user_id = $1",
        update.effective_user.id,
    )
    await update.message.reply_text(t(user["lang"] or "en", "unsubscribed"))


async def handle_lords_prayer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LORDS_PRAYER_TEXT)


# ---------------------------------------------------------------------------
# Prayer requests: message -> category buttons -> delivery
# ---------------------------------------------------------------------------
async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = await get_user(update.effective_user.id)
    lang = user["lang"] or "en"

    if user["blocked"]:
        await message.reply_text(t(lang, "blocked"))
        return

    if not await start_request_if_allowed(update.effective_user.id):
        await message.reply_text(t(lang, "cooldown", n=COOLDOWN_SECONDS))
        return

    await message.reply_text(
        t(lang, "ask_category"), reply_markup=category_keyboard(lang), do_quote=True
    )


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    category = query.data.split(":", 1)[1]
    await query.answer()
    if category not in CATEGORIES:
        return

    user = await get_user(query.from_user.id)
    lang = user["lang"] or "en"

    if user["blocked"]:
        await query.edit_message_text(t(lang, "blocked"))
        return

    # Remove the buttons first. A second tap then fails here, so a request
    # can never be sent twice.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        return

    original = getattr(query.message, "reply_to_message", None)
    if original is None:
        await query.edit_message_text(t(lang, "expired"))
        return

    now = datetime.now(EAT)
    header = (
        f"🙏 New prayer request · #{user['anon_code']}\n"
        f"📂 {CATEGORY_LABELS['en'][category]}\n"
        f"🕒 {format_time(now, lang)}"
    )
    delivered = await deliver(context, original.chat_id, original.message_id, header)

    if delivered:
        await db(
            "execute",
            "INSERT INTO requests (category, kind, lang) VALUES ($1, $2, $3)",
            category,
            detect_kind(original),
            lang,
        )
        await query.edit_message_text(t(lang, "sent", verse=random_verse(lang)))
    else:
        # Nothing was delivered, so don't make them wait out the cooldown.
        await db(
            "execute",
            "UPDATE users SET last_request_at = NULL WHERE user_id = $1",
            query.from_user.id,
        )
        await query.edit_message_text(t(lang, "send_failed"))


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    await update.message.reply_text(t(user["lang"] or "en", "unsupported"))


# ---------------------------------------------------------------------------
# Admin tools
# ---------------------------------------------------------------------------
def _is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    start_of_today = datetime.now(EAT).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = await db(
        "fetchrow",
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE created_at >= $1) AS today,
               count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS week
        FROM requests
        """,
        start_of_today,
    )
    by_category = await db(
        "fetch",
        "SELECT category, count(*) AS n FROM requests GROUP BY category ORDER BY n DESC",
    )
    by_kind = await db(
        "fetch", "SELECT kind, count(*) AS n FROM requests GROUP BY kind ORDER BY n DESC"
    )
    users = await db(
        "fetchrow",
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE subscribed) AS subscribed,
               count(*) FILTER (WHERE lang = 'am') AS amharic,
               count(*) FILTER (WHERE lang = 'en') AS english,
               count(*) FILTER (WHERE blocked) AS blocked
        FROM users
        """,
    )

    lines = [
        "📊 Stats",
        "",
        f"Requests: today {counts['today']} · last 7 days {counts['week']} · total {counts['total']}",
        "",
        "By category: "
        + (
            " · ".join(f"{CATEGORY_LABELS['en'][r['category']]} {r['n']}" for r in by_category)
            or "none yet"
        ),
        "By type: " + (" · ".join(f"{r['kind']} {r['n']}" for r in by_kind) or "none yet"),
        "",
        f"Users: {users['total']} · subscribers {users['subscribed']} · "
        f"Amharic {users['amharic']} · English {users['english']} · blocked {users['blocked']}",
    ]
    await update.message.reply_text("\n".join(lines))


async def _set_blocked(update: Update, context, blocked: bool):
    if not _is_admin(update):
        return
    verb = "block" if blocked else "unblock"
    if not context.args:
        await update.message.reply_text(f"Usage: /{verb} CODE  (the #CODE on a request)")
        return
    code = context.args[0].lstrip("#").upper()
    result = await db(
        "execute", "UPDATE users SET blocked = $2 WHERE anon_code = $1", code, blocked
    )
    changed = int(result.split()[-1])
    if changed:
        await update.message.reply_text(
            f"{'🚫 Blocked' if blocked else '✅ Unblocked'} #{code}"
        )
    else:
        await update.message.reply_text(f"No sender found with code #{code}.")


async def handle_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_blocked(update, context, True)


async def handle_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_blocked(update, context, False)


# ---------------------------------------------------------------------------
# Daily reminder (06:30 Ethiopian-standard time)
# ---------------------------------------------------------------------------
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    rows = await db(
        "fetch", "SELECT user_id, lang FROM users WHERE subscribed AND NOT blocked"
    )
    logging.info("Sending daily reminder to %d subscribers", len(rows))
    for row in rows:
        lang = row["lang"] or "en"
        try:
            await context.bot.send_message(
                chat_id=row["user_id"], text=t(lang, "reminder", verse=daily_verse(lang))
            )
        except Forbidden:
            # They blocked the bot, so stop trying.
            await db(
                "execute",
                "UPDATE users SET subscribed = FALSE WHERE user_id = $1",
                row["user_id"],
            )
        except TelegramError as e:
            logging.warning("Reminder failed for %s: %s", row["user_id"], e)
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limit


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error", exc_info=context.error)


# ---------------------------------------------------------------------------
# Application + web server (Render): Telegram webhook and /health
# ---------------------------------------------------------------------------
REQUEST_FILTER = filters.ChatType.PRIVATE & (
    (filters.TEXT & ~filters.COMMAND)
    | filters.PHOTO
    | filters.VOICE
    | filters.VIDEO
    | filters.VIDEO_NOTE
    | filters.AUDIO
    | filters.Document.ALL
    | filters.ANIMATION
)


def build_application():
    # updater(None): updates arrive through our own web server below
    application = ApplicationBuilder().token(BOT_TOKEN).updater(None).build()

    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", handle_start, filters=private))
    application.add_handler(CommandHandler("help", handle_help, filters=private))
    application.add_handler(CommandHandler("language", handle_language, filters=private))
    application.add_handler(CommandHandler("subscribe", handle_subscribe, filters=private))
    application.add_handler(
        CommandHandler("unsubscribe", handle_unsubscribe, filters=private)
    )
    application.add_handler(
        CommandHandler("lordsprayer", handle_lords_prayer, filters=private)
    )
    application.add_handler(CommandHandler("stats", handle_stats, filters=private))
    application.add_handler(CommandHandler("block", handle_block, filters=private))
    application.add_handler(CommandHandler("unblock", handle_unblock, filters=private))

    application.add_handler(CallbackQueryHandler(handle_lang_choice, pattern=r"^lang:"))
    application.add_handler(CallbackQueryHandler(handle_category, pattern=r"^cat:"))

    application.add_handler(MessageHandler(REQUEST_FILTER, handle_request))
    # Anything else in private chat (stickers, locations, ...)
    application.add_handler(
        MessageHandler(private & ~filters.COMMAND, handle_unsupported)
    )
    application.add_error_handler(on_error)

    if application.job_queue is None:
        raise RuntimeError("Install python-telegram-bot[job-queue] for the reminders.")
    application.job_queue.run_daily(
        send_daily_reminder, time=REMINDER_TIME_UTC, name="daily_reminder"
    )
    return application


async def main():
    global pool
    pool = await asyncpg.create_pool(
        clean_dsn(DATABASE_URL),
        min_size=1,
        max_size=5,
        statement_cache_size=0,  # works with Neon's pooled connections
        max_inactive_connection_lifetime=60,
    )
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)

    application = build_application()

    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}/{BOT_TOKEN}", allowed_updates=Update.ALL_TYPES
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start"),
            BotCommand("language", "Language / ቋንቋ"),
            BotCommand("subscribe", "Daily prayer reminder"),
            BotCommand("unsubscribe", "Stop the reminder"),
            BotCommand("lordsprayer", "The Lord's Prayer"),
            BotCommand("help", "Help"),
        ]
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
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
