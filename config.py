import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Необходимо указать BOT_TOKEN в .env файле")

# Настройки для yt-dlp
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() in ("true", "1")
PROXY_URL = os.getenv("PROXY_URL")
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES_PATH")
DOWNLOADS_DIR = "downloads"
