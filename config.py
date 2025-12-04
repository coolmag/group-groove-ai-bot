import os
import logging
from enum import Enum
from typing import Dict, Optional, Any
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования - только в консоль
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Токен бота (обязательно)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в .env файле!")
    raise ValueError("BOT_TOKEN обязателен")

# Cookies для YouTube
COOKIES_TEXT = os.getenv("COOKIES_TEXT", "")
if not COOKIES_TEXT:
    logger.warning("⚠️ COOKIES_TEXT не задан. YouTube будет блокировать запросы!")
else:
    logger.info("✅ COOKIES_TEXT загружен")

# Админы
ADMIN_IDS = []
try:
    admin_str = os.getenv("ADMIN_IDS", "")
    if admin_str:
        ADMIN_IDS = [int(id.strip()) for id in admin_str.split(",") if id.strip().isdigit()]
except Exception as e:
    logger.error(f"Ошибка парсинга ADMIN_IDS: {e}")

# Определяем директорию для загрузок
if os.path.exists("/tmp"):
    DOWNLOADS_DIR = "/tmp/music_bot_downloads"
else:
    DOWNLOADS_DIR = "downloads"

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Простые классы данных (без pydantic)
class TrackInfo:
    def __init__(self, title: str, artist: str, duration: int, source: str):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.source = source

class RadioStatus:
    def __init__(self):
        self.is_on: bool = False
        self.current_genre: Optional[str] = None
        self.current_track: Optional[TrackInfo] = None
        self.last_played_time: float = 0
        self.cooldown: int = 300

class ChatData:
    def __init__(self):
        self.status_message_id: Optional[int] = None

# Источники музыки
class Source(Enum):
    YOUTUBE = "YouTube"
    YOUTUBE_MUSIC = "YouTube Music"
    SOUNDCLOUD = "SoundCloud"
    JAMENDO = "Jamendo"
    ARCHIVE = "Internet Archive"
    DEEZER = "Deezer"

    @staticmethod
    def get_available_sources():
        return [Source.DEEZER, Source.YOUTUBE, Source.YOUTUBE_MUSIC]

class BotState:
    def __init__(self):
        self.source: Source = Source.DEEZER
        self.radio_status = RadioStatus()
        self.active_chats: Dict[int, ChatData] = {}

# Сообщения
MESSAGES = {
    'welcome': "🎵 Добро пожаловать в музыкального бота!\n\nИспользуйте /play <название> для поиска музыки.",
    'menu': "📋 Главное меню",
    'play_usage': "🎶 Использование: /play <название трека или артиста>",
    'audiobook_usage': "📖 Использование: /audiobook <название книги>",
    'searching': "🔍 Ищу трек...",
    'searching_audiobook': "🔍 Ищу аудиокнигу...",
    'not_found': "❌ Трек не найден. Попробуйте другой запрос или используйте /source для смены источника.",
    'audiobook_not_found': "❌ Аудиокнига не найдена. Попробуйте другое название.",
    'file_too_large': "❌ Файл слишком большой для отправки.",
    'radio_on': "📻 Радио включено! Музыка скоро начнет играть.",
    'radio_off': "📻 Радио выключено.",
    'next_track': "⏭️ Пропускаю текущий трек...",
    'source_switched': "💿 Источник изменен на: {source}",
    'admin_only': "⛔ Эта команда только для администраторов.",
    'error': "⚠️ Произошла ошибка. Попробуйте позже.",
    'youtube_blocked': "⚠️ YouTube заблокировал запрос. Используйте /source для переключения на Deezer."
}

def check_environment() -> bool:
    """Проверяет наличие необходимых зависимостей."""
    try:
        import subprocess
        
        # Проверка FFmpeg
        try:
            result = subprocess.run(
                ['ffmpeg', '-version'], 
                capture_output=True, 
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info("✅ FFmpeg доступен")
            else:
                logger.error("❌ FFmpeg не найден!")
                return False
        except FileNotFoundError:
            logger.error("❌ FFmpeg не установлен!")
            return False
        except subprocess.TimeoutExpired:
            logger.error("❌ FFmpeg завис при проверке!")
            return False
        
        # Проверка yt-dlp
        try:
            import yt_dlp
            logger.info(f"✅ yt-dlp {yt_dlp.version.__version__} доступен")
        except ImportError:
            logger.error("❌ yt-dlp не установлен!")
            return False
        
        logger.info(f"✅ Директория загрузок: {DOWNLOADS_DIR}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка проверки окружения: {e}")
        return False

def cleanup_temp_files():
    """Очищает временные файлы."""
    try:
        import glob
        import time
        
        current_time = time.time()
        
        for filepath in glob.glob(os.path.join(DOWNLOADS_DIR, "*.*")):
            try:
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > 3600:
                    os.remove(filepath)
                    logger.debug(f"Удален старый файл: {os.path.basename(filepath)}")
            except Exception as e:
                logger.debug(f"Не удалось удалить файл {filepath}: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка при очистке файлов: {e}")
