import os
import re
import json
import hmac
import base64
import hashlib
import random
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
import uvicorn
from cryptography.fernet import Fernet, InvalidToken
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
from telegram.error import BadRequest, Forbidden, TelegramError
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
# Any long random secret. It encrypts journal entries. Keep it safe: if it is
# lost or changed, existing journal entries can no longer be read.
JOURNAL_SECRET = os.environ["JOURNAL_SECRET"]
# Base prayer team (also admins). Comma-separated Telegram IDs.
RECIPIENT_IDS = [int(i) for i in os.environ["MY_USER_ID"].split(",") if i.strip()]
# The ultimate admin. Defaults to the first ID in MY_USER_ID.
OWNER_ID = int(os.environ.get("OWNER_ID") or RECIPIENT_IDS[0])
PORT = int(os.environ.get("PORT", 8080))
# Shown under Amharic verses (see verses_am.json). Leave empty for none.
AMHARIC_BIBLE_CREDIT = os.environ.get("AMHARIC_BIBLE_CREDIT", "")

WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
if not WEBHOOK_URL:
    raise RuntimeError("Set WEBHOOK_URL (Render sets RENDER_EXTERNAL_URL itself).")

COOLDOWN_SECONDS = 15
MAX_TEXT = 3500
EAT = timezone(timedelta(hours=3))  # Ethiopia (East Africa Time), no daylight saving
DAILY_TIME_UTC = dtime(hour=3, minute=30, tzinfo=timezone.utc)  # 06:30 EAT
WEEKLY_TIME_UTC = dtime(hour=15, minute=0, tzinfo=timezone.utc)  # Sunday 18:00 EAT

fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(JOURNAL_SECRET.encode()).digest()))


# ---------------------------------------------------------------------------
# Bible verses live in verses.json (next to this file), not in the code.
# Each entry looks like this:
#   {"ref": "Psalm 23:1", "ref_am": "መዝሙር 23:1",
#    "en": "English text", "am": "Amharic text", "feelings": ["anxious", "afraid"]}
# To add a verse, add one more entry to verses.json. Nothing here needs to change.
# English text: World English Bible (WEB), public domain.
# Amharic credit (shown under Amharic verses): set AMHARIC_BIBLE_CREDIT.
# ---------------------------------------------------------------------------
# Feelings offered under "Scripture for me". A verse is shown for a feeling when
# that feeling is listed in the verse's "feelings" in verses.json.
FEELINGS = ["anxious", "tired", "afraid", "sad", "lonely", "guilty", "lost", "thankful"]


def _load_verses() -> list:
    path = Path(__file__).with_name("verses.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Could not read verses.json (it must sit next to this file): {e}") from e
    verses = []
    for item in data:
        ref = str(item.get("ref", "")).strip()
        en = str(item.get("en", "")).strip()
        if not ref or not en:
            logging.warning("Skipping a verse with no ref or no English text: %r", item)
            continue
        verses.append(
            {
                "ref": ref,
                "ref_am": str(item.get("ref_am", "")).strip(),
                "en": en,
                "am": str(item.get("am", "")).strip(),
                "feelings": [f for f in item.get("feelings", []) if f in FEELINGS],
            }
        )
    if not verses:
        raise RuntimeError("verses.json has no usable verses.")
    return verses


VERSES = _load_verses()
# Verses for each feeling (if none are tagged for a feeling, any verse is used).
FEELING_REFS = {f: [v for v in VERSES if f in v["feelings"]] or VERSES for f in FEELINGS}


def verse_block(lang: str, verse: dict) -> str:
    """Amharic users get the Amharic text when the verse has it, otherwise English."""
    if lang == "am" and verse["am"]:
        block = f'✨ {verse["ref_am"] or verse["ref"]}\n"{verse["am"]}"'
        if AMHARIC_BIBLE_CREDIT:
            block += f"\n— {AMHARIC_BIBLE_CREDIT}"
        return block
    return f'✨ {verse["ref"]}\n"{verse["en"]}"'


def random_verse(lang: str) -> str:
    return verse_block(lang, random.choice(VERSES))


def daily_verse(lang: str) -> str:
    """Same verse for everyone on a given day, rotating through the list."""
    index = datetime.now(EAT).date().toordinal() % len(VERSES)
    return verse_block(lang, VERSES[index])


# ---------------------------------------------------------------------------
# Texts (English / Amharic)
# ---------------------------------------------------------------------------
LANG_PROMPT = "🌍 Choose your language\nቋንቋዎን ይምረጡ"

TEXTS = {
    "en": {
        "welcome": (
            "🙏 Welcome to Anonymous Prayer bot.\n\n"
            "Send a prayer request and it will be shared anonymously so someone can "
            "pray for you. You can also find Scripture for how you feel, keep a "
            "private prayer journal, and set reminders.\n\nChoose an option below:"
        ),
        "home": "🙏 Anonymous Prayer bot\n\nChoose an option:",
        "request_prompt": "✍️ Send me your prayer request now: text, a photo, a voice message, a video or a file.",
        "ask_category": "📂 What is your request about? Choose a category:",
        "preview": (
            "📝 Preview\n\nCategory: {category}\n\nYour message above will be shared "
            "anonymously with the prayer team. Send it?"
        ),
        "cancelled": "Cancelled. Nothing was sent.",
        "sent": (
            "Thank you for sharing your prayer request. It has been shared "
            "anonymously so someone can pray for you. 🙏\n\n{verse}"
        ),
        "cooldown": "🙏 Please wait {n} seconds before sending another request.",
        "blocked": "⚠️ Your messages can't be delivered right now.",
        "unsupported": "🙏 I can accept text, photos, voice messages, videos, audio and files as prayer requests.",
        "expired": "Sorry, I couldn't find your message. Please send your request again.",
        "send_failed": "Sorry, something went wrong while sending your request. Please try again in a moment.",
        "need_text": "Please send text.",
        "text_too_long": "That's too long. Please shorten it (under 3500 characters).",
        "scripture_prompt": "📖 How are you feeling right now?",
        "journal_home": (
            "📓 My private journal\n\nOnly you can open this journal. Entries are stored "
            "encrypted, and the bot's admin tools have no way to show them. You can "
            "delete one entry or everything at any time.\n\nEntries: {n}"
        ),
        "journal_new_prompt": "✍️ Write your prayer or thought now. It will be saved privately.",
        "journal_saved": "✅ Saved to your private journal.\n\nWould you like a reminder to revisit it?",
        "remind_ask": "🔔 When should I remind you?",
        "remind_set": "🔔 I'll remind you on {date}.",
        "remind_none": "No reminder set.",
        "journal_empty": "Your journal is empty.",
        "journal_list": "📖 Your entries (newest first):",
        "confirm_delete": "Delete this entry permanently?",
        "confirm_delete_all": "Delete ALL {n} entries permanently? This can't be undone.",
        "deleted": "🗑 Deleted.",
        "all_deleted": "🗑 Your journal is now empty.",
        "saved_toast": "✅ Saved to your private journal.",
        "cant_save": "Only text requests can be saved.",
        "entry_missing": "That entry wasn't found.",
        "answered": "✅ Answered",
        "reminders_menu": (
            "🔔 Reminders\n\n🌅 Daily verse (6:30 AM): {daily}\n"
            "📅 Weekly reflection (Sunday evening): {weekly}\n\nTap to switch on or off."
        ),
        "on": "ON ✅",
        "off": "OFF",
        "reminder": (
            "🌅 Good morning! Take a moment to pray today.\n\n{verse}"
        ),
        "reminder_due": "🔔 Time to revisit your prayer:\n\n{text}",
        "weekly": (
            "📅 Your week in prayer\n\n📓 Entries written: {new}\n"
            "✅ Prayers marked answered: {answered}\n🙏 Still open: {open}\n\n"
            "Reflect:\n• What did you see God doing in your life this week?\n"
            "• What are you carrying into next week?\n\n{verse}"
        ),
        # buttons
        "b_send": "🙏 Send a prayer request",
        "b_scripture": "📖 Scripture for me",
        "b_journal": "📓 My journal",
        "b_reminders": "🔔 Reminders",
        "b_language": "🌍 Language",
        "b_lords": "✝️ Lord's Prayer",
        "b_admin": "🔧 Admin",
        "b_menu": "🏠 Menu",
        "b_confirm_send": "✅ Send anonymously",
        "b_cancel_send": "❌ Don't send",
        "b_save_journal": "📓 Save to my journal",
        "b_new_entry": "➕ New entry",
        "b_entries": "📖 My entries",
        "b_delete_all": "🗑 Delete everything",
        "b_cancel": "❌ Cancel",
        "b_another": "🔄 Another verse",
        "b_feelings": "📖 Other feelings",
        "b_remind": "🔔 Remind me",
        "b_mark_answered": "✅ Mark answered",
        "b_reopen": "↩️ Reopen",
        "b_delete": "🗑 Delete",
        "b_back": "⬅️ Back",
        "b_yes_delete": "🗑 Yes, delete",
        "b_yes_delete_all": "🗑 Yes, delete everything",
        "r_d1": "Tomorrow",
        "r_d7": "In a week",
        "r_d30": "In a month",
        "r_none": "No reminder",
        "b_daily": "🌅 Daily verse",
        "b_weekly": "📅 Weekly reflection",
        "b_write_reflection": "✍️ Write a reflection",
        "b_open_journal": "📓 Open journal",
        "b_open_entry": "📄 Open entry",
    },
    "am": {
        "welcome": (
            "🙏 እንኳን ወደ Anonymous Prayer bot በደህና መጡ።\n\n"
            "የጸሎት ጥያቄ ይላኩ፤ ማንነትዎ ሳይታወቅ ይተላለፋል፤ ሌላ ሰውም ስለእርስዎ ይጸልያል። "
            "እንዲሁም ለሚሰማዎት ስሜት የሚሆን ጥቅስ ማግኘት፣ የግል የጸሎት ማስታወሻ መያዝ እና "
            "ማስታወሻዎችን ማዘጋጀት ይችላሉ።\n\nከታች ይምረጡ፦"
        ),
        "home": "🙏 Anonymous Prayer bot\n\nአንድ አማራጭ ይምረጡ፦",
        "request_prompt": "✍️ የጸሎት ጥያቄዎን አሁን ይላኩልኝ፦ ጽሑፍ፣ ፎቶ፣ ድምፅ፣ ቪዲዮ ወይም ፋይል።",
        "ask_category": "📂 ጥያቄዎ ስለ ምንድን ነው? ምድብ ይምረጡ፦",
        "preview": (
            "📝 ቅድመ ዕይታ\n\nምድብ፦ {category}\n\nከላይ ያለው መልእክትዎ ስምዎ ሳይታወቅ ለጸሎት ቡድኑ "
            "ይላካል። ይላክ?"
        ),
        "cancelled": "ተሰርዟል። ምንም አልተላከም።",
        "sent": (
            "የጸሎት ጥያቄዎን ስላካፈሉን እናመሰግናለን። ማንነትዎ ሳይታወቅ ተላልፏል፤ "
            "አንድ ሰው ስለእርስዎ ይጸልያል። 🙏\n\n{verse}"
        ),
        "cooldown": "🙏 እባክዎ ሌላ ጥያቄ ከመላክዎ በፊት {n} ሰከንድ ይጠብቁ።",
        "blocked": "⚠️ ለጊዜው መልእክትዎ ሊደርስ አይችልም።",
        "unsupported": "🙏 የጸሎት ጥያቄ እንደ ጽሑፍ፣ ፎቶ፣ ድምፅ፣ ቪዲዮ፣ ኦዲዮ እና ፋይል መላክ ይችላሉ።",
        "expired": "ይቅርታ፣ መልእክትዎን ማግኘት አልቻልኩም። እባክዎ ጥያቄዎን እንደገና ይላኩ።",
        "send_failed": "ይቅርታ፣ ጥያቄዎን በመላክ ላይ ችግር ተፈጠረ። እባክዎ ጥቂት ቆይተው እንደገና ይሞክሩ።",
        "need_text": "እባክዎ ጽሑፍ ይላኩ።",
        "text_too_long": "ጽሑፉ በጣም ረጅም ነው። እባክዎ ያሳጥሩት (ከ3500 ፊደላት በታች)።",
        "scripture_prompt": "📖 አሁን ምን ተሰምቶዎታል?",
        "journal_home": (
            "📓 የእኔ የግል ማስታወሻ\n\nይህን ማስታወሻ መክፈት የሚችሉት እርስዎ ብቻ ነዎት። ጽሑፎቹ "
            "ተመስጥረው ይቀመጣሉ፤ ከአስተዳዳሪ መሣሪያዎችም ሊታዩ አይችሉም። አንድ ጽሑፍ ወይም "
            "ሁሉንም በማንኛውም ጊዜ ማጥፋት ይችላሉ።\n\nጽሑፎች፦ {n}"
        ),
        "journal_new_prompt": "✍️ ጸሎትዎን ወይም ሐሳብዎን አሁን ይጻፉ። በግል ይቀመጣል።",
        "journal_saved": "✅ በግል ማስታወሻዎ ተቀምጧል።\n\nለመመለስ ማስታወሻ ይፈልጋሉ?",
        "remind_ask": "🔔 መቼ ላስታውስዎ?",
        "remind_set": "🔔 {date} አስታውስዎታለሁ።",
        "remind_none": "ማስታወሻ አልተዘጋጀም።",
        "journal_empty": "ማስታወሻዎ ባዶ ነው።",
        "journal_list": "📖 ጽሑፎችዎ (አዲሱ መጀመሪያ)፦",
        "confirm_delete": "ይህ ጽሑፍ ለዘላለም ይጥፋ?",
        "confirm_delete_all": "ሁሉም {n} ጽሑፎች ለዘላለም ይጥፉ? ይህ መመለስ አይችልም።",
        "deleted": "🗑 ተሰርዟል።",
        "all_deleted": "🗑 ማስታወሻዎ አሁን ባዶ ነው።",
        "saved_toast": "✅ በግል ማስታወሻዎ ተቀምጧል።",
        "cant_save": "ጽሑፍ ያላቸው ጥያቄዎች ብቻ ሊቀመጡ ይችላሉ።",
        "entry_missing": "ይህ ጽሑፍ አልተገኘም።",
        "answered": "✅ ተመልሷል",
        "reminders_menu": (
            "🔔 ማስታወሻዎች\n\n🌅 የዕለት ጥቅስ (ጠዋት 12:30)፦ {daily}\n"
            "📅 የሳምንት ነጸብራቅ (እሑድ ማታ)፦ {weekly}\n\nለማብራት ወይም ለማጥፋት ይንኩ።"
        ),
        "on": "በርቷል ✅",
        "off": "ጠፍቷል",
        "reminder": (
            "🌅 እንደምን አደሩ! ዛሬ ለጸሎት ጥቂት ጊዜ ይውሰዱ።\n\n{verse}"
        ),
        "reminder_due": "🔔 ጸሎትዎን የሚያስቡበት ጊዜ ደርሷል፦\n\n{text}",
        "weekly": (
            "📅 የሳምንቱ የጸሎት ዕይታ\n\n📓 የተጻፉ ጽሑፎች፦ {new}\n"
            "✅ የተመለሱ ጸሎቶች፦ {answered}\n🙏 ገና ክፍት የሆኑ፦ {open}\n\n"
            "ያስቡ፦\n• በዚህ ሳምንት እግዚአብሔር በሕይወትዎ ሲሠራ ያዩት ምንድን ነው?\n"
            "• ወደ ቀጣዩ ሳምንት ምን ይዘው ይሄዳሉ?\n\n{verse}"
        ),
        "b_send": "🙏 የጸሎት ጥያቄ ላክ",
        "b_scripture": "📖 ጥቅስ ለእኔ",
        "b_journal": "📓 የእኔ ማስታወሻ",
        "b_reminders": "🔔 ማስታወሻዎች",
        "b_language": "🌍 ቋንቋ",
        "b_lords": "✝️ የጌታ ጸሎት",
        "b_admin": "🔧 አስተዳዳሪ",
        "b_menu": "🏠 ዋና ማውጫ",
        "b_confirm_send": "✅ ስም አልባ ላክ",
        "b_cancel_send": "❌ አትላክ",
        "b_save_journal": "📓 በማስታወሻዬ አስቀምጥ",
        "b_new_entry": "➕ አዲስ ጽሑፍ",
        "b_entries": "📖 ጽሑፎቼ",
        "b_delete_all": "🗑 ሁሉንም አጥፋ",
        "b_cancel": "❌ ተወው",
        "b_another": "🔄 ሌላ ጥቅስ",
        "b_feelings": "📖 ሌሎች ስሜቶች",
        "b_remind": "🔔 አስታውሰኝ",
        "b_mark_answered": "✅ ተመልሷል በል",
        "b_reopen": "↩️ እንደገና ክፈት",
        "b_delete": "🗑 ሰርዝ",
        "b_back": "⬅️ ተመለስ",
        "b_yes_delete": "🗑 አዎ፣ ሰርዝ",
        "b_yes_delete_all": "🗑 አዎ፣ ሁሉንም ሰርዝ",
        "r_d1": "ነገ",
        "r_d7": "በሳምንት ውስጥ",
        "r_d30": "በወር ውስጥ",
        "r_none": "አያስፈልግም",
        "b_daily": "🌅 የዕለት ጥቅስ",
        "b_weekly": "📅 የሳምንት ነጸብራቅ",
        "b_write_reflection": "✍️ ነጸብራቅ ጻፍ",
        "b_open_journal": "📓 ማስታወሻ ክፈት",
        "b_open_entry": "📄 ጽሑፉን ክፈት",
    },
}

CATEGORIES = ["health", "family", "school", "work", "spiritual", "other"]
CATEGORY_LABELS = {
    "en": {
        "health": "🩺 Health", "family": "👨‍👩‍👧 Family", "school": "📚 School / Exams",
        "work": "💼 Work / Money", "spiritual": "✝️ Spiritual", "other": "🙏 Other",
    },
    "am": {
        "health": "🩺 ጤና", "family": "👨‍👩‍👧 ቤተሰብ", "school": "📚 ትምህርት / ፈተና",
        "work": "💼 ሥራ / ገንዘብ", "spiritual": "✝️ መንፈሳዊ", "other": "🙏 ሌላ",
    },
}  # fmt: skip
FEELING_LABELS = {
    "en": {
        "anxious": "😟 Anxious", "tired": "😩 Exhausted", "afraid": "😨 Afraid",
        "sad": "😢 Sad / grieving", "lonely": "🫂 Lonely", "guilty": "😔 Guilty",
        "lost": "🧭 Need direction", "thankful": "🙌 Thankful",
    },
    "am": {
        "anxious": "😟 የተጨነቅሁ", "tired": "😩 የደከመኝ", "afraid": "😨 የፈራሁ",
        "sad": "😢 ያዘንሁ", "lonely": "🫂 ብቸኝነት የተሰማኝ", "guilty": "😔 የጸጸት ስሜት",
        "lost": "🧭 አቅጣጫ የሚያስፈልገኝ", "thankful": "🙌 አመስጋኝ",
    },
}  # fmt: skip

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
# Ethiopian calendar and clock (used when the user chose Amharic)
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
    now = now.astimezone(EAT)
    if lang == "am":
        year, month, day = gregorian_to_ethiopian(now.date())
        return f"{ET_MONTHS[month - 1]} {day}, {year} ዓ.ም · {ethiopian_clock(now)}"
    return now.strftime("%Y-%m-%d %H:%M") + " (EAT)"


def format_date(dt: datetime, lang: str) -> str:
    dt = dt.astimezone(EAT)
    if lang == "am":
        year, month, day = gregorian_to_ethiopian(dt.date())
        return f"{ET_MONTHS[month - 1]} {day}, {year}"
    return dt.strftime("%b %d, %Y")


def short_date(dt: datetime, lang: str) -> str:
    dt = dt.astimezone(EAT)
    if lang == "am":
        _, month, day = gregorian_to_ethiopian(dt.date())
        return f"{ET_MONTHS[month - 1]} {day}"
    return dt.strftime("%b %d")


# ---------------------------------------------------------------------------
# Database (Neon Postgres). Render's free disk is wiped on every restart.
# ---------------------------------------------------------------------------
pool: asyncpg.Pool | None = None
_bot = None  # set in build_application, used for notifications

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
ALTER TABLE users ADD COLUMN IF NOT EXISTS weekly BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS state TEXT;
CREATE INDEX IF NOT EXISTS users_anon_code_idx ON users (anon_code);
CREATE TABLE IF NOT EXISTS requests (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    category   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    lang       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    id         BIGSERIAL PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    user_id    BIGINT,
    added_by   BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS journal (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    remind_at   TIMESTAMPTZ,
    answered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS journal_user_idx ON journal (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS journal_remind_idx ON journal (remind_at) WHERE remind_at IS NOT NULL;
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
    """Stable anonymous ID shown to the prayer team so a spammer can be
    blocked without anyone seeing who they are."""
    digest = hmac.new(BOT_TOKEN.encode(), str(user_id).encode(), hashlib.sha256)
    return digest.hexdigest()[:8].upper()


def encrypt(text: str) -> str:
    return fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return "⚠️ (this entry can't be decrypted)"


async def get_user(tg_user):
    """Create/fetch the user row. Also links a pending admin @username to this
    person the first time they talk to the bot."""
    row = await db(
        "fetchrow",
        """
        INSERT INTO users (user_id, anon_code) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
        RETURNING lang, blocked, subscribed, weekly, state, anon_code
        """,
        tg_user.id,
        anon_code(tg_user.id),
    )
    if tg_user.username:
        claimed = await db(
            "fetchrow",
            """
            UPDATE admins SET user_id = $1
            WHERE user_id IS NULL AND username = $2 RETURNING username
            """,
            tg_user.id,
            tg_user.username.lower(),
        )
        if claimed and _bot is not None:
            await _notify_new_admin(tg_user)
    return row


async def _notify_new_admin(tg_user):
    try:
        await _bot.send_message(
            chat_id=tg_user.id,
            text="🔧 You are now an admin of this bot. Open 🏠 Menu → 🔧 Admin. "
            "You will also receive the prayer requests.",
        )
        await _bot.send_message(
            chat_id=OWNER_ID, text=f"✅ @{tg_user.username} is now an active admin."
        )
    except TelegramError as e:
        logging.warning("Admin notification failed: %s", e)


async def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID or user_id in RECIPIENT_IDS:
        return True
    return bool(await db("fetchval", "SELECT 1 FROM admins WHERE user_id = $1", user_id))


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def recipient_ids() -> list[int]:
    rows = await db("fetch", "SELECT user_id FROM admins WHERE user_id IS NOT NULL")
    ids = [OWNER_ID, *RECIPIENT_IDS, *[r["user_id"] for r in rows]]
    return list(dict.fromkeys(ids))  # unique, keeps order


async def set_state(user_id: int, state: str | None):
    await db("execute", "UPDATE users SET state = $2 WHERE user_id = $1", user_id, state)


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
# Keyboards and small helpers
# ---------------------------------------------------------------------------
def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


def menu_row(lang: str):
    return [btn(t(lang, "b_menu"), "m:home")]


def menu_keyboard(lang: str, admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn(t(lang, "b_send"), "m:request")],
        [btn(t(lang, "b_scripture"), "m:scripture"), btn(t(lang, "b_journal"), "j:home")],
        [btn(t(lang, "b_reminders"), "m:reminders"), btn(t(lang, "b_language"), "m:language")],
        [btn(t(lang, "b_lords"), "m:lords")],
    ]
    if admin:
        rows.append([btn(t(lang, "b_admin"), "adm:home")])
    return InlineKeyboardMarkup(rows)


def language_keyboard() -> InlineKeyboardMarkup:
    return kb([btn("English", "lang:en"), btn("አማርኛ", "lang:am")])


def category_keyboard(lang: str) -> InlineKeyboardMarkup:
    buttons = [btn(CATEGORY_LABELS[lang][c], f"cat:{c}") for c in CATEGORIES]
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def feelings_keyboard(lang: str) -> InlineKeyboardMarkup:
    buttons = [btn(FEELING_LABELS[lang][f], f"feel:{f}") for f in FEELINGS]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(menu_row(lang))
    return InlineKeyboardMarkup(rows)


async def show(query, text: str, markup=None):
    """Replace the message the button was on."""
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def detect_kind(m: Message) -> str:
    for attr, kind in (
        ("photo", "photo"), ("voice", "voice"), ("video", "video"),
        ("video_note", "video_note"), ("audio", "audio"),
        ("animation", "animation"), ("document", "document"),
    ):  # fmt: skip
        if getattr(m, attr):
            return kind
    return "text"


def parse_username(raw: str) -> str | None:
    match = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{4,31})", raw.strip())
    return match.group(1).lower() if match else None


def remind_datetime(days: int) -> datetime:
    """06:30 Ethiopian-standard time, `days` days from now."""
    target = datetime.now(EAT) + timedelta(days=days)
    return target.replace(hour=6, minute=30, second=0, microsecond=0)


async def deliver(context, from_chat_id: int, message_id: int, header: str, code: str) -> int:
    """Send the header (with a Block button) and a copy of the message to every
    team member. copy_message does not reveal the sender. One failing
    recipient does not stop the others. Returns how many got it."""
    delivered = 0
    for recipient_id in await recipient_ids():
        try:
            head = await context.bot.send_message(
                chat_id=recipient_id,
                text=header,
                reply_markup=kb([btn("🚫 Block sender", f"blk:{code}")]),
            )
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
# Start, menu, language
# ---------------------------------------------------------------------------
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user)
    await set_state(update.effective_user.id, None)
    if user["lang"] is None:
        await update.message.reply_text(LANG_PROMPT, reply_markup=language_keyboard())
        return
    await update.message.reply_text(
        t(user["lang"], "welcome"),
        reply_markup=menu_keyboard(user["lang"], await is_admin(update.effective_user.id)),
    )


async def handle_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user)
    await set_state(update.effective_user.id, None)
    lang = user["lang"] or "en"
    await update.message.reply_text(
        t(lang, "home"),
        reply_markup=menu_keyboard(lang, await is_admin(update.effective_user.id)),
    )


async def handle_lang_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = query.data.split(":", 1)[1]
    await query.answer()
    if lang not in TEXTS:
        return
    await get_user(query.from_user)
    await db(
        "execute", "UPDATE users SET lang = $2 WHERE user_id = $1", query.from_user.id, lang
    )
    await show(
        query, t(lang, "welcome"), menu_keyboard(lang, await is_admin(query.from_user.id))
    )


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    await query.answer()
    user = await get_user(query.from_user)
    await set_state(query.from_user.id, None)
    lang = user["lang"] or "en"

    if action == "home":
        await show(
            query, t(lang, "home"), menu_keyboard(lang, await is_admin(query.from_user.id))
        )
    elif action == "request":
        await show(query, t(lang, "request_prompt"), kb(menu_row(lang)))
    elif action == "scripture":
        await show(query, t(lang, "scripture_prompt"), feelings_keyboard(lang))
    elif action == "language":
        await show(query, LANG_PROMPT, language_keyboard())
    elif action == "lords":
        await show(query, LORDS_PRAYER_TEXT, kb(menu_row(lang)))
    elif action == "reminders":
        await show_reminders(query, user, lang)


# ---------------------------------------------------------------------------
# Scripture for the moment
# ---------------------------------------------------------------------------
async def handle_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    feeling = query.data.split(":", 1)[1]
    await query.answer()
    if feeling not in FEELING_REFS:
        return
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    text = f"{FEELING_LABELS[lang][feeling]}\n\n{verse_block(lang, random.choice(FEELING_REFS[feeling]))}"
    await show(
        query,
        text,
        kb(
            [btn(t(lang, "b_another"), f"feel:{feeling}")],
            [btn(t(lang, "b_feelings"), "m:scripture")],
            menu_row(lang),
        ),
    )


# ---------------------------------------------------------------------------
# Reminders menu (daily verse + weekly reflection toggles)
# ---------------------------------------------------------------------------
async def show_reminders(query, user, lang):
    on, off = t(lang, "on"), t(lang, "off")
    await show(
        query,
        t(
            lang,
            "reminders_menu",
            daily=on if user["subscribed"] else off,
            weekly=on if user["weekly"] else off,
        ),
        kb(
            [btn(t(lang, "b_daily"), "r:daily")],
            [btn(t(lang, "b_weekly"), "r:weekly")],
            menu_row(lang),
        ),
    )


async def handle_reminder_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    which = query.data.split(":", 1)[1]
    await query.answer()
    column = {"daily": "subscribed", "weekly": "weekly"}.get(which)
    if column is None:
        return
    await get_user(query.from_user)
    user = await db(
        "fetchrow",
        f"UPDATE users SET {column} = NOT {column} WHERE user_id = $1 "
        "RETURNING lang, subscribed, weekly",
        query.from_user.id,
    )
    await show_reminders(query, user, user["lang"] or "en")


# ---------------------------------------------------------------------------
# Prayer requests: message -> category -> preview -> send / don't send
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = await get_user(update.effective_user)
    lang = user["lang"] or "en"

    if user["state"]:
        await handle_state_message(update, user, lang)
        return

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
    """Category chosen -> show the preview with Send / Don't send."""
    query = update.callback_query
    category = query.data.split(":", 1)[1]
    await query.answer()
    if category not in CATEGORIES:
        return
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    if getattr(query.message, "reply_to_message", None) is None:
        await show(query, t(lang, "expired"), kb(menu_row(lang)))
        return
    await show(
        query,
        t(lang, "preview", category=CATEGORY_LABELS[lang][category]),
        kb(
            [btn(t(lang, "b_confirm_send"), f"send:{category}")],
            [btn(t(lang, "b_cancel_send"), "cancel")],
        ),
    )


async def handle_cancel_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    # Nothing was sent, so don't make them wait out the cooldown.
    await db(
        "execute",
        "UPDATE users SET last_request_at = NULL WHERE user_id = $1",
        query.from_user.id,
    )
    await show(query, t(lang, "cancelled"), kb(menu_row(lang)))


async def handle_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    category = query.data.split(":", 1)[1]
    await query.answer()
    if category not in CATEGORIES:
        return

    user = await get_user(query.from_user)
    lang = user["lang"] or "en"

    if user["blocked"]:
        await show(query, t(lang, "blocked"), kb(menu_row(lang)))
        return

    # Remove the buttons first. A second tap then fails here, so a request
    # can never be sent twice.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        return

    original = getattr(query.message, "reply_to_message", None)
    if original is None:
        await show(query, t(lang, "expired"), kb(menu_row(lang)))
        return

    header = (
        f"🙏 New prayer request · #{user['anon_code']}\n"
        f"📂 {CATEGORY_LABELS['en'][category]}\n"
        f"🕒 {format_time(datetime.now(EAT), 'en')}"
    )
    delivered = await deliver(
        context, original.chat_id, original.message_id, header, user["anon_code"]
    )

    if delivered:
        await db(
            "execute",
            "INSERT INTO requests (category, kind, lang) VALUES ($1, $2, $3)",
            category,
            detect_kind(original),
            lang,
        )
        rows = []
        if original.text:
            rows.append([btn(t(lang, "b_save_journal"), "js")])
        rows.append(menu_row(lang))
        await show(query, t(lang, "sent", verse=random_verse(lang)), kb(*rows))
    else:
        await db(
            "execute",
            "UPDATE users SET last_request_at = NULL WHERE user_id = $1",
            query.from_user.id,
        )
        await show(query, t(lang, "send_failed"), kb(menu_row(lang)))


async def handle_save_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Save to my journal' under the thank-you message."""
    query = update.callback_query
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    original = getattr(query.message, "reply_to_message", None)
    if original is None or not original.text:
        await query.answer(t(lang, "cant_save"), show_alert=True)
        return
    # Change the buttons FIRST. If a second tap arrives, this fails and we stop,
    # so the entry can never be saved twice.
    try:
        await query.edit_message_reply_markup(
            reply_markup=kb([btn(t(lang, "b_open_journal"), "j:home")], menu_row(lang))
        )
    except TelegramError:
        await query.answer()
        return
    await db(
        "execute",
        "INSERT INTO journal (user_id, body) VALUES ($1, $2)",
        query.from_user.id,
        encrypt(original.text[:MAX_TEXT]),
    )
    await query.answer(t(lang, "saved_toast"))


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user)
    await update.message.reply_text(t(user["lang"] or "en", "unsupported"))


# ---------------------------------------------------------------------------
# Text typed while the bot is waiting for something (journal entry, admin input)
# ---------------------------------------------------------------------------
async def handle_state_message(update: Update, user, lang: str):
    message = update.message
    uid = update.effective_user.id
    state = user["state"]

    if not message.text:
        await message.reply_text(t(lang, "need_text"))
        return

    if state == "journal_new":
        if len(message.text) > MAX_TEXT:
            await message.reply_text(t(lang, "text_too_long"))
            return
        entry_id = await db(
            "fetchval",
            "INSERT INTO journal (user_id, body) VALUES ($1, $2) RETURNING id",
            uid,
            encrypt(message.text),
        )
        await set_state(uid, None)
        await message.reply_text(
            t(lang, "journal_saved"), reply_markup=remind_keyboard(lang, entry_id)
        )

    elif state == "admin_add" and is_owner(uid):
        await set_state(uid, None)
        await message.reply_text(await add_admin(uid, message.text))

    elif state == "admin_block" and await is_admin(uid):
        await set_state(uid, None)
        await message.reply_text(await set_blocked_by_code(message.text, True))

    else:
        await set_state(uid, None)
        await message.reply_text(
            t(lang, "home"),
            reply_markup=menu_keyboard(lang, await is_admin(uid)),
        )


# ---------------------------------------------------------------------------
# Private prayer journal
# ---------------------------------------------------------------------------
def remind_keyboard(lang: str, entry_id: int) -> InlineKeyboardMarkup:
    return kb(
        [btn(t(lang, "r_d1"), f"j:setrem:{entry_id}:1"), btn(t(lang, "r_d7"), f"j:setrem:{entry_id}:7")],
        [btn(t(lang, "r_d30"), f"j:setrem:{entry_id}:30"), btn(t(lang, "r_none"), f"j:setrem:{entry_id}:0")],
    )


async def fetch_entry(user_id: int, entry_id: int):
    return await db(
        "fetchrow",
        "SELECT id, body, created_at, remind_at, answered_at FROM journal "
        "WHERE id = $1 AND user_id = $2",
        entry_id,
        user_id,
    )


async def show_journal_home(query, lang: str, uid: int):
    n = await db("fetchval", "SELECT count(*) FROM journal WHERE user_id = $1", uid)
    await show(
        query,
        t(lang, "journal_home", n=n),
        kb(
            [btn(t(lang, "b_new_entry"), "j:new"), btn(t(lang, "b_entries"), "j:list:0")],
            [btn(t(lang, "b_delete_all"), "j:delall")],
            menu_row(lang),
        ),
    )


async def show_journal_list(query, lang: str, uid: int, offset: int):
    rows = await db(
        "fetch",
        "SELECT id, body, created_at, answered_at FROM journal WHERE user_id = $1 "
        "ORDER BY created_at DESC, id DESC OFFSET $2 LIMIT 9",
        uid,
        offset,
    )
    if not rows and offset == 0:
        await show(
            query,
            t(lang, "journal_empty"),
            kb([btn(t(lang, "b_new_entry"), "j:new")], [btn(t(lang, "b_back"), "j:home")]),
        )
        return
    buttons = []
    for row in rows[:8]:
        lines = decrypt(row["body"]).strip().splitlines()
        snippet = (lines[0][:28] if lines else "") or "…"
        mark = "✅" if row["answered_at"] else "🙏"
        label = f"{mark} {short_date(row['created_at'], lang)} · {snippet}"
        buttons.append([btn(label, f"j:view:{row['id']}")])
    nav = []
    if offset > 0:
        nav.append(btn("⬅️", f"j:list:{max(offset - 8, 0)}"))
    if len(rows) > 8:
        nav.append(btn("➡️", f"j:list:{offset + 8}"))
    if nav:
        buttons.append(nav)
    buttons.append([btn(t(lang, "b_back"), "j:home")])
    await show(query, t(lang, "journal_list"), InlineKeyboardMarkup(buttons))


async def show_entry(query, lang: str, row):
    header = format_date(row["created_at"], lang)
    if row["answered_at"]:
        header += f" · {t(lang, 'answered')}"
    text = f"📄 {header}\n\n{decrypt(row['body'])}"
    if row["remind_at"]:
        text += f"\n\n🔔 {format_date(row['remind_at'], lang)}"
    answer_btn = (
        btn(t(lang, "b_reopen"), f"j:ans:{row['id']}")
        if row["answered_at"]
        else btn(t(lang, "b_mark_answered"), f"j:ans:{row['id']}")
    )
    await show(
        query,
        text,
        kb(
            [btn(t(lang, "b_remind"), f"j:rem:{row['id']}"), answer_btn],
            [btn(t(lang, "b_delete"), f"j:del:{row['id']}"), btn(t(lang, "b_back"), "j:list:0")],
        ),
    )


async def handle_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    await query.answer()
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    uid = query.from_user.id

    if action == "home":
        await set_state(uid, None)
        await show_journal_home(query, lang, uid)
        return
    if action == "new":
        await set_state(uid, "journal_new")
        await show(query, t(lang, "journal_new_prompt"), kb([btn(t(lang, "b_cancel"), "j:home")]))
        return
    if action == "list":
        await show_journal_list(query, lang, uid, int(parts[2]))
        return
    if action == "delall":
        n = await db("fetchval", "SELECT count(*) FROM journal WHERE user_id = $1", uid)
        await show(
            query,
            t(lang, "confirm_delete_all", n=n),
            kb(
                [btn(t(lang, "b_yes_delete_all"), "j:delallyes")],
                [btn(t(lang, "b_cancel"), "j:home")],
            ),
        )
        return
    if action == "delallyes":
        await db("execute", "DELETE FROM journal WHERE user_id = $1", uid)
        await show(query, t(lang, "all_deleted"), kb(menu_row(lang)))
        return

    # Everything below works on one entry, and only the owner of it.
    try:
        entry_id = int(parts[2])
    except (IndexError, ValueError):
        return
    row = await fetch_entry(uid, entry_id)
    if row is None:
        await show(query, t(lang, "entry_missing"), kb([btn(t(lang, "b_back"), "j:list:0")]))
        return

    if action == "view":
        await show_entry(query, lang, row)
    elif action == "rem":
        await show(
            query,
            t(lang, "remind_ask"),
            InlineKeyboardMarkup(
                [list(r) for r in remind_keyboard(lang, entry_id).inline_keyboard]
                + [[btn(t(lang, "b_back"), f"j:view:{entry_id}")]]
            ),
        )
    elif action == "setrem":
        days = int(parts[3])
        when = remind_datetime(days) if days else None
        await db(
            "execute",
            "UPDATE journal SET remind_at = $3 WHERE id = $1 AND user_id = $2",
            entry_id,
            uid,
            when,
        )
        text = t(lang, "remind_set", date=format_date(when, lang)) if when else t(lang, "remind_none")
        await show(
            query,
            text,
            kb(
                [btn(t(lang, "b_open_entry"), f"j:view:{entry_id}")],
                [btn(t(lang, "b_open_journal"), "j:home")],
            ),
        )
    elif action == "ans":
        await db(
            "execute",
            "UPDATE journal SET answered_at = CASE WHEN answered_at IS NULL "
            "THEN now() ELSE NULL END WHERE id = $1 AND user_id = $2",
            entry_id,
            uid,
        )
        await show_entry(query, lang, await fetch_entry(uid, entry_id))
    elif action == "del":
        await show(
            query,
            t(lang, "confirm_delete"),
            kb(
                [btn(t(lang, "b_yes_delete"), f"j:delyes:{entry_id}")],
                [btn(t(lang, "b_cancel"), f"j:view:{entry_id}")],
            ),
        )
    elif action == "delyes":
        await db("execute", "DELETE FROM journal WHERE id = $1 AND user_id = $2", entry_id, uid)
        await show(
            query,
            t(lang, "deleted"),
            kb([btn(t(lang, "b_entries"), "j:list:0")], menu_row(lang)),
        )


# ---------------------------------------------------------------------------
# Admin: owner (ultimate admin) and admins
#   admins: see stats, block senders, see the admin list
#   owner : everything above + unblock, add/remove admins by @username
# ---------------------------------------------------------------------------
async def stats_text() -> str:
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
               count(*) FILTER (WHERE weekly) AS weekly,
               count(*) FILTER (WHERE lang = 'am') AS amharic,
               count(*) FILTER (WHERE lang = 'en') AS english,
               count(*) FILTER (WHERE blocked) AS blocked
        FROM users
        """,
    )
    journal = await db("fetchval", "SELECT count(*) FROM journal")
    lines = [
        "📊 Stats",
        "",
        f"Requests: today {counts['today']} · last 7 days {counts['week']} · total {counts['total']}",
        "",
        "By category: "
        + (" · ".join(f"{CATEGORY_LABELS['en'][r['category']]} {r['n']}" for r in by_category) or "none yet"),
        "By type: " + (" · ".join(f"{r['kind']} {r['n']}" for r in by_kind) or "none yet"),
        "",
        f"Users: {users['total']} · Amharic {users['amharic']} · English {users['english']} · blocked {users['blocked']}",
        f"Daily verse subscribers: {users['subscribed']} · weekly reflection: {users['weekly']}",
        f"Journal entries (count only): {journal}",
    ]
    return "\n".join(lines)


async def set_blocked_by_code(raw: str, blocked: bool) -> str:
    code = raw.strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{8}", code):
        return "That doesn't look like a sender code. It's 8 characters, like #A1B2C3D4."
    result = await db(
        "execute", "UPDATE users SET blocked = $2 WHERE anon_code = $1", code, blocked
    )
    if int(result.split()[-1]):
        return f"{'🚫 Blocked' if blocked else '✅ Unblocked'} #{code}"
    return f"No sender found with code #{code}."


async def add_admin(owner_id: int, raw: str) -> str:
    username = parse_username(raw)
    if username is None:
        return "That doesn't look like a Telegram @username (5-32 letters, numbers or _)."
    row = await db(
        "fetchrow",
        "INSERT INTO admins (username, added_by) VALUES ($1, $2) "
        "ON CONFLICT (username) DO NOTHING RETURNING id",
        username,
        owner_id,
    )
    if row is None:
        return f"@{username} is already an admin."
    return (
        f"✅ Added @{username}. They become active the first time they open this bot "
        "and send /start, and you'll get a message when that happens."
    )


async def admin_home_text_and_keyboard(uid: int):
    rows = [
        [btn("📊 Stats", "adm:stats"), btn("🚫 Blocked senders", "adm:blocked")],
        [btn("🚫 Block a code", "adm:block"), btn("👥 Admins", "adm:admins")],
        [btn("🏠 Menu", "m:home")],
    ]
    role = "Owner (full control)" if is_owner(uid) else "Admin"
    return f"🔧 Admin panel\n\nYou are: {role}", InlineKeyboardMarkup(rows)


async def render_blocked(query, uid: int):
    back = [btn("⬅️ Back", "adm:home")]
    rows = await db("fetch", "SELECT anon_code FROM users WHERE blocked ORDER BY anon_code LIMIT 40")
    if not rows:
        await show(query, "No blocked senders.", kb(back))
        return
    buttons = (
        [[btn(f"✅ Unblock #{r['anon_code']}", f"adm:unb:{r['anon_code']}")] for r in rows]
        if is_owner(uid)
        else []
    )
    text = "🚫 Blocked senders:\n" + "\n".join(f"#{r['anon_code']}" for r in rows)
    if not is_owner(uid):
        text += "\n\nOnly the owner can unblock."
    await show(query, text, InlineKeyboardMarkup(buttons + [back]))


async def render_admins(query, uid: int):
    back = [btn("⬅️ Back", "adm:home")]
    rows = await db("fetch", "SELECT id, username, user_id FROM admins ORDER BY id")
    lines = ["👥 Admins", "", "👑 Owner", f"⚙️ {len(RECIPIENT_IDS)} team member(s) from server settings"]
    buttons = []
    for r in rows:
        status = "active" if r["user_id"] else "pending (needs to open the bot and send /start)"
        lines.append(f"• @{r['username']} — {status}")
        if is_owner(uid):
            buttons.append([btn(f"❌ Remove @{r['username']}", f"adm:rm:{r['id']}")])
    if is_owner(uid):
        buttons.append([btn("➕ Add admin", "adm:add")])
    await show(query, "\n".join(lines), InlineKeyboardMarkup(buttons + [back]))


async def handle_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    uid = query.from_user.id
    if not await is_admin(uid):
        await query.answer("Admins only.", show_alert=True)
        return
    await query.answer()
    await get_user(query.from_user)
    back = [btn("⬅️ Back", "adm:home")]

    if action == "home":
        await set_state(uid, None)
        text, markup = await admin_home_text_and_keyboard(uid)
        await show(query, text, markup)

    elif action == "stats":
        await show(query, await stats_text(), kb(back))

    elif action == "blocked":
        await render_blocked(query, uid)

    elif action == "unb":
        if not is_owner(uid):
            await query.answer("Only the owner can unblock.", show_alert=True)
            return
        await db("execute", "UPDATE users SET blocked = FALSE WHERE anon_code = $1", parts[2])
        await render_blocked(query, uid)

    elif action == "block":
        await set_state(uid, "admin_block")
        await show(
            query,
            "Send the sender code to block (the #CODE on a request).",
            kb([btn("❌ Cancel", "adm:home")]),
        )

    elif action == "admins":
        await render_admins(query, uid)

    elif action == "add":
        if not is_owner(uid):
            await query.answer("Only the owner can add admins.", show_alert=True)
            return
        await set_state(uid, "admin_add")
        await show(
            query,
            "Send the Telegram @username of the new admin.\n\nThey will also receive the prayer requests.",
            kb([btn("❌ Cancel", "adm:home")]),
        )

    elif action == "rm":
        if not is_owner(uid):
            await query.answer("Only the owner can remove admins.", show_alert=True)
            return
        await db("execute", "DELETE FROM admins WHERE id = $1", int(parts[2]))
        await render_admins(query, uid)


async def handle_block_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'🚫 Block sender' under a request header."""
    query = update.callback_query
    if not await is_admin(query.from_user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    result = await set_blocked_by_code(query.data.split(":", 1)[1], True)
    await query.answer(result, show_alert=True)
    if result.startswith("🚫"):
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass


async def _admin_command_guard(update: Update, owner_only: bool) -> bool:
    uid = update.effective_user.id
    allowed = is_owner(uid) if owner_only else await is_admin(uid)
    return allowed  # unauthorized users get no reply, so the commands stay hidden


async def handle_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, False):
        await update.message.reply_text(await stats_text())


async def handle_block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, False):
        if not context.args:
            await update.message.reply_text("Usage: /block CODE")
            return
        await update.message.reply_text(await set_blocked_by_code(context.args[0], True))


async def handle_unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):
        if not context.args:
            await update.message.reply_text("Usage: /unblock CODE")
            return
        await update.message.reply_text(await set_blocked_by_code(context.args[0], False))


async def handle_addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):
        if not context.args:
            await update.message.reply_text("Usage: /addadmin @username")
            return
        await update.message.reply_text(await add_admin(update.effective_user.id, context.args[0]))


async def handle_removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):
        username = parse_username(context.args[0]) if context.args else None
        if not username:
            await update.message.reply_text("Usage: /removeadmin @username")
            return
        result = await db("execute", "DELETE FROM admins WHERE username = $1", username)
        await update.message.reply_text(
            f"Removed @{username}." if int(result.split()[-1]) else f"@{username} is not in the admin list."
        )


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """06:30 Ethiopian-standard time: verse for daily-verse subscribers."""
    rows = await db("fetch", "SELECT user_id, lang FROM users WHERE subscribed AND NOT blocked")
    logging.info("Sending daily verse to %d subscribers", len(rows))
    for row in rows:
        lang = row["lang"] or "en"
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=t(lang, "reminder", verse=daily_verse(lang)),
                reply_markup=kb(menu_row(lang)),
            )
        except Forbidden:
            await db("execute", "UPDATE users SET subscribed = FALSE WHERE user_id = $1", row["user_id"])
        except TelegramError as e:
            logging.warning("Daily verse failed for %s: %s", row["user_id"], e)
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limit


async def send_weekly_reflection(context: ContextTypes.DEFAULT_TYPE):
    """Sunday 18:00 Ethiopian-standard time."""
    rows = await db(
        "fetch",
        """
        SELECT u.user_id, u.lang,
               count(j.id) FILTER (WHERE j.created_at >= now() - interval '7 days') AS new,
               count(j.id) FILTER (WHERE j.answered_at >= now() - interval '7 days') AS answered,
               count(j.id) FILTER (WHERE j.answered_at IS NULL) AS open
        FROM users u LEFT JOIN journal j ON j.user_id = u.user_id
        WHERE u.weekly AND NOT u.blocked
        GROUP BY u.user_id, u.lang
        """,
    )
    logging.info("Sending weekly reflection to %d users", len(rows))
    for row in rows:
        lang = row["lang"] or "en"
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=t(
                    lang, "weekly", new=row["new"], answered=row["answered"],
                    open=row["open"], verse=daily_verse(lang),
                ),
                reply_markup=kb(
                    [btn(t(lang, "b_write_reflection"), "j:new")],
                    [btn(t(lang, "b_open_journal"), "j:home")],
                ),
            )
        except Forbidden:
            await db("execute", "UPDATE users SET weekly = FALSE WHERE user_id = $1", row["user_id"])
        except TelegramError as e:
            logging.warning("Weekly reflection failed for %s: %s", row["user_id"], e)
        await asyncio.sleep(0.05)


async def check_journal_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Every 10 minutes: send journal reminders that have come due."""
    rows = await db(
        "fetch",
        """
        WITH due AS (
            UPDATE journal SET remind_at = NULL
            WHERE remind_at IS NOT NULL AND remind_at <= now()
            RETURNING id, user_id, body
        )
        SELECT due.id, due.user_id, due.body, u.lang
        FROM due LEFT JOIN users u ON u.user_id = due.user_id
        """,
    )
    for row in rows:
        lang = row["lang"] or "en"
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=t(lang, "reminder_due", text=decrypt(row["body"])),
                reply_markup=kb(
                    [btn(t(lang, "b_mark_answered"), f"j:ans:{row['id']}"), btn(t(lang, "b_remind"), f"j:rem:{row['id']}")],
                    [btn(t(lang, "b_open_entry"), f"j:view:{row['id']}")],
                ),
            )
        except Forbidden:
            pass  # they blocked the bot; the reminder is dropped
        except TelegramError as e:
            logging.warning("Journal reminder failed for %s: %s", row["user_id"], e)
            await db(
                "execute",
                "UPDATE journal SET remind_at = now() + interval '1 hour' WHERE id = $1",
                row["id"],
            )
        await asyncio.sleep(0.05)


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
    global _bot
    # updater(None): updates arrive through our own web server below
    application = ApplicationBuilder().token(BOT_TOKEN).updater(None).build()
    _bot = application.bot

    private = filters.ChatType.PRIVATE
    for name, handler in (
        ("start", handle_start),
        ("menu", handle_menu_command),
        ("help", handle_menu_command),
        ("stats", handle_stats_command),
        ("block", handle_block_command),
        ("unblock", handle_unblock_command),
        ("addadmin", handle_addadmin_command),
        ("removeadmin", handle_removeadmin_command),
    ):
        application.add_handler(CommandHandler(name, handler, filters=private))

    for pattern, handler in (
        (r"^lang:", handle_lang_choice),
        (r"^m:", handle_menu),
        (r"^feel:", handle_feeling),
        (r"^r:", handle_reminder_toggle),
        (r"^cat:", handle_category),
        (r"^send:", handle_send),
        (r"^cancel$", handle_cancel_send),
        (r"^js$", handle_save_request),
        (r"^j:", handle_journal),
        (r"^adm:", handle_admin),
        (r"^blk:", handle_block_button),
    ):
        application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    application.add_handler(MessageHandler(REQUEST_FILTER, handle_message))
    # Anything else in private chat (stickers, locations, ...)
    application.add_handler(MessageHandler(private & ~filters.COMMAND, handle_unsupported))
    application.add_error_handler(on_error)

    jobs = application.job_queue
    if jobs is None:
        raise RuntimeError("Install python-telegram-bot[job-queue] for the reminders.")
    jobs.run_daily(send_daily_reminder, time=DAILY_TIME_UTC, name="daily_verse")
    jobs.run_daily(send_weekly_reflection, time=WEEKLY_TIME_UTC, days=(0,), name="weekly_reflection")
    jobs.run_repeating(check_journal_reminders, interval=600, first=60, name="journal_reminders")
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
        [BotCommand("start", "Start"), BotCommand("menu", "Open the menu")]
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
