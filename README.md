# Anonymous Prayer Bot

A Telegram bot that passes prayer requests to a prayer team without revealing
who sent them. Everything is done with buttons (`/menu` opens the main menu).
Works in English and Amharic.

## For users
- **Send a prayer request**: text, photo, voice, video, video note, audio, file
  or a photo album. Pick a category, check the preview, then send anonymously.
- **Scripture for me**: tap how you feel (anxious, exhausted, afraid, sad,
  lonely, guilty, need direction, thankful) and get a fitting verse.
- **Private prayer journal**: entries are encrypted, only you can open them.
  Set a reminder to revisit an entry, mark it answered, delete one entry or
  everything.
- **Reminders**: a daily verse at 6:30 AM and a weekly reflection on Sunday
  evening (Ethiopian time). They are **on by default**: new users are told once and can switch them off under
  Reminders, and everyone who already had an account was switched on once, at the
  first deploy of this version (see `ENABLE_FOR_EXISTING_USERS` in `main.py`). A
  marker row makes that happen only once, so later deploys never undo anyone's choice.
- **Language**: English or Amharic. Amharic users also see the Ethiopian
  calendar and clock.
- **Help**: `/help` explains the bot and has a Contact the developer button.

## For the team
- Senders appear only as an anonymous `#CODE` (12 characters). Nobody on the
  team can see who sent a request.
- Requests are sent with Telegram's content protection, so team members cannot
  forward or save them.
- **Admins** (added by the owner) receive the prayer requests and can block a
  sender with the button under each request, or by code. An admin can quit the
  role at any time.
- **Owner** (the ultimate admin) can do everything an admin can, plus:
  - see the **stats dashboard** as a designed picture, or as text
  - **unblock** senders
  - **add and remove admins** by Telegram @username
  - send an **announcement** to all users or only to daily-verse subscribers,
    after a preview and a confirmation
- Nobody becomes an admin without agreeing: the person gets an Accept / Decline
  invitation that expires after 48 hours, and the owner is told the answer.
- Admin commands: `/stats` and `/addadmin @name` and `/removeadmin @name`
  (owner), `/block CODE` (admins), `/unblock CODE` (owner). People who are not
  allowed get no reply, so the commands stay hidden.
- The admin panel is always in English. Requests and reminders arrive in each
  person's own language.

## Files
| File | What it is |
|---|---|
| `main.py` | the bot |
| `stats_template.svg` | the design of the stats dashboard (see below) |
| `fonts/` | the DejaVu Sans fonts used to draw the dashboard picture (the `.ttf` files can also sit next to `main.py`) |
| `verses.json` | all Bible verses (see below) |
| `requirements.txt` | Python packages |

## Stats dashboard (owner only)
Open **🔧 Admin → 📊 Stats**, or send `/stats`. You get the dashboard as a
picture, and the buttons under it refresh the numbers or switch to text:

| View | What you get |
|---|---|
| 🖼 Picture | the full designed dashboard from `stats_template.svg`, drawn as an image you can read right in the chat |
| 📝 Text | the same numbers as text, always available |

The picture is drawn by `resvg` using the fonts in `fonts/`, in a separate
small process so the bot never freezes while it works. If something needed for
it is missing, the bot falls back automatically (designed picture, then a
simpler Pillow drawing, then text) instead of failing.

`/stats text` opens the text view directly. `/stats svg` (not shown as a button)
sends the raw `.svg` file, which you can open in a browser.

The dashboard shows today / 7 days / 30 days / all-time requests, a 7-day bar
chart with the average, requests by category, a language donut with new-user
chips, message types, and reminder subscriptions. It contains numbers only,
never any prayer text.

**Changing the look:** the design lives in `stats_template.svg`. Open it in a
browser, Inkscape or Figma and restyle colors, fonts and positions. Keep the
double-curly-brace placeholders (like `DATE` or `WEEK_CHART` inside the braces)
as they are: the bot fills them in by name and refuses to build the picture if
one is unknown or the file stops being valid XML. The picture is always drawn
with DejaVu Sans, so text can be a little wider than in your own browser. Leave
room in text boxes. The charts are drawn inside their cards using the card's own
top-left corner as the origin, so if you resize a card, adjust the matching
function in `main.py` (`_svg_week_chart`, `_svg_category_rows`,
`_svg_users_card`, `_svg_type_chips`, `_svg_sub_bars`).

## Verses (`verses.json`)
The verses live in `verses.json`, not in the code. The file must sit next to
`main.py`, and the bot will not start without it. Each verse looks like this:

```json
{
  "ref": "Psalm 23:1",
  "ref_am": "መዝሙር 23:1",
  "en": "The LORD is my shepherd; I shall lack nothing.",
  "am": "Amharic text",
  "feelings": ["anxious", "afraid"]
}
```

- `en` is required (World English Bible, public domain). If `am` is empty,
  Amharic users see the English verse.
- `feelings` decides which "Scripture for me" buttons can show the verse. Valid
  values: `anxious`, `tired`, `afraid`, `sad`, `lonely`, `guilty`, `lost`,
  `thankful`.
- To add a verse, add one more entry. No code change is needed.
- Check the license of the Amharic Bible you use. The 1962/2003 Amharic Bible
  (United Bible Societies) asks for its copyright statement to be shown. Put
  that line in `AMHARIC_BIBLE_CREDIT` and it appears under every Amharic verse.

## Environment variables
| Name | Required | What |
|---|---|---|
| `BOT_TOKEN` | yes | token from @BotFather |
| `MY_USER_ID` | yes | base prayer team Telegram IDs, comma-separated (they are admins too) |
| `DATABASE_URL` | yes | Postgres connection string (Neon) |
| `JOURNAL_SECRET` | yes | long random secret that encrypts journals. Never change or lose it, or saved journal entries become unreadable |
| `OWNER_ID` | no | the owner's Telegram ID. Defaults to the first ID in `MY_USER_ID` |
| `AMHARIC_BIBLE_CREDIT` | no | credit line shown under Amharic verses |
| `WEBHOOK_URL` | no | Render provides `RENDER_EXTERNAL_URL` by itself |

Never put these values in the code or in GitHub.

## Deploy (Render, free)
1. Create a free Postgres database on Neon and copy its connection string.
2. On Render, create a **Web Service** from this repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
   - Health check path: `/health`
3. Add the environment variables above.
4. Add an UptimeRobot HTTP(s) monitor for `https://<your-service>.onrender.com/health`
   with a 5-minute interval. It keeps the service awake, and the daily verse,
   weekly reflection and journal reminders only run while it is awake.

Tables are created and upgraded automatically on every start.

## Privacy and security notes
- Keep this repository private.
- The bot token, journal secret and database password are hidden from the
  logs. The HTTP library's request logging is turned off and the web server's
  access log is disabled, because the webhook path contains the bot token.
- Journal entries are encrypted in the database. No admin screen can show them,
  but whoever controls the server also controls `JOURNAL_SECRET`.
- Ordinary users' @usernames are never stored, only a keyed hash that lets the
  owner promote someone who already started the bot. The database holds Telegram
  user IDs, language, reminder settings and a block flag, but never prayer
  request contents.
- If the bot token is ever exposed, revoke it in BotFather (`/revoke`) and
  update `BOT_TOKEN` on Render.

## Run locally
    pip install -r requirements.txt
    python main.py
