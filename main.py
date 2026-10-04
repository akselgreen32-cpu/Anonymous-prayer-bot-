import os
import math
import re
import json
import time
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
from xml.sax.saxutils import escape as xml_escape

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
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
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

# ---------------------------------------------------------------------------
# Keep secrets out of the logs. The HTTP library prints every Telegram call
# (the URL contains the bot token), so it is silenced, and as a safety net any
# secret that still reaches a log line (even inside an error traceback) is
# replaced before it is printed.
# ---------------------------------------------------------------------------
class RedactingFormatter(logging.Formatter):
    SECRETS = [x for x in (BOT_TOKEN, JOURNAL_SECRET, DATABASE_URL) if x]

    def format(self, record):
        text = super().format(record)
        for secret in self.SECRETS:
            text = text.replace(secret, "<hidden>")
        return text


for _handler in logging.getLogger().handlers:
    _handler.setFormatter(RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Base prayer team (also admins). Comma-separated Telegram IDs.
RECIPIENT_IDS = [int(i) for i in os.environ["MY_USER_ID"].split(",") if i.strip()]
# The ultimate admin. Defaults to the first ID in MY_USER_ID.
OWNER_ID = int(os.environ.get("OWNER_ID") or RECIPIENT_IDS[0])
PORT = int(os.environ.get("PORT", 8080))
# Shown under Amharic verses. Leave empty for none.
AMHARIC_BIBLE_CREDIT = os.environ.get("AMHARIC_BIBLE_CREDIT", "")

WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
if not WEBHOOK_URL:
    raise RuntimeError("Set WEBHOOK_URL (Render sets RENDER_EXTERNAL_URL itself).")

COOLDOWN_SECONDS = 15
MAX_TEXT = 3500
STATE_TTL_MINUTES = 30  # "waiting for text" states are ignored after this long
ADMIN_PENDING_HOURS = 48  # a pending admin @username can be claimed for this long
DEVELOPER_URL = "https://t.me/akseling"
ADMIN_LANG = "en"  # the admin panel and admin notices are always English
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
        "help": (
            "ℹ️ About this bot\n\n"
            "Anonymous Prayer bot lets you share a prayer request without revealing who "
            "you are, so someone can pray for you. You can also find Scripture for how "
            "you feel, keep a private prayer journal, and set gentle reminders.\n\n"
            "Tap 🏠 Menu to begin, or tap Contact the developer if you need help."
        ),
        "request_prompt": "✍️ Send me your prayer request now: text, a photo, a voice message, a video or a file.",
        "ask_category": "📂 What is your request about? Choose a category:",
        "preview": (
            "📝 Preview\n\nCategory: {category}\n\nYour message above will be shared "
            "anonymously with the prayer team. Send it?"
        ),
        "cancelled": "Cancelled. Nothing was sent.",
        "sent_short": "✅ Sent anonymously.",
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
        # prayer request header (what the prayer team sees)
        "req_header": "🙏 New prayer request · #{code}\n📂 {category}\n🕒 {time}",
        "b_block_sender": "🚫 Block sender",
        # buttons
        "b_send": "🙏 Send a prayer request",
        "b_scripture": "📖 Scripture for me",
        "b_journal": "📓 My journal",
        "b_reminders": "🔔 Reminders",
        "b_language": "🌍 Language",
        "b_lords": "✝️ Lord's Prayer",
        "b_help": "ℹ️ Help",
        "b_contact": "💬 Contact the developer",
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
        # ---- admin panel ----
        "adm_panel": "🔧 Admin panel\n\nYou are: {role}",
        "role_owner": "Owner (full control)",
        "role_admin": "Admin",
        "b_stats": "📊 Stats",
        "b_refresh": "🔄 Refresh",
        "b_text_view": "📝 Text",
        "b_image_view": "🖼 Picture",
        "b_svg_view": "🎨 SVG",
        "b_panel": "⬅️ Admin panel",
        "b_blocked": "🚫 Blocked senders",
        "b_block_code": "🚫 Block a code",
        "b_admins": "👥 Admins",
        "b_broadcast": "📢 Message all users",
        "b_unblock": "✅ Unblock #{code}",
        "b_remove_admin": "❌ Remove @{u}",
        "b_add_admin": "➕ Add admin",
        "admins_only": "Admins only.",
        "owner_only": "Only the owner can do this.",
        "no_blocked": "No blocked senders.",
        "blocked_list": "🚫 Blocked senders:\n{items}",
        "blocked_note": "\n\nOnly the owner can unblock.",
        "block_prompt": "Send the sender code to block (the #CODE on a request).",
        "bad_code": "That doesn't look like a sender code. It's 8 to 12 characters, like #A1B2C3D4E5F6.",
        "blocked_ok": "🚫 Blocked #{code}",
        "unblocked_ok": "✅ Unblocked #{code}",
        "code_missing": "No sender found with code #{code}.",
        "none_yet": "none yet",
        "admins_head": "👥 Admins\n\n👑 Owner\n⚙️ {n} team member(s) from server settings",
        "status_active": "active",
        "status_pending": "pending (must open the bot and send /start within {h}h)",
        "status_invited": "invited, waiting for their answer ({h}h left)",
        "admin_add_prompt": "Send the Telegram @username of the new admin.\n\nThey will also receive the prayer requests.",
        "add_bad": "That doesn't look like a Telegram @username (5-32 letters, numbers or _).",
        "add_dup": "@{u} is already an admin.",
        "add_active": "✅ Invitation sent to @{u}. They become an admin once they accept.",
        "add_pending": (
            "✅ Added @{u}. They have 48 hours to open this bot and send /start. They "
            "will then be asked to accept or decline, and you'll be told their answer. "
            "Only add people who can do this right away."
        ),
        "admin_now": (
            "🔧 You are now an admin of this bot. Open 🏠 Menu → 🔧 Admin. "
            "You will also receive the prayer requests."
        ),
        "owner_accepted": "✅ @{u} accepted the admin role.",
        "owner_declined": "❌ @{u} declined the admin role.",
        "owner_quit": "🚪 @{u} quit the admin role.",
        "admin_invite": (
            "🔧 Admin invitation\n\nYou have been asked to become an admin of this bot. "
            "Admins receive the prayer requests, which are private and must never be "
            "shared with anyone. Do you accept?"
        ),
        "b_accept_admin": "✅ Accept",
        "b_decline_admin": "❌ Decline",
        "admin_declined": "Okay, you declined. Nothing has changed.",
        "invite_gone": "This invitation is no longer valid.",
        "b_quit": "🚪 Quit admin role",
        "quit_confirm": (
            "Quit being an admin?\n\nYou will stop receiving prayer requests. "
            "The owner will be notified."
        ),
        "b_quit_yes": "🚪 Yes, quit",
        "quit_done": "You are no longer an admin.",
        "quit_server": "Your admin access comes from the server settings, so it can't be changed here. Please ask the owner.",
        "removed": "Removed @{u}.",
        "not_admin": "@{u} is not in the admin list.",
        "usage_block": "Usage: /block CODE",
        "usage_unblock": "Usage: /unblock CODE",
        "usage_addadmin": "Usage: /addadmin @username",
        "usage_removeadmin": "Usage: /removeadmin @username",
        # ---- announcements (owner only) ----
        "bc_prompt": (
            "📢 Send the message you want to announce. It can be text, a photo, a video, "
            "a voice message or a file.\n\nYou'll choose who receives it next."
        ),
        "bc_choose": "Who should receive this message?",
        "bc_preview": "📝 Preview. This is exactly what people will receive:",
        "b_bc_all": "👥 All users ({n})",
        "b_bc_subs": "🌅 Daily verse subscribers ({n})",
        "aud_all": "all users",
        "aud_subs": "daily verse subscribers",
        "bc_confirm": "Send the message above to {n} people ({audience})? This can't be undone.",
        "b_bc_yes": "✅ Yes, send now",
        "bc_started": "📤 Sending to {n} people… I'll tell you when it's done.",
        "bc_done": "✅ Announcement finished.\nDelivered: {ok}\nFailed: {failed}",
        "bc_expired": "I couldn't find that message. Start again from 📢.",
    },
    "am": {
        "welcome": (
            "🙏 እንኳን ወደ Anonymous Prayer bot በደህና መጡ።\n\n"
            "የጸሎት ጥያቄዎን ይላኩ፤ ማንነትዎ ሳይታወቅ ይተላለፋል፤ ሌላ ሰውም ስለእርስዎ ይጸልያል። "
            "እንዲሁም ለሚሰማዎት ስሜት የሚሆን ጥቅስ ማግኘት፣ የግል የጸሎት ማስታወሻ መያዝ እና "
            "የጥሞና ግዜ ማንቂያ ማዘጋጀት ይችላሉ።\n\nከታች ይምረጡ፦"
        ),
        "home": "🙏 Anonymous Prayer bot\n\nአንድ አማራጭ ይምረጡ፦",
        "help": (
            "ℹ️ ስለዚህ ቦት\n\n"
            "Anonymous Prayer bot ማንነትዎ ሳይታወቅ የጸሎት ጥያቄ እንዲያካፍሉ ያስችልዎታል፤ ሌላ ሰውም "
            "ስለእርስዎ ይጸልያል። እንዲሁም ለሚሰማዎት ስሜት የሚሆን ጥቅስ ማግኘት፣ የግል የጸሎት ማስታወሻ "
            "መያዝ እና ማንቂያ ማዘጋጀት ይችላሉ።\n\n"
            "ለመጀመር 🏠 ዋና ማውጫን ይንኩ፤ እርዳታ ካስፈለግዎ Contact the developer."
        ),
        "request_prompt": "✍️ የጸሎት ጥያቄዎን አሁን ይላኩልኝ፦ ጽሑፍ፣ ፎቶ፣ ድምፅ፣ ቪዲዮ ወይም ፋይል።",
        "ask_category": "📂 ጥያቄዎ ስለ ምንድን ነው? ከስር ይምረጡ፦",
        "preview": (
            "📝 የጸሎት ጥያቄዎ\n\nምድብ፦ {category}\n\nከላይ ያለው መልእክትዎ ስምዎ ሳይታወቅ ለጸሎት ቡድኑ "
            "ይላካል። ይላክ?"
        ),
        "cancelled": "ተሰርዟል። ምንም አልተላከም።",
        "sent_short": "✅ ማንነትዎ ሳይታወቅ ተልኳል።",
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
            "📓 የእኔ የግል የጸሎት ማስታወሻ\n\nይህን ማስታወሻ መክፈት የሚችሉት እርስዎ ብቻ ነዎት። አንድ ጽሑፍ ወይም "
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
        "confirm_delete_all": "ሁሉም {n} ጽሑፎች ይጥፉ? ከጠፉ መመለስ አይቻልም።",
        "deleted": "🗑 ተሰርዟል።",
        "all_deleted": "🗑 ማስታወሻዎ አሁን ባዶ ነው።",
        "saved_toast": "✅ በግል ማስታወሻዎ ተቀምጧል።",
        "cant_save": "ጽሑፍ ያላቸው ጥያቄዎች ብቻ ሊቀመጡ ይችላሉ።",
        "entry_missing": "ይህ ጽሑፍ አልተገኘም።",
        "answered": "✅ ተመልሷል",
        "reminders_menu": (
            "🔔 የጥሞና ግዜ ማንቂያ\n\n🌅 የዕለት የጥሞና ግዜ ጥቅስ (ጠዋት 12:30)፦ {daily}\n"
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
        "req_header": "🙏 አዲስ የጸሎት ጥያቄ · #{code}\n📂 {category}\n🕒 {time}",
        "b_block_sender": "🚫 ላኪውን አግድ",
        "b_send": "🙏 የጸሎት ጥያቄ ላክ",
        "b_scripture": "📖 ጥቅስ ለእኔ",
        "b_journal": "📓 የግል የጸሎት ማስታወሻ",
        "b_reminders": "🔔 የጥሞና ግዜ ማንቂያ",
        "b_language": "🌍 ቋንቋ",
        "b_lords": "✝️ የጌታ ጸሎት",
        "b_help": "ℹ️ እገዛ",
        "b_contact": "💬 Contact the developer",
        "b_admin": "🔧 አስተዳዳሪ",
        "b_menu": "🏠 ዋና ማውጫ",
        "b_confirm_send": "✅ ላክ",
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
        "b_daily": "🌅 የዕለት የጥሞና ግዜ ጥቅስ",
        "b_weekly": "📅 የሳምንት ነጸብራቅ",
        "b_write_reflection": "✍️ ነጸብራቅ ጻፍ",
        "b_open_journal": "📓 ማስታወሻ ክፈት",
        "b_open_entry": "📄 ጽሑፉን ክፈት",
        # ---- admin invitation (the admin panel itself is English only) ----
        "admin_now": (
            "🔧 አሁን የዚህ ቦት አስተዳዳሪ ሆነዋል። 🏠 ዋና ማውጫ → 🔧 አስተዳዳሪ ይክፈቱ። "
            "የጸሎት ጥያቄዎችንም ይቀበላሉ።"
        ),
        "admin_invite": (
            "🔧 የአስተዳዳሪነት ጥያቄ\n\nየዚህ ቦት አስተዳዳሪ እንዲሆኑ ተጠይቀዋል። አስተዳዳሪዎች የጸሎት "
            "ጥያቄዎችን ይቀበላሉ፤ ጥያቄዎቹ የግል ስለሆኑ ለማንም መነገር የለባቸውም። ይቀበላሉ?"
        ),
        "b_accept_admin": "✅ ተቀበል",
        "b_decline_admin": "❌ አልቀበልም",
        "admin_declined": "እሺ፣ አልተቀበሉም። ምንም አልተቀየረም።",
        "invite_gone": "ይህ ጥያቄ ከአሁን በኋላ አይሰራም።",
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
        "anxious": "😟 ተጨንቄያለሁ", "tired": "😩 ደክሞኛል", "afraid": "😨 ፈርቻለሁ",
        "sad": "😢 አዝኛለሁ", "lonely": "🫂 ብቸኝነት ይሰማኛል", "guilty": "😔 የጸጸት ስሜት ይሰማኛል",
        "lost": "🧭 አቅጣጫ ጠፍቶብኛል", "thankful": "🙌 እያመሰገንኩ ነው",
    },
}  # fmt: skip

LORDS_PRAYER_TEXT = {
    "en": (
        "🙏 The Lord's Prayer\n"
        "📖 Matthew 6:9-13\n\n"
        "Our Father in heaven,\n"
        "hallowed be your name,\n"
        "your kingdom come,\n"
        "your will be done, on earth as it is in heaven.\n"
        "Give us today our daily bread.\n"
        "And forgive us our debts, as we also have forgiven our debtors.\n"
        "And lead us not into temptation, but deliver us from the evil one,\n"
        "for yours is the kingdom and the power and the glory forever. Amen."
    ),
    "am": (
        "🙏 የጌታ ጸሎት\n"
        "📖 ማቴዎስ 6:9-13\n\n"
        "በሰማያት የምትኖር አባታችን ሆይ፥\n"
        "ስምህ ይቀደስ፤\n"
        "መንግሥትህ ትምጣ፤\n"
        "ፈቃድህ በሰማይ እንደ ሆነች እንዲሁ በምድር ትሁን፤\n"
        "የዕለት እንጀራችንን ዛሬ ስጠን፤\n"
        "እኛም ደግሞ የበደሉንን ይቅር እንደምንል በደላችንን ይቅር በለን፤\n"
        "ወደ ፈተናም አታግባን ከክፉው አድነን እንጂ፤\n"
        "መንግሥት ያንተ ናትና ኃይልም ክብርም ለዘለዓለሙ፤ አሜን።"
    ),
}


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
ALTER TABLE users ADD COLUMN IF NOT EXISTS state_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS uname_hash TEXT;
CREATE INDEX IF NOT EXISTS users_anon_code_idx ON users (anon_code);
CREATE INDEX IF NOT EXISTS users_uname_idx ON users (uname_hash);
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
ALTER TABLE admins ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
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
    blocked without anyone seeing who they are. 12 hex characters, so two
    people sharing a code is practically impossible. (Older users keep their
    original 8-character codes, which still work.)"""
    digest = hmac.new(BOT_TOKEN.encode(), str(user_id).encode(), hashlib.sha256)
    return digest.hexdigest()[:12].upper()


def uname_hash(username: str) -> str:
    """We never store Telegram @usernames of ordinary users. Only a keyed hash,
    which lets the owner promote someone who already started the bot."""
    digest = hmac.new(BOT_TOKEN.encode(), b"uname:" + username.lower().encode(), hashlib.sha256)
    return digest.hexdigest()


def encrypt(text: str) -> str:
    return fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return "⚠️ (this entry can't be decrypted)"


async def purge_expired_admins():
    """Admin invitations nobody answered in time (or never claimed) are deleted,
    so a released or changed username can never be claimed by a stranger later."""
    await db(
        "execute",
        "DELETE FROM admins WHERE accepted_at IS NULL "
        "AND created_at <= now() - make_interval(hours => $1)",
        ADMIN_PENDING_HOURS,
    )


async def get_user(tg_user):
    """Create/fetch the user row. Also links a pending admin @username to this
    person the first time they talk to the bot (only while it hasn't expired).
    A 'waiting for text' state older than STATE_TTL_MINUTES is ignored."""
    row = await db(
        "fetchrow",
        f"""
        INSERT INTO users (user_id, anon_code, uname_hash) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET uname_hash = EXCLUDED.uname_hash
        RETURNING lang, blocked, subscribed, weekly, anon_code,
                  CASE WHEN state_at IS NOT NULL
                            AND state_at > now() - interval '{STATE_TTL_MINUTES} minutes'
                       THEN state END AS state
        """,
        tg_user.id,
        anon_code(tg_user.id),
        uname_hash(tg_user.username) if tg_user.username else None,
    )
    if tg_user.username:
        claimed = await db(
            "fetchrow",
            """
            UPDATE admins SET user_id = $1
            WHERE user_id IS NULL AND username = $2
              AND created_at > now() - make_interval(hours => $3)
            RETURNING username
            """,
            tg_user.id,
            tg_user.username.lower(),
            ADMIN_PENDING_HOURS,
        )
        if claimed and _bot is not None:
            await _send_admin_invite(tg_user.id, row["lang"] or "en")
    return row


async def _send_admin_invite(user_id: int, lang: str):
    """Nobody becomes an admin without saying yes: they get Accept / Decline."""
    try:
        await _bot.send_message(
            chat_id=user_id,
            text=t(lang, "admin_invite"),
            reply_markup=kb(
                [btn(t(lang, "b_accept_admin"), "ainv:yes"), btn(t(lang, "b_decline_admin"), "ainv:no")]
            ),
        )
    except TelegramError as e:
        logging.warning("Admin invitation failed: %s", e)


async def _notify_owner(text: str):
    """Tell the owner about admin changes (accepted, declined, quit)."""
    try:
        await _bot.send_message(chat_id=OWNER_ID, text=text)
    except TelegramError as e:
        logging.warning("Owner notification failed: %s", e)


async def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID or user_id in RECIPIENT_IDS:
        return True
    return bool(
        await db(
            "fetchval",
            "SELECT 1 FROM admins WHERE user_id = $1 AND accepted_at IS NOT NULL",
            user_id,
        )
    )


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def recipient_ids() -> list[int]:
    rows = await db(
        "fetch", "SELECT user_id FROM admins WHERE user_id IS NOT NULL AND accepted_at IS NOT NULL"
    )
    ids = [OWNER_ID, *RECIPIENT_IDS, *[r["user_id"] for r in rows]]
    return list(dict.fromkeys(ids))  # unique, keeps order


async def set_state(user_id: int, state: str | None):
    await db(
        "execute",
        "UPDATE users SET state = $2, state_at = now() WHERE user_id = $1",
        user_id,
        state,
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
# Keyboards and small helpers
# ---------------------------------------------------------------------------
def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


def menu_row(lang: str):
    """Menu button that EDITS the current message into the menu."""
    return [btn(t(lang, "b_menu"), "m:home")]


def menu_row_new(lang: str):
    """Menu button for chat-style messages: keeps the message (verse, prayer...)
    in the chat and sends the menu as a NEW message."""
    return [btn(t(lang, "b_menu"), "m:homenew")]


def menu_keyboard(lang: str, admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn(t(lang, "b_send"), "m:request")],
        [btn(t(lang, "b_scripture"), "m:scripture"), btn(t(lang, "b_journal"), "j:home")],
        [btn(t(lang, "b_reminders"), "m:reminders"), btn(t(lang, "b_language"), "m:language")],
        [btn(t(lang, "b_lords"), "m:lords"), btn(t(lang, "b_help"), "m:help")],
    ]
    if admin:
        rows.append([btn(t(lang, "b_admin"), "adm:home")])
    return InlineKeyboardMarkup(rows)


def help_keyboard(lang: str, new_message: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "b_contact"), url=DEVELOPER_URL)],
            menu_row_new(lang) if new_message else menu_row(lang),
        ]
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return kb([btn("🇺🇸 English", "lang:en"), btn("🇪🇹 አማርኛ", "lang:am")])


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


async def drop_buttons(query) -> bool:
    """Remove the buttons from the tapped message. False if that fails
    (for example a double tap, when the buttons are already gone)."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        return True
    except TelegramError:
        return False


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


async def deliver(context, from_chat_id: int, message_ids: list[int], category: str, code: str) -> int:
    """Send a header (in the recipient's own language, with a Block button) and
    a copy of the message(s) to every team member. copy_message does not reveal
    the sender. protect_content stops team members from forwarding or saving the
    request, so private cases stay private. One failing recipient does not stop
    the others. Returns how many got it."""
    ids = await recipient_ids()
    rows = await db(
        "fetch", "SELECT user_id, lang FROM users WHERE user_id = ANY($1::bigint[])", ids
    )
    langs = {r["user_id"]: r["lang"] for r in rows}
    now = datetime.now(EAT)
    delivered = 0
    for recipient_id in ids:
        lang = langs.get(recipient_id) or "en"
        header = t(
            lang,
            "req_header",
            code=code,
            category=CATEGORY_LABELS[lang][category],
            time=format_time(now, lang),
        )
        try:
            head = await context.bot.send_message(
                chat_id=recipient_id,
                text=header,
                reply_markup=kb([btn(t(lang, "b_block_sender"), f"blk:{code}")]),
                protect_content=True,
            )
            for mid in message_ids:
                await context.bot.copy_message(
                    chat_id=recipient_id,
                    from_chat_id=from_chat_id,
                    message_id=mid,
                    protect_content=True,
                    reply_parameters=ReplyParameters(message_id=head.message_id),
                )
            delivered += 1
        except TelegramError as e:
            logging.warning("Could not deliver to %s: %s", recipient_id, e)
    return delivered


# ---------------------------------------------------------------------------
# Start, menu, help, language
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


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help: a short description of the bot and a way to reach the developer."""
    user = await get_user(update.effective_user)
    await set_state(update.effective_user.id, None)
    lang = user["lang"] or "en"
    await update.message.reply_text(t(lang, "help"), reply_markup=help_keyboard(lang, True))


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
    uid = query.from_user.id
    await set_state(uid, None)
    lang = user["lang"] or "en"

    if action == "home":
        await show(query, t(lang, "home"), menu_keyboard(lang, await is_admin(uid)))
    elif action == "homenew":
        # The message the button was on (a verse, a prayer...) stays in the chat.
        if await drop_buttons(query):
            await context.bot.send_message(
                chat_id=uid,
                text=t(lang, "home"),
                reply_markup=menu_keyboard(lang, await is_admin(uid)),
            )
    elif action == "request":
        await show(query, t(lang, "request_prompt"), kb(menu_row(lang)))
    elif action == "scripture":
        await show(query, t(lang, "scripture_prompt"), feelings_keyboard(lang))
    elif action == "scripture2":
        # From a verse message: keep the verse, send the feelings list as a new message.
        if await drop_buttons(query):
            await context.bot.send_message(
                chat_id=uid, text=t(lang, "scripture_prompt"), reply_markup=feelings_keyboard(lang)
            )
    elif action == "language":
        await show(query, LANG_PROMPT, language_keyboard())
    elif action == "lords":
        # Arrives as a new message; the menu above stays as it is.
        await context.bot.send_message(
            chat_id=uid, text=LORDS_PRAYER_TEXT[lang], reply_markup=kb(menu_row_new(lang))
        )
    elif action == "help":
        await show(query, t(lang, "help"), help_keyboard(lang, False))
    elif action == "reminders":
        await show_reminders(query, user, lang)


# ---------------------------------------------------------------------------
# Scripture for the moment: every verse arrives as a NEW message (like a chat)
# ---------------------------------------------------------------------------
async def handle_feeling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, feeling = query.data.split(":", 1)  # "feel" (from the list) or "more"
    await query.answer()
    if feeling not in FEELING_REFS:
        return
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    if action == "more":
        # Take the buttons off the previous verse. A double tap fails here,
        # so one tap can never send two verses.
        if not await drop_buttons(query):
            return
    text = f"{FEELING_LABELS[lang][feeling]}\n\n{verse_block(lang, random.choice(FEELING_REFS[feeling]))}"
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=text,
        reply_markup=kb(
            [btn(t(lang, "b_another"), f"more:{feeling}")],
            [btn(t(lang, "b_feelings"), "m:scripture2")],
            menu_row_new(lang),
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
ALBUM_TTL_SECONDS = 3600
ALBUM_MAX = 10


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = await get_user(update.effective_user)
    lang = user["lang"] or "en"

    # Albums arrive as several separate updates. The first one drives the
    # prompts; the others are remembered silently and sent along with it.
    group = message.media_group_id
    if group:
        albums = context.bot_data.setdefault("albums", {})
        entry = albums.get(group)
        if entry is not None:
            if len(entry["ids"]) < ALBUM_MAX and message.message_id not in entry["ids"]:
                entry["ids"].append(message.message_id)
            return
        now = time.time()
        for key in [k for k, v in albums.items() if now - v["ts"] > ALBUM_TTL_SECONDS]:
            del albums[key]
        albums[group] = {"first": message.message_id, "ids": [message.message_id], "ts": now}

    if user["state"]:
        await handle_state_message(update, context, user, lang)
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
    # The cooldown is NOT reset here. Otherwise an old, unanswered prompt could
    # be cancelled right after a delivered request to skip the waiting time.
    query = update.callback_query
    await query.answer()
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
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
    if not await drop_buttons(query):
        return

    original = getattr(query.message, "reply_to_message", None)
    if original is None:
        await show(query, t(lang, "expired"), kb(menu_row(lang)))
        return

    # An album is sent as a whole (all the photos that arrived together).
    message_ids = [original.message_id]
    for entry in context.bot_data.get("albums", {}).values():
        if entry["first"] == original.message_id:
            message_ids = list(entry["ids"])
            break

    delivered = await deliver(
        context, original.chat_id, message_ids, category, user["anon_code"]
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
        rows.append(menu_row_new(lang))
        await show(query, t(lang, "sent_short"), None)
        # The thank-you and the verse arrive as a new message (replying to the
        # original so "Save to my journal" can still find it).
        thanks = t(lang, "sent", verse=random_verse(lang))
        try:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=thanks,
                reply_markup=kb(*rows),
                reply_parameters=ReplyParameters(message_id=original.message_id),
            )
        except TelegramError:
            await context.bot.send_message(
                chat_id=query.from_user.id, text=thanks, reply_markup=kb(menu_row_new(lang))
            )
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
            reply_markup=kb([btn(t(lang, "b_open_journal"), "j:home")], menu_row_new(lang))
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
async def handle_state_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user, lang: str):
    message = update.message
    uid = update.effective_user.id
    state = user["state"]

    # An announcement can be any kind of message, so check it before "text only".
    if state == "admin_broadcast" and is_owner(uid):
        await set_state(uid, None)
        everyone = len(await audience_rows("all"))
        subs = len(await audience_rows("subs"))
        # Preview: the owner sees the message exactly as people will receive it.
        try:
            await context.bot.send_message(chat_id=uid, text=t(ADMIN_LANG, "bc_preview"))
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
                reply_markup=kb(menu_row_new(lang)),
            )
        except TelegramError as e:
            logging.warning("Announcement preview failed: %s", e)
        await message.reply_text(
            t(ADMIN_LANG, "bc_choose"),
            reply_markup=kb(
                [btn(t(ADMIN_LANG, "b_bc_all", n=everyone), "bc:pick:all")],
                [btn(t(ADMIN_LANG, "b_bc_subs", n=subs), "bc:pick:subs")],
                [btn(t(ADMIN_LANG, "b_cancel"), "bc:cancel")],
            ),
            reply_parameters=ReplyParameters(message_id=message.message_id),
        )
        return

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
        await message.reply_text(await add_admin(uid, message.text, ADMIN_LANG))

    elif state == "admin_block" and await is_admin(uid):
        await set_state(uid, None)
        _, text = await set_blocked_by_code(message.text, True, ADMIN_LANG)
        await message.reply_text(text)

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
# Admin: owner (ultimate admin) and admins. The admin panel is always English
# (prayer requests and reminders still arrive in each person's own language).
#   admins: block senders, quit the admin role
#   owner : everything above + stats, unblock, add/remove admins, announcements,
#           and the only person who can see who the other admins are
# ---------------------------------------------------------------------------
STATS_RULE = "━━━━━━━━━━━━━━━━━━━━"
KIND_ICONS = {
    "text": "💬", "photo": "🖼", "voice": "🎤", "video": "🎬", "video_note": "⭕",
    "audio": "🎵", "animation": "🎞", "document": "📎",
}  # fmt: skip
CATEGORY_PLAIN = {
    "health": "Health", "family": "Family", "school": "School / Exams",
    "work": "Work / Money", "spiritual": "Spiritual", "other": "Other",
}  # fmt: skip


def _bar(n: int, total: int, width: int = 10) -> str:
    filled = round(width * n / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total)}%" if total else "0%"


async def stats_data() -> dict:
    """All numbers for the owner dashboard (used by the picture and the text)."""
    now = datetime.now(EAT)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = start_today - timedelta(days=6)

    req = await db(
        "fetchrow",
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE created_at >= $1) AS today,
               count(*) FILTER (WHERE created_at >= now() - interval '30 days') AS month
        FROM requests
        """,
        start_today,
    )
    day_rows = await db(
        "fetch",
        "SELECT (created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS d, count(*) AS n "
        "FROM requests WHERE created_at >= $1 GROUP BY d",
        week_start,
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
               count(*) FILTER (WHERE created_at >= $1) AS new_today,
               count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS new_week,
               count(*) FILTER (WHERE lang = 'am') AS amharic,
               count(*) FILTER (WHERE lang = 'en') AS english,
               count(*) FILTER (WHERE lang IS NULL) AS no_lang,
               count(*) FILTER (WHERE subscribed) AS subscribed,
               count(*) FILTER (WHERE weekly) AS weekly,
               count(*) FILTER (WHERE blocked) AS blocked
        FROM users
        """,
        start_today,
    )
    per_day = {r["d"]: r["n"] for r in day_rows}
    days = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date()
        days.append((d, per_day.get(d, 0)))
    return {
        "now": now,
        "req": dict(req),
        "days": days,
        "week_total": sum(n for _, n in days),
        "peak": max((n for _, n in days), default=0),
        "cats": [(r["category"], r["n"]) for r in by_category],
        "kinds": [(r["kind"], r["n"]) for r in by_kind],
        "users": dict(users),
    }


def format_stats_text(d: dict) -> str:
    """Text version of the dashboard (bars made of block characters)."""
    now, req, users = d["now"], d["req"], d["users"]
    days, week_total, peak = d["days"], d["week_total"], d["peak"]
    lines = [
        "📊 PRAYER BOT DASHBOARD",
        f"🕒 {now.strftime('%a %d %b, %H:%M')} EAT",
        STATS_RULE,
        "",
        "🙏 PRAYER REQUESTS",
        f"   Today          {req['today']}",
        f"   Last 7 days    {week_total}  (avg {week_total / 7:.1f}/day)",
        f"   Last 30 days   {req['month']}",
        f"   All time       {req['total']}",
        "",
        "📈 LAST 7 DAYS",
    ]
    for day, n in days:
        marker = " ◀ today" if day == now.date() else ""
        lines.append(f"   {day.strftime('%a %d')}  {_bar(n, peak)}  {n}{marker}")

    lines += ["", STATS_RULE, "", "📂 BY CATEGORY"]
    cat_total = sum(n for _, n in d["cats"])
    if d["cats"]:
        for cat, n in d["cats"]:
            lines.append(
                f"   {CATEGORY_LABELS['en'][cat]}\n"
                f"   {_bar(n, cat_total)}  {_pct(n, cat_total)} · {n}"
            )
    else:
        lines.append("   nothing yet")

    lines += ["", "📎 BY TYPE"]
    if d["kinds"]:
        lines.append(
            "   " + "  ".join(f"{KIND_ICONS.get(k, '•')} {k} {n}" for k, n in d["kinds"])
        )
    else:
        lines.append("   nothing yet")

    total = users["total"]
    lines += [
        "",
        STATS_RULE,
        "",
        "👥 USERS",
        f"   Total {total}   🆕 +{users['new_today']} today · +{users['new_week']} this week",
        f"   🇪🇹 Amharic   {_bar(users['amharic'], total)}  {_pct(users['amharic'], total)} · {users['amharic']}",
        f"   🇺🇸 English   {_bar(users['english'], total)}  {_pct(users['english'], total)} · {users['english']}",
    ]
    if users["no_lang"]:
        lines.append(f"   ❔ No language yet: {users['no_lang']}")
    lines.append(f"   🚫 Blocked: {users['blocked']}")
    lines += [
        "",
        "🔔 SUBSCRIPTIONS",
        f"   🌅 Daily verse   {_bar(users['subscribed'], total)}  {_pct(users['subscribed'], total)} · {users['subscribed']}",
        f"   📅 Weekly        {_bar(users['weekly'], total)}  {_pct(users['weekly'], total)} · {users['weekly']}",
    ]
    return "\n".join(lines)


def render_stats_png(d: dict) -> bytes:
    """Draw the dashboard as a PNG picture (needs Pillow >= 10.1; no system
    fonts or emoji are used, so it looks the same on any server)."""
    import io
    from PIL import Image, ImageDraw, ImageFont

    S, W = 3, 760  # drawn at 3x, then shrunk to 1.5x for smooth edges
    BG, CARD, TRACK = (23, 33, 43), (31, 45, 61), (36, 52, 71)
    TEXT, MUTED, SOFT, WHITE = (232, 238, 245), (127, 147, 168), (159, 179, 200), (255, 255, 255)
    BLUE, BLUE_D, PURPLE, PURPLE_L = (94, 181, 247), (59, 130, 246), (139, 92, 246), (192, 132, 252)
    GREEN, RED = (74, 222, 128), (248, 113, 113)
    fonts: dict = {}

    def font(px: int):
        if px not in fonts:
            fonts[px] = ImageFont.load_default(size=px * S)
        return fonts[px]

    class Null:  # measuring pass: same calls, nothing drawn
        def __getattr__(self, name):
            return lambda *a, **k: None

    now, req, users = d["now"], d["req"], d["users"]
    days, peak, week_total = d["days"], d["peak"], d["week_total"]

    def layout(draw, img):
        def tx(x, y, s, size=17, fill=TEXT, anchor="ls", bold=False):
            draw.text(
                (x * S, y * S), s, font=font(size), fill=fill, anchor=anchor,
                stroke_width=1 if bold else 0, stroke_fill=fill,
            )

        def rrect(x, y, w, h, r, fill):
            draw.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=r * S, fill=fill)

        def gradient(x, y, w, h, c1, c2):
            if img is None:
                return
            bw, bh = max(int(w * S), 2), max(int(h * S), 2)
            grad = Image.new("RGB", (bw, bh))
            gd = ImageDraw.Draw(grad)
            for i in range(bw):
                t_ = i / max(bw - 1, 1)
                gd.line([(i, 0), (i, bh)], fill=tuple(int(c1[k] + (c2[k] - c1[k]) * t_) for k in range(3)))
            mask = Image.new("L", (bw, bh), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=bh // 2, fill=255)
            img.paste(grad, (int(x * S), int(y * S)), mask)

        def hbar(x, y, w, h, n, total, c1=BLUE_D, c2=BLUE):
            rrect(x, y, w, h, h / 2, TRACK)
            if total and n > 0:
                gradient(x, y, max(w * n / total, h), h, c1, c2)

        def section(y, title):
            tx(40, y, title, 15, MUTED, bold=True)
            draw.line([40 * S, (y + 10) * S, (W - 40) * S, (y + 10) * S], fill=TRACK, width=S)

        def labeled_bar(y, label, n, total, c1=BLUE_D, c2=BLUE):
            tx(40, y, label, 17)
            pct = round(100 * n / total) if total else 0
            tx(W - 40, y, f"{pct}%   {n}", 16, SOFT, anchor="rs")
            hbar(40, y + 10, W - 80, 12, n, total, c1, c2)

        # header
        tx(40, 62, "Prayer Bot Dashboard", 30, WHITE, bold=True)
        tx(40, 92, now.strftime("%a %d %b %Y  -  %H:%M EAT"), 16, MUTED)
        # tiles
        tiles = [("Today", req["today"]), ("Last 7 days", week_total), ("Last 30 days", req["month"]), ("All time", req["total"])]
        tw = (W - 80 - 3 * 14) / 4
        for i, (label, value) in enumerate(tiles):
            x = 40 + i * (tw + 14)
            rrect(x, 116, tw, 92, 16, CARD)
            tx(x + 16, 148, label, 14, MUTED)
            tx(x + 16, 190, f"{value:,}", 34, BLUE if i == 0 else WHITE, bold=True)
        tx(40, 236, f"Average {week_total / 7:.1f} prayer requests per day this week", 15, MUTED)

        # 7-day chart
        section(278, "LAST 7 DAYS")
        cw, base, maxh = (W - 120) / 7, 470, 130
        for i, (day, n) in enumerate(days):
            x, w = 60 + i * cw + cw * 0.18, cw * 0.64
            is_today = day == now.date()
            rrect(x, base - maxh, w, maxh, 8, CARD)
            if n and peak:
                h = max(maxh * n / peak, 10)
                rrect(x, base - h, w, h, 8, BLUE if is_today else BLUE_D)
            tx(x + w / 2, base - maxh - 8, str(n), 15, WHITE, anchor="ms", bold=True)
            tx(x + w / 2, base + 24, day.strftime("%a"), 14, BLUE if is_today else MUTED, anchor="ms", bold=is_today)

        # categories
        section(540, "BY CATEGORY")
        y = 574
        cat_total = sum(n for _, n in d["cats"])
        if d["cats"]:
            for cat, n in d["cats"]:
                labeled_bar(y, CATEGORY_PLAIN.get(cat, cat), n, cat_total)
                y += 52
        else:
            tx(40, y, "Nothing yet", 17, MUTED)
            y += 52

        # types (wraps after 4 per row)
        section(y + 10, "BY TYPE")
        ty = y + 46
        if d["kinds"]:
            for i in range(0, len(d["kinds"]), 4):
                row = d["kinds"][i : i + 4]
                tx(40, ty, "      ".join(f"{k} {n}" for k, n in row), 17)
                ty += 28
        else:
            tx(40, ty, "Nothing yet", 17, MUTED)
            ty += 28
        y = ty + 22

        # users
        total = users["total"]
        section(y, "USERS")
        tx(40, y + 38, f"Total {total:,}", 20, WHITE, bold=True)
        tx(W - 40, y + 38, f"+{users['new_today']} today    +{users['new_week']} this week", 16, GREEN, anchor="rs")
        y2 = y + 78
        labeled_bar(y2, "Amharic", users["amharic"], total)
        labeled_bar(y2 + 50, "English", users["english"], total, PURPLE, PURPLE_L)
        y2 += 100
        note = f"No language yet: {users['no_lang']}      Blocked: {users['blocked']}"
        tx(40, y2 + 4, note, 15, RED if users["blocked"] else MUTED)
        y = y2 + 44

        # subscriptions
        section(y, "SUBSCRIPTIONS")
        y2 = y + 42
        labeled_bar(y2, "Daily verse (6:30 AM)", users["subscribed"], total)
        labeled_bar(y2 + 50, "Weekly reflection", users["weekly"], total, PURPLE, PURPLE_L)
        return y2 + 50 + 40

    height = layout(Null(), None)
    img = Image.new("RGB", (W * S, height * S), BG)
    layout(ImageDraw.Draw(img), img)
    img = img.resize((W * S // 2, height * S // 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# SVG dashboard. The DESIGN lives in stats_template.svg (open it in a browser or
# Inkscape to restyle it); the numbers are filled in here. Each chart below draws
# inside its card with coordinates relative to the card's top-left corner, so if
# you resize a card in the template, adjust the matching function.
# ---------------------------------------------------------------------------
SVG_TEMPLATE_PATH = Path(__file__).with_name("stats_template.svg")
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
TYPE_ORDER = [
    ("text", "Text"), ("photo", "Photo"), ("voice", "Voice"), ("video", "Video"),
    ("video_note", "Round"), ("audio", "Audio"), ("animation", "GIF"), ("document", "File"),
]  # fmt: skip
TYPE_COLORS = ["#60C3FA", "#C77DFF", "#5EEAA0", "#FDD35C", "#FB8BA0", "#5EEAD4", "#FDBA74", "#A9B8CC"]
CATEGORY_GRADIENTS = {
    "health": "gRose", "family": "gAmber", "school": "gBlue",
    "work": "gGreen", "spiritual": "gPurple", "other": "gGray",
}  # fmt: skip
SVG_TRACK = "#0E182A"


def _x(value) -> str:
    return xml_escape(str(value))


def _fit(text: str, big: int) -> int:
    """Font size that keeps a number inside its tile: long numbers get smaller."""
    n = len(text)
    return big if n <= 5 else round(big * 0.82) if n <= 7 else round(big * 0.68) if n <= 9 else round(big * 0.54)


def _compact_int(n: int) -> str:
    """1,234 stays as it is; 12,345 becomes 12.3K; 1,200,000 becomes 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return f"{n:,}"


def _compact_avg(x: float) -> str:
    return _compact_int(round(x)) if x >= 1000 else f"{x:.1f}"


def _pct_of(n: int, total: int) -> int:
    return round(100 * n / total) if total else 0


def _svg_week_chart(d: dict) -> str:
    """Card 984 x 400: one bar per day, today highlighted, dashed average line."""
    days, peak, today = d["days"], d["peak"], d["now"].date()
    avg = d["week_total"] / 7
    pad, col_w, bar_w, base, max_h = 44, 128, 64, 310, 210
    out = [
        f'<text x="{pad}" y="56" font-size="22" font-weight="600" fill="#C9D6E5">Prayer requests per day</text>',
        '<line x1="672" y1="50" x2="712" y2="50" stroke="#FBBF24" stroke-width="3" stroke-dasharray="6 6" stroke-linecap="round"/>',
        f'<text x="724" y="57" font-size="19" fill="#FDD35C">avg {_compact_avg(avg)} / day</text>',
    ]
    for i, (day, n) in enumerate(days):
        cx = pad + col_w * i + col_w / 2
        is_today = day == today
        out.append(
            f'<rect x="{cx - bar_w / 2:.1f}" y="{base - max_h}" width="{bar_w}" height="{max_h}" rx="20" fill="{SVG_TRACK}"/>'
        )
        if n and peak:
            h = max(max_h * n / peak, 16)
            fill = "url(#barToday)" if is_today else "url(#barBlue)"
            glow = ' filter="url(#glow)"' if is_today else ""
            out.append(
                f'<rect x="{cx - bar_w / 2:.1f}" y="{base - h:.1f}" width="{bar_w}" height="{h:.1f}" '
                f'rx="{min(20, h / 2):.1f}" fill="{fill}"{glow}/>'
            )
            out.append(
                f'<text x="{cx:.1f}" y="{base - h - 14:.1f}" font-size="24" font-weight="700" text-anchor="middle" '
                f'fill="{"#7DD3FC" if is_today else "#FFFFFF"}">{_compact_int(n)}</text>'
            )
        else:
            out.append(
                f'<text x="{cx:.1f}" y="{base - 14}" font-size="22" text-anchor="middle" fill="#55687F">0</text>'
            )
        out.append(
            f'<text x="{cx:.1f}" y="{base + 40}" font-size="23" font-weight="{700 if is_today else 500}" '
            f'text-anchor="middle" fill="{"#7DD3FC" if is_today else "#9FB3C8"}">{_x(day.strftime("%a"))}</text>'
        )
        out.append(
            f'<text x="{cx:.1f}" y="{base + 66}" font-size="18" text-anchor="middle" fill="#6B7F96">{_x(day.strftime("%d"))}</text>'
        )
    if peak:
        y_avg = base - max_h * min(avg / peak, 1)
        out.append(
            f'<line x1="{pad}" y1="{y_avg:.1f}" x2="{984 - pad}" y2="{y_avg:.1f}" stroke="#FBBF24" '
            'stroke-opacity="0.7" stroke-width="2" stroke-dasharray="8 8" stroke-linecap="round"/>'
        )
    else:
        out.append(
            '<text x="492" y="200" font-size="24" text-anchor="middle" fill="#6B7F96">No requests in the last 7 days</text>'
        )
    return "\n    ".join(out)


def _svg_category_rows(d: dict) -> str:
    """Card 588 x 520: all six categories, busiest first, with gradient bars."""
    counts = dict(d["cats"])
    total = sum(counts.values())
    order = sorted(CATEGORIES, key=lambda c: (-counts.get(c, 0), CATEGORIES.index(c)))
    left, width = 36, 516
    out = []
    for i, cat in enumerate(order):
        n = counts.get(cat, 0)
        y = 66 + i * 78
        dim = n == 0
        out.append(
            f'<text x="{left}" y="{y}" font-size="25" font-weight="600" fill="{"#6B7F96" if dim else "#E8EEF5"}">{_x(CATEGORY_PLAIN[cat])}</text>'
        )
        out.append(
            f'<text x="{left + width}" y="{y}" font-size="22" text-anchor="end" fill="#9FB3C8">'
            f'<tspan font-weight="700" fill="{"#6B7F96" if dim else "#FFFFFF"}">{_pct_of(n, total)}%</tspan>  ·  {n:,}</text>'
        )
        out.append(f'<rect x="{left}" y="{y + 18}" width="{width}" height="16" rx="8" fill="{SVG_TRACK}"/>')
        if n:
            w = max(width * n / total, 16)
            out.append(
                f'<rect x="{left}" y="{y + 18}" width="{w:.1f}" height="16" rx="8" fill="url(#{CATEGORY_GRADIENTS[cat]})"/>'
            )
    return "\n    ".join(out)


def _svg_users_card(d: dict) -> str:
    """Card 364 x 520: language donut, legend, new-user chips, blocked count."""
    u = d["users"]
    total = u["total"]
    cx, cy, r, stroke = 182, 150, 88, 28
    circ = 2 * math.pi * r
    segments = [
        ("Amharic", u["amharic"], "gBlue", "#60C3FA"),
        ("English", u["english"], "gPurple", "#C77DFF"),
        ("No language yet", u["no_lang"], "gGray", "#A9B8CC"),
    ]
    out = [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{SVG_TRACK}" stroke-width="{stroke}"/>']
    present = [s for s in segments if s[1] > 0]
    gap = 7 if len(present) > 1 else 0
    offset = 0.0
    for _, n, grad, _color in present:
        length = circ * n / total
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="url(#{grad})" stroke-width="{stroke}" '
            f'stroke-dasharray="{max(length - gap, 0.5):.2f} {circ:.2f}" stroke-dashoffset="{-(offset + gap / 2):.2f}" '
            f'transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += length
    total_txt = f"{total:,}"
    out.append(
        f'<text x="{cx}" y="{cy + 8}" font-size="{_fit(total_txt, 42)}" font-weight="800" text-anchor="middle" fill="#FFFFFF">{total_txt}</text>'
    )
    out.append(f'<text x="{cx}" y="{cy + 38}" font-size="19" text-anchor="middle" fill="#8CA0B8">users</text>')
    for i, (label, n, _grad, color) in enumerate(segments):
        y = 316 + i * 38
        out.append(f'<circle cx="44" cy="{y - 7}" r="8" fill="{color}" fill-opacity="{1 if n else 0.35}"/>')
        out.append(f'<text x="64" y="{y}" font-size="21" fill="{"#C9D6E5" if n else "#6B7F96"}">{_x(label)}</text>')
        out.append(
            f'<text x="328" y="{y}" font-size="20" text-anchor="end" fill="#9FB3C8">{_pct_of(n, total)}%  ·  {n:,}</text>'
        )
    for x, w, label in ((36, 130, f"+{u['new_today']:,} today"), (178, 150, f"+{u['new_week']:,} / week")):
        out.append(
            f'<rect x="{x}" y="424" width="{w}" height="44" rx="22" fill="#22C55E" fill-opacity="0.13" '
            'stroke="#22C55E" stroke-opacity="0.4" stroke-width="1.5"/>'
        )
        out.append(
            f'<text x="{x + w / 2}" y="453" font-size="19" font-weight="600" text-anchor="middle" fill="#BBF7D0">{_x(label)}</text>'
        )
    blocked = u["blocked"]
    out.append(
        f'<text x="36" y="498" font-size="19" fill="{"#F87171" if blocked else "#6B7F96"}">Blocked senders: {blocked:,}</text>'
    )
    return "\n    ".join(out)


def _svg_type_chips(d: dict) -> str:
    """Card 588 x 220: eight message-type chips (4 per row)."""
    counts = dict(d["kinds"])
    out = []
    for i, (kind, name) in enumerate(TYPE_ORDER):
        x, y = 32 + (i % 4) * 135, 34 + (i // 4) * 88
        n = counts.get(kind, 0)
        count_txt = f"{n:,}"
        out.append(
            f'<rect x="{x}" y="{y}" width="119" height="72" rx="18" fill="{SVG_TRACK}" stroke="#22334F" stroke-width="1.5"/>'
        )
        out.append(
            f'<circle cx="{x + 22}" cy="{y + 24}" r="6" fill="{TYPE_COLORS[i]}" fill-opacity="{1 if n else 0.35}"/>'
        )
        out.append(f'<text x="{x + 38}" y="{y + 29}" font-size="17" fill="#9FB3C8">{_x(name)}</text>')
        out.append(
            f'<text x="{x + 16}" y="{y + 60}" font-size="{_fit(count_txt, 28)}" font-weight="800" '
            f'fill="{"#FFFFFF" if n else "#55687F"}">{count_txt}</text>'
        )
    return "\n    ".join(out)


def _svg_sub_bars(d: dict) -> str:
    """Card 364 x 220: how many users opted in to each reminder."""
    u = d["users"]
    total = u["total"]
    out = []
    for i, (label, n, grad) in enumerate(
        (("Daily verse", u["subscribed"], "gBlue"), ("Weekly reflection", u["weekly"], "gPurple"))
    ):
        y = 62 + i * 78
        out.append(f'<text x="32" y="{y}" font-size="21" fill="#E8EEF5">{_x(label)}</text>')
        out.append(
            f'<text x="332" y="{y}" font-size="19" text-anchor="end" fill="#9FB3C8">{_pct_of(n, total)}%  ·  {n:,}</text>'
        )
        out.append(f'<rect x="32" y="{y + 16}" width="300" height="14" rx="7" fill="{SVG_TRACK}"/>')
        if n:
            out.append(
                f'<rect x="32" y="{y + 16}" width="{max(300 * n / total, 14):.1f}" height="14" rx="7" fill="url(#{grad})"/>'
            )
    return "\n    ".join(out)


def render_stats_svg(d: dict) -> bytes:
    """Fill stats_template.svg with the numbers. Raises (so the caller can fall
    back to the picture or text) if the template is missing, uses a placeholder
    this code doesn't know, or the result isn't valid XML."""
    import xml.etree.ElementTree as ET

    template = SVG_TEMPLATE_PATH.read_text(encoding="utf-8")
    now, req, days = d["now"], d["req"], d["days"]

    def kpi(prefix: str, number: int, sub: str) -> dict:
        txt = f"{number:,}"
        return {f"{prefix}": txt, f"{prefix}_FS": str(_fit(txt, 56)), f"{prefix}_SUB": _x(sub)}

    yesterday = days[-2][1] if len(days) >= 2 else 0
    diff = req["today"] - yesterday
    if diff == 0:
        today_sub = "same as yesterday"
    else:
        today_sub = f"{'▲ +' if diff > 0 else '▼ -'}{_compact_int(abs(diff))} vs yesterday"
        if len(today_sub) > 20:  # keep it inside the tile
            today_sub = today_sub.replace("yesterday", "yday")
    values = {
        "DATE": _x(now.strftime("%a %d %b %Y")),
        "TIME": _x(now.strftime("%H:%M") + " EAT"),
        **kpi("K_TODAY", req["today"], today_sub),
        **kpi("K_WEEK", d["week_total"], f"avg {_compact_avg(d['week_total'] / 7)} per day"),
        **kpi("K_MONTH", req["month"], f"avg {_compact_avg(req['month'] / 30)} per day"),
        **kpi("K_ALL", req["total"], "since launch"),
        "WEEK_CHART": _svg_week_chart(d),
        "CATEGORY_ROWS": _svg_category_rows(d),
        "USERS_CARD": _svg_users_card(d),
        "TYPE_CHIPS": _svg_type_chips(d),
        "SUB_BARS": _svg_sub_bars(d),
    }
    svg = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)  # unknown name -> KeyError
    ET.fromstring(svg.encode("utf-8"))  # must be well-formed XML
    return svg.encode("utf-8")


STATS_MODES = ("png", "svg", "text")
STATS_VIEW_LABEL = {"png": "b_image_view", "svg": "b_svg_view", "text": "b_text_view"}


def stats_keyboard(mode: str) -> InlineKeyboardMarkup:
    """Refresh the view you are on, or switch to one of the other two."""
    row = [btn(t(ADMIN_LANG, "b_refresh"), f"adm:sv:{mode}")] + [
        btn(t(ADMIN_LANG, STATS_VIEW_LABEL[m]), f"adm:sv:{m}") for m in STATS_MODES if m != mode
    ]
    return kb(row, [btn(t(ADMIN_LANG, "b_panel"), "adm:homenew")])


async def send_stats(bot, chat_id: int, mode: str = "png"):
    """Send the dashboard as a picture (png), a designed SVG file (svg) or text.
    If the chosen view can't be made, fall back: svg -> picture -> text."""
    d = await stats_data()
    when = d["now"].strftime("%a %d %b, %H:%M")
    if mode == "svg":
        try:
            svg = await asyncio.to_thread(render_stats_svg, d)
            await bot.send_document(
                chat_id=chat_id,
                document=svg,
                filename=f"prayer-dashboard-{d['now'].strftime('%Y-%m-%d_%H%M')}.svg",
                caption=f"🎨 Dashboard (SVG) · {when} EAT\nOpen the file in a browser or your gallery to see the design.",
                reply_markup=stats_keyboard("svg"),
            )
            return
        except Exception:
            logging.exception("Could not make the SVG dashboard; trying the picture instead")
            mode = "png"
    if mode == "png":
        try:
            png = await asyncio.to_thread(render_stats_png, d)
            await bot.send_photo(
                chat_id=chat_id,
                photo=png,
                caption=f"📊 Dashboard · {when} EAT",
                reply_markup=stats_keyboard("png"),
            )
            return
        except Exception:
            logging.exception("Could not make the stats picture; sending text instead")
    await bot.send_message(
        chat_id=chat_id, text=format_stats_text(d), reply_markup=stats_keyboard("text")
    )


async def set_blocked_by_code(raw: str, blocked: bool, lang: str) -> tuple[bool, str]:
    """Returns (done, message). Codes are 12 characters (older ones 8)."""
    code = raw.strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{8,12}", code):
        return False, t(lang, "bad_code")
    result = await db(
        "execute", "UPDATE users SET blocked = $2 WHERE anon_code = $1", code, blocked
    )
    if int(result.split()[-1]):
        return True, t(lang, "blocked_ok" if blocked else "unblocked_ok", code=code)
    return False, t(lang, "code_missing", code=code)


async def add_admin(owner_id: int, raw: str, lang: str) -> str:
    """The person always gets an Accept / Decline invitation. If they already
    started the bot it is sent right now; otherwise a pending row is made that
    expires after ADMIN_PENDING_HOURS and the invitation goes out when they
    first send /start."""
    username = parse_username(raw)
    if username is None:
        return t(lang, "add_bad")
    await purge_expired_admins()
    known = await db(
        "fetchrow",
        "SELECT user_id, lang FROM users WHERE uname_hash = $1 ORDER BY created_at DESC LIMIT 1",
        uname_hash(username),
    )
    row = await db(
        "fetchrow",
        "INSERT INTO admins (username, user_id, added_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (username) DO NOTHING RETURNING id",
        username,
        known["user_id"] if known else None,
        owner_id,
    )
    if row is None:
        return t(lang, "add_dup", u=username)
    if known:
        await _send_admin_invite(known["user_id"], known["lang"] or "en")
        return t(lang, "add_active", u=username)
    return t(lang, "add_pending", u=username)


def can_quit_admin(uid: int) -> bool:
    """Admins added from the bot can quit. The owner and the team set in the
    server settings (MY_USER_ID) are managed outside the bot."""
    return not is_owner(uid) and uid not in RECIPIENT_IDS


def admin_home(uid: int, lang: str):
    first = []
    if is_owner(uid):
        first.append(btn(t(lang, "b_stats"), "adm:stats"))  # stats: owner only
    first.append(btn(t(lang, "b_blocked"), "adm:blocked"))
    second = [btn(t(lang, "b_block_code"), "adm:block")]
    rows = [first, second]
    if is_owner(uid):
        second.append(btn(t(lang, "b_admins"), "adm:admins"))
        rows.append([btn(t(lang, "b_broadcast"), "adm:bc")])
    elif can_quit_admin(uid):
        rows.append([btn(t(lang, "b_quit"), "adm:quit")])
    rows.append(menu_row(lang))
    role = t(lang, "role_owner" if is_owner(uid) else "role_admin")
    return t(lang, "adm_panel", role=role), InlineKeyboardMarkup(rows)


async def render_blocked(query, uid: int, lang: str):
    back = [btn(t(lang, "b_back"), "adm:home")]
    rows = await db("fetch", "SELECT anon_code FROM users WHERE blocked ORDER BY anon_code LIMIT 40")
    if not rows:
        await show(query, t(lang, "no_blocked"), kb(back))
        return
    buttons = (
        [[btn(t(lang, "b_unblock", code=r["anon_code"]), f"adm:unb:{r['anon_code']}")] for r in rows]
        if is_owner(uid)
        else []
    )
    text = t(lang, "blocked_list", items="\n".join(f"#{r['anon_code']}" for r in rows))
    if not is_owner(uid):
        text += t(lang, "blocked_note")
    await show(query, text, InlineKeyboardMarkup(buttons + [back]))


async def render_admins(query, uid: int, lang: str):
    """Owner only. Other admins never see who else is on the team, or how many."""
    await purge_expired_admins()
    back = [btn(t(lang, "b_back"), "adm:home")]
    rows = await db(
        "fetch",
        "SELECT id, username, user_id, accepted_at, "
        "GREATEST(1, CEIL(EXTRACT(EPOCH FROM (created_at + make_interval(hours => $1) - now())) / 3600))::int AS hours_left "
        "FROM admins ORDER BY id",
        ADMIN_PENDING_HOURS,
    )
    lines = [t(lang, "admins_head", n=len(RECIPIENT_IDS))]
    buttons = []
    for r in rows:
        if r["accepted_at"]:
            status = t(lang, "status_active")
        elif r["user_id"]:
            status = t(lang, "status_invited", h=r["hours_left"])
        else:
            status = t(lang, "status_pending", h=r["hours_left"])
        lines.append(f"• @{r['username']} — {status}")
        buttons.append([btn(t(lang, "b_remove_admin", u=r["username"]), f"adm:rm:{r['id']}")])
    buttons.append([btn(t(lang, "b_add_admin"), "adm:add")])
    await show(query, "\n".join(lines), InlineKeyboardMarkup(buttons + [back]))


async def handle_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    uid = query.from_user.id
    lang = ADMIN_LANG
    if not await is_admin(uid):
        await query.answer(t(lang, "admins_only"), show_alert=True)
        return
    await query.answer()
    await get_user(query.from_user)
    back = [btn(t(lang, "b_back"), "adm:home")]
    cancel_home = kb([btn(t(lang, "b_cancel"), "adm:home")])

    async def owner_only() -> bool:
        if is_owner(uid):
            return True
        await query.answer(t(lang, "owner_only"), show_alert=True)
        return False

    if action == "home":
        await set_state(uid, None)
        text, markup = admin_home(uid, lang)
        await show(query, text, markup)

    elif action in ("stats", "sv", "statsr", "statstext"):
        if not await owner_only():
            return
        # "statsr" and "statstext" are the buttons of dashboards sent by the
        # previous version; they keep working.
        mode = {"stats": "png", "statsr": "png", "statstext": "text"}.get(action) or (
            parts[2] if len(parts) > 2 else "png"
        )
        if mode not in STATS_MODES:
            return
        # From a stats message: take its buttons off first, so a double tap
        # can't send two copies. The dashboard arrives as a new message.
        if action != "stats" and not await drop_buttons(query):
            return
        await send_stats(context.bot, uid, mode)

    elif action == "homenew":
        if await drop_buttons(query):
            text, markup = admin_home(uid, lang)
            await context.bot.send_message(chat_id=uid, text=text, reply_markup=markup)

    elif action == "blocked":
        await render_blocked(query, uid, lang)

    elif action == "unb":
        if not await owner_only():
            return
        await db("execute", "UPDATE users SET blocked = FALSE WHERE anon_code = $1", parts[2])
        await render_blocked(query, uid, lang)

    elif action == "block":
        await set_state(uid, "admin_block")
        await show(query, t(lang, "block_prompt"), cancel_home)

    elif action == "admins":
        if not await owner_only():
            return
        await render_admins(query, uid, lang)

    elif action == "add":
        if not await owner_only():
            return
        await set_state(uid, "admin_add")
        await show(query, t(lang, "admin_add_prompt"), cancel_home)

    elif action == "rm":
        if not await owner_only():
            return
        await db("execute", "DELETE FROM admins WHERE id = $1", int(parts[2]))
        await render_admins(query, uid, lang)

    elif action == "bc":
        if not await owner_only():
            return
        await set_state(uid, "admin_broadcast")
        await show(query, t(lang, "bc_prompt"), cancel_home)

    elif action == "quit":
        if not can_quit_admin(uid):
            await query.answer(t(lang, "quit_server"), show_alert=True)
            return
        await show(
            query,
            t(lang, "quit_confirm"),
            kb([btn(t(lang, "b_quit_yes"), "adm:quityes")], [btn(t(lang, "b_cancel"), "adm:home")]),
        )

    elif action == "quityes":
        if not can_quit_admin(uid):
            await query.answer(t(lang, "quit_server"), show_alert=True)
            return
        row = await db(
            "fetchrow",
            "DELETE FROM admins WHERE user_id = $1 AND accepted_at IS NOT NULL RETURNING username",
            uid,
        )
        await show(query, t(lang, "quit_done"), kb(menu_row(lang)))
        if row:
            await _notify_owner(t(ADMIN_LANG, "owner_quit", u=row["username"]))


async def handle_admin_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept / Decline on an admin invitation. The owner is told either way."""
    query = update.callback_query
    answer = query.data.split(":", 1)[1]
    await query.answer()
    user = await get_user(query.from_user)
    lang = user["lang"] or "en"
    uid = query.from_user.id

    if answer == "yes":
        row = await db(
            "fetchrow",
            "UPDATE admins SET accepted_at = now() "
            "WHERE user_id = $1 AND accepted_at IS NULL "
            "AND created_at > now() - make_interval(hours => $2) RETURNING username",
            uid,
            ADMIN_PENDING_HOURS,
        )
        if row is None:
            await show(query, t(lang, "invite_gone"), kb(menu_row(lang)))
            return
        await show(query, t(lang, "admin_now"), kb(menu_row(lang)))
        await _notify_owner(t(ADMIN_LANG, "owner_accepted", u=row["username"]))
    elif answer == "no":
        row = await db(
            "fetchrow",
            "DELETE FROM admins WHERE user_id = $1 AND accepted_at IS NULL RETURNING username",
            uid,
        )
        if row is None:
            await show(query, t(lang, "invite_gone"), kb(menu_row(lang)))
            return
        await show(query, t(lang, "admin_declined"), kb(menu_row(lang)))
        await _notify_owner(t(ADMIN_LANG, "owner_declined", u=row["username"]))


async def handle_block_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'🚫 Block sender' under a request header."""
    query = update.callback_query
    uid = query.from_user.id
    if not await is_admin(uid):
        await query.answer(t(ADMIN_LANG, "admins_only"), show_alert=True)
        return
    done, text = await set_blocked_by_code(query.data.split(":", 1)[1], True, ADMIN_LANG)
    await query.answer(text, show_alert=True)
    if done:
        await drop_buttons(query)


# ---------------------------------------------------------------------------
# Announcements to everyone (owner only)
# ---------------------------------------------------------------------------
async def audience_rows(which: str):
    """Who gets an announcement: all users, or only daily-verse subscribers.
    Blocked users never receive it."""
    sql = "SELECT user_id, lang FROM users WHERE NOT blocked"
    if which == "subs":
        sql += " AND subscribed"
    return await db("fetch", sql)


async def run_broadcast(bot, from_chat_id: int, message_id: int, rows, owner_id: int, owner_lang: str):
    """Runs in the background so the bot keeps answering everyone else."""
    ok = failed = 0
    try:
        for row in rows:
            lang = row["lang"] or "en"
            for attempt in (1, 2):
                try:
                    await bot.copy_message(
                        chat_id=row["user_id"],
                        from_chat_id=from_chat_id,
                        message_id=message_id,
                        reply_markup=kb(menu_row_new(lang)),
                    )
                    ok += 1
                    break
                except RetryAfter as e:
                    delay = e.retry_after
                    delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                    if attempt == 2:
                        failed += 1
                    else:
                        await asyncio.sleep(float(delay) + 1)
                except Forbidden:
                    failed += 1  # they blocked the bot
                    break
                except TelegramError as e:
                    logging.warning("Announcement failed for %s: %s", row["user_id"], e)
                    failed += 1
                    break
            await asyncio.sleep(0.05)  # stay well under Telegram's rate limit
    except Exception:
        logging.exception("Announcement crashed")
    try:
        await bot.send_message(
            chat_id=owner_id, text=t(owner_lang, "bc_done", ok=ok, failed=failed)
        )
    except TelegramError:
        pass


async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """bc:pick:<all|subs> -> confirm ; bc:go:<all|subs> -> send ; bc:cancel."""
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    uid = query.from_user.id
    lang = ADMIN_LANG
    if not is_owner(uid):
        await query.answer(t(lang, "owner_only"), show_alert=True)
        return
    await query.answer()
    await get_user(query.from_user)
    back = kb([btn(t(lang, "b_back"), "adm:home")])

    if action == "cancel":
        await show(query, t(lang, "cancelled"), back)
        return

    which = parts[2] if len(parts) > 2 else ""
    if which not in ("all", "subs"):
        return
    original = getattr(query.message, "reply_to_message", None)
    if original is None:
        await show(query, t(lang, "bc_expired"), back)
        return
    rows = await audience_rows(which)
    audience = t(lang, "aud_all" if which == "all" else "aud_subs")

    if action == "pick":
        await show(
            query,
            t(lang, "bc_confirm", n=len(rows), audience=audience),
            kb(
                [btn(t(lang, "b_bc_yes"), f"bc:go:{which}")],
                [btn(t(lang, "b_cancel"), "bc:cancel")],
            ),
        )
    elif action == "go":
        # Buttons off first: a double tap can never send the announcement twice.
        if not await drop_buttons(query):
            return
        await show(query, t(lang, "bc_started", n=len(rows)), back)
        context.application.create_task(
            run_broadcast(context.bot, original.chat_id, original.message_id, rows, uid, lang)
        )


async def _admin_command_guard(update: Update, owner_only: bool) -> bool:
    uid = update.effective_user.id
    allowed = is_owner(uid) if owner_only else await is_admin(uid)
    return allowed  # unauthorized users get no reply, so the commands stay hidden


async def handle_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):  # stats: owner only
        mode = (context.args[0].lower() if context.args else "png")
        await send_stats(context.bot, update.effective_chat.id, mode if mode in STATS_MODES else "png")


async def handle_block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, False):
        lang = ADMIN_LANG
        if not context.args:
            await update.message.reply_text(t(lang, "usage_block"))
            return
        _, text = await set_blocked_by_code(context.args[0], True, lang)
        await update.message.reply_text(text)


async def handle_unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):
        lang = ADMIN_LANG
        if not context.args:
            await update.message.reply_text(t(lang, "usage_unblock"))
            return
        _, text = await set_blocked_by_code(context.args[0], False, lang)
        await update.message.reply_text(text)


async def handle_addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):
        lang = ADMIN_LANG
        if not context.args:
            await update.message.reply_text(t(lang, "usage_addadmin"))
            return
        await update.message.reply_text(
            await add_admin(update.effective_user.id, context.args[0], lang)
        )


async def handle_removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _admin_command_guard(update, True):
        lang = ADMIN_LANG
        username = parse_username(context.args[0]) if context.args else None
        if not username:
            await update.message.reply_text(t(lang, "usage_removeadmin"))
            return
        result = await db("execute", "DELETE FROM admins WHERE username = $1", username)
        await update.message.reply_text(
            t(lang, "removed", u=username)
            if int(result.split()[-1])
            else t(lang, "not_admin", u=username)
        )


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """06:30 Ethiopian-standard time: verse for daily-verse subscribers."""
    await purge_expired_admins()
    rows = await db("fetch", "SELECT user_id, lang FROM users WHERE subscribed AND NOT blocked")
    logging.info("Sending daily verse to %d subscribers", len(rows))
    for row in rows:
        lang = row["lang"] or "en"
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=t(lang, "reminder", verse=daily_verse(lang)),
                reply_markup=kb(menu_row_new(lang)),
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
# Only brand-new messages. Edited messages have no update.message and would
# crash the handlers.
REQUEST_FILTER = (
    filters.ChatType.PRIVATE
    & filters.UpdateType.MESSAGE
    & (
        (filters.TEXT & ~filters.COMMAND)
        | filters.PHOTO
        | filters.VOICE
        | filters.VIDEO
        | filters.VIDEO_NOTE
        | filters.AUDIO
        | filters.Document.ALL
        | filters.ANIMATION
    )
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
        ("help", handle_help),
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
        (r"^(feel|more):", handle_feeling),
        (r"^r:", handle_reminder_toggle),
        (r"^cat:", handle_category),
        (r"^send:", handle_send),
        (r"^cancel$", handle_cancel_send),
        (r"^js$", handle_save_request),
        (r"^j:", handle_journal),
        (r"^adm:", handle_admin),
        (r"^bc:", handle_broadcast),
        (r"^ainv:", handle_admin_invite),
        (r"^blk:", handle_block_button),
    ):
        application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    application.add_handler(MessageHandler(REQUEST_FILTER, handle_message))
    # Anything else in private chat (stickers, locations, ...)
    application.add_handler(
        MessageHandler(private & filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_unsupported)
    )
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
        had_accept = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'admins' AND column_name = 'accepted_at'"
        )
        await conn.execute(SCHEMA)
        if not had_accept:
            # One-time: admins who were already active before invitations existed.
            await conn.execute(
                "UPDATE admins SET accepted_at = created_at WHERE user_id IS NOT NULL"
            )
        try:
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_anon_code_uidx ON users (anon_code)"
            )
        except asyncpg.PostgresError as e:
            logging.warning("Could not make anon_code unique (duplicate codes exist?): %s", e)

    application = build_application()

    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}/{BOT_TOKEN}", allowed_updates=Update.ALL_TYPES
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start"),
            BotCommand("menu", "Open the menu"),
            BotCommand("help", "About this bot & contact"),
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
        uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
    )

    logging.info("Bot is starting with webhook at %s", WEBHOOK_URL)
    async with application:
        await application.start()
        await server.serve()
        await application.stop()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
