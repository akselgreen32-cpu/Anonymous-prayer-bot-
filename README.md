# Anonymous Prayer Bot

Telegram bot that forwards prayer requests to recipients without revealing
the sender.

## Features
- Text, photo, voice, video, video note, audio and file requests
- Amharic / English (chosen at /start, change with /language)
- Category buttons (health, family, school, work, spiritual, other)
- Persistent 15-second cooldown (stored in the database, survives restarts)
- Timestamps in Ethiopian calendar and clock for Amharic senders, EAT otherwise
- Daily prayer reminder with a verse at 06:30 Ethiopian time (/subscribe)
- Admin tools: /stats, /block CODE, /unblock CODE (senders appear only as an
  anonymous #CODE)
- Bible verses from the World English Bible (public domain)
- /lordsprayer in English and Amharic

## Environment variables
- `BOT_TOKEN`: token from @BotFather
- `MY_USER_ID`: recipient Telegram IDs, comma-separated
- `DATABASE_URL`: Postgres connection string (Neon)
- `ADMIN_IDS`: optional, comma-separated; defaults to the recipients
- `WEBHOOK_URL`: optional; Render provides `RENDER_EXTERNAL_URL` itself

## Run
pip install -r requirements.txt
python main.py

Health check: `/health`
