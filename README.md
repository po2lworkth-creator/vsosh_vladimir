# vsosh_vladimir

Telegram bot for class/test management and CTF task generation with YandexGPT.

## Run

1) Install dependencies:
   `pip install -r requirements.txt`

2) Create a `.env` file with:
   - `BOT_TOKEN` (Telegram bot token)
   - `ADMIN_CODE` (optional, default: `admin123`)
   - `YANDEX_API_KEY` (required for YandexGPT features)
   - `YANDEX_FOLDER_ID` (required for YandexGPT features)

3) Start the bot:
   `python simple_bor_v7.py`

## Data

The bot stores state in `bot_data.json` and creates the file automatically if missing.
