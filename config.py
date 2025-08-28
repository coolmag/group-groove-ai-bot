import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Токен вашего бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Данные для Pyrogram / py-tgcalls
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Остальные настройки
DOWNLOADS_DIR = "downloads"
PROXY_ENABLED = False
PROXY_URL = ""
YOUTUBE_COOKIES_PATH = None

# Валидация обязательных переменных
if not BOT_TOKEN:
    raise ValueError("Необходимо указать BOT_TOKEN в .env файле")
if not API_ID or not API_HASH:
    raise ValueError("Необходимо указать API_ID и API_HASH в .env файле для работы с голосовыми чатами")
