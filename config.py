import os
import logging
from enum import Enum
from typing import Dict, Optional, List
from pydantic import BaseModel
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Токен бота (обязательно)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в .env файле!")
    raise ValueError("BOT_TOKEN обязателен")

# Cookies для YouTube (очень важно!)
COOKIES_TEXT = os.getenv("COOKIES_TEXT", "")
if not COOKIES_TEXT:
    logger.warning("⚠️ COOKIES_TEXT не задан. YouTube будет блокировать запросы!")
else:
    logger.info("✅ COOKIES_TEXT загружен (длина: %d символов)", len(COOKIES_TEXT))

# Админы
ADMIN_IDS = []
try:
    admin_str = os.getenv("ADMIN_IDS", "")
    if admin_str:
        ADMIN_IDS = [int(id.strip()) for id in admin_str.split(",") if id.strip().isdigit()]
except Exception as e:
    logger.error(f"Ошибка парсинга ADMIN_IDS: {e}")

if not ADMIN_IDS:
    logger.warning("⚠️ ADMIN_IDS не задан. Некоторые команды будут недоступны")

# Определяем директорию для загрузок
if os.path.exists("/tmp"):
    DOWNLOADS_DIR = "/tmp/music_bot_downloads"
else:
    DOWNLOADS_DIR = "downloads"

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Прокси (необязательно)
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL", "")

# Ограничения
MAX_QUERY_LENGTH = 200
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# --- Модели данных ---
class TrackInfo(BaseModel):
    title: str
    artist: str
    duration: int
    source: str

class RadioStatus(BaseModel):
    is_on: bool = False
    current_genre: Optional[str] = None
    current_track: Optional[TrackInfo] = None
    last_played_time: float = 0
    cooldown: int = 300  # 5 минут

class ChatData(BaseModel):
    status_message_id: Optional[int] = None

# --- Источники музыки ---
class Source(Enum):
    YOUTUBE = "YouTube"
    YOUTUBE_MUSIC = "YouTube Music"
    SOUNDCLOUD = "SoundCloud"
    JAMENDO = "Jamendo"
    ARCHIVE = "Internet Archive"
    DEEZER = "Deezer"

    @staticmethod
    def get_available_sources():
        """Возвращает только доступные источники."""
        return [Source.DEEZER, Source.YOUTUBE, Source.YOUTUBE_MUSIC]

class BotState:
    """Состояние бота."""
    
    def __init__(self):
        self.source: Source = Source.DEEZER  # Deezer как источник по умолчанию
        self.radio_status = RadioStatus()
        self.active_chats: Dict[int, ChatData] = {}

# --- Сообщения ---
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
    'proxy_enabled': "🌐 Прокси включен.",
    'proxy_disabled': "🌐 Прокси выключен.",
    'admin_only': "⛔ Эта команда только для администраторов.",
    'error': "⚠️ Произошла ошибка. Попробуйте позже.",
    'youtube_blocked': "⚠️ YouTube заблокировал запрос. Проверьте COOKIES_TEXT в настройках.",
    'downloading': "📥 Скачиваю трек...",
    'processing': "⚙️ Обрабатываю аудио..."
}

def check_environment() -> bool:
    """Проверяет наличие необходимых зависимостей."""
    try:
        import subprocess
        import sys
        
        # Проверка FFmpeg
        try:
            result = subprocess.run(
                ['ffmpeg', '-version'], 
                capture_output=True, 
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info("✅ FFmpeg доступен: %s", result.stdout.split('\n')[0])
            else:
                logger.error("❌ FFmpeg не найден или не работает!")
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
        
        # Проверка cookies
        if not COOKIES_TEXT:
            logger.warning("⚠️ COOKIES_TEXT не задан. YouTube может блокировать запросы!")
        else:
            # Проверяем, что cookies содержат необходимые поля
            if 'youtube.com' in COOKIES_TEXT and 'LOGIN_INFO' in COOKIES_TEXT:
                logger.info("✅ Cookies выглядят валидными")
            else:
                logger.warning("⚠️ Cookies могут быть неполными")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка проверки окружения: {e}", exc_info=True)
        return False

def cleanup_temp_files():
    """Очищает временные файлы."""
    try:
        import glob
        import time
        import shutil
        
        current_time = time.time()
        
        # Очищаем старые файлы в директории загрузок
        for filepath in glob.glob(os.path.join(DOWNLOADS_DIR, "*.*")):
            try:
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > 3600:  # Удаляем файлы старше 1 часа
                    os.remove(filepath)
                    logger.debug(f"Удален старый файл: {os.path.basename(filepath)}")
            except Exception as e:
                logger.debug(f"Не удалось удалить файл {filepath}: {e}")
        
        # Очищаем старые логи (старше 7 дней)
        log_files = glob.glob("*.log")
        for log_file in log_files:
            try:
                if os.path.exists(log_file):
                    file_age = current_time - os.path.getmtime(log_file)
                    if file_age > 7 * 24 * 3600:  # 7 дней
                        os.remove(log_file)
            except:
                pass
                
    except Exception as e:
        logger.error(f"Ошибка при очистке файлов: {e}")