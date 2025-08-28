import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# --- Основные переменные ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# --- Настройки для yt-dlp ---
# Преобразуем 'true'/'True' в True, все остальное в False
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() in ("true", "1")
PROXY_URL = os.getenv("PROXY_URL")  # например, http://user:pass@host:port
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES_PATH")  # например, youtube_cookies.txt

# --- Валидация ---
if not BOT_TOKEN:
    raise ValueError("Необходимо указать BOT_TOKEN в .env файле")
if not API_ID or not API_HASH:
    raise ValueError("Необходимо указать API_ID и API_HASH в .env файле для работы с голосовыми чатами")

# Проверка консистентности прокси
if PROXY_ENABLED and not PROXY_URL:
    raise ValueError("PROXY_ENABLED is true, but PROXY_URL is not set.")