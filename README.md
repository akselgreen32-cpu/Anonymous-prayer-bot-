# Anonymous Prayer Bot

Telegram bot that passes prayer requests to a prayer team without revealing
the sender. Everything is done with buttons (/menu opens the main menu).

## Features
- Send a prayer request (text, photo, voice, video, video note, audio, file)
  with a category and a preview (Send anonymously / Don't send)
- Amharic / English, including Ethiopian calendar and clock for Amharic users
- Scripture for the moment: pick a feeling, get a fitting verse
- Private prayer journal: encrypted, optional reminder to revisit an entry,
  mark answered, delete one entry or everything
- Daily verse at 06:30 and a weekly reflection (Sunday evening), both opt-in
- Persistent 15-second cooldown (stored in the database)
- Owner (ultimate admin) and admins added by Telegram @username
  - Admins: stats, block senders (also from the button under each request)
  - Owner: all of that + unblock, add and remove admins
  - Admins also receive the prayer requests
- Senders appear only as an anonymous #CODE

## Environment variables
| Name | Required | What |
|---|---|---|
| `BOT_TOKEN` | yes | token from @BotFather |
| `MY_USER_ID` | yes | base prayer-team Telegram IDs, comma-separated (also admins) |
| `DATABASE_URL` | yes | Neon Postgres connection string |
| `JOURNAL_SECRET` | yes | long random secret that encrypts journals. Never change or lose it |
| `OWNER_ID` | no | the ultimate admin's Telegram ID (defaults to the first `MY_USER_ID`) |
| `AMHARIC_BIBLE_CREDIT` | no | credit line shown under Amharic verses |

`WEBHOOK_URL` is optional; Render provides `RENDER_EXTERNAL_URL` itself.

## Amharic verses
Fill in `verses_am.json` (reference -> Amharic text). Empty entries fall back
to English. Check the license of the Bible text you use.

## Run
    pip install -r requirements.txt
    python main.py

Health check: `/health`  (point UptimeRobot at it, 5-minute interval)
