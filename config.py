import os
import logging
from enum import Enum
from typing import Dict, Optional
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Проверка обязательных переменных
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN обязателен")

COOKIES_TEXT = os.getenv("COOKIES_TEXT", "")
if not COOKIES_TEXT:
    logger.warning("⚠️ COOKIES_TEXT не задан")

# Прокси (необязательно)
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL", "")

# Директория для загрузок
if os.path.exists("/tmp"):
    DOWNLOADS_DIR = "/tmp/music_bot_downloads"
else:
    DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Админы
ADMIN_IDS = []
try:
    admin_str = os.getenv("ADMIN_IDS", "")
    if admin_str:
        ADMIN_IDS = [int(id.strip()) for id in admin_str.split(",") if id.strip().isdigit()]
except:
    pass

# Ограничения
MAX_QUERY_LENGTH = 200
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# Модели данных
class TrackInfo:
    def __init__(self, title: str, artist: str, duration: int, source: str):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.source = source

class RadioStatus:
    def __init__(self):
        self.is_on = False
        self.current_genre = None
        self.current_track = None
        self.last_played_time = 0
        self.cooldown = 300

class ChatData:
    def __init__(self):
        self.status_message_id = None

# Источники
class Source(Enum):
    YOUTUBE = "YouTube"
    YOUTUBE_MUSIC = "YouTube Music"
    DEEZER = "Deezer"

class BotState:
    def __init__(self):
        self.source: Source = Source.DEEZER
        self.radio_status = RadioStatus()
        self.active_chats: Dict[int, ChatData] = {}

# Сообщения
MESSAGES = {
    'welcome': "🎵 Добро пожаловать!\nИспользуйте /play <название> для поиска музыки.",
    'play_usage': "🎶 Использование: /play <название трека>",
    'audiobook_usage': "📖 Использование: /audiobook <название книги>",
    'searching': "🔍 Ищу трек...",
    'searching_audiobook': "🔍 Ищу аудиокнигу...",
    'not_found': "❌ Трек не найден. Попробуйте другой запрос.",
    'audiobook_not_found': "❌ Аудиокнига не найдена.",
    'file_too_large': "❌ Файл слишком большой.",
    'radio_on': "📻 Радио включено!",
    'radio_off': "📻 Радио выключено.",
    'next_track': "⏭️ Пропускаю трек...",
    'source_switched': "💿 Источник изменен на: {source}",
    'proxy_enabled': "🌐 Прокси включен.",
    'proxy_disabled': "🌐 Прокси выключен.",
    'admin_only': "⛔ Только для администраторов.",
    'error': "⚠️ Произошла ошибка.",
    'youtube_blocked': "⚠️ YouTube заблокировал запрос.",
}

def check_environment():
    try:
        import subprocess
        import yt_dlp
        
        # Проверка FFmpeg
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.error("❌ FFmpeg не найден!")
            return False
            
        logger.info("✅ FFmpeg доступен")
        logger.info(f"✅ yt-dlp {yt_dlp.version.__version__} доступен")
        logger.info(f"✅ Директория загрузок: {DOWNLOADS_DIR}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False

def cleanup_temp_files():
    import glob
    import time
    import os
    
    current_time = time.time()
    for filepath in glob.glob(os.path.join(DOWNLOADS_DIR, "*.*")):
        try:
            if current_time - os.path.getmtime(filepath) > 3600:
                os.remove(filepath)
        except:
            pass