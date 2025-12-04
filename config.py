import os
import logging
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from enum import Enum
from typing import List, Dict, Optional
import subprocess

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Основные ID и токены ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Читаем переменную ADMIN_IDS, ожидая строку с ID через запятую
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "0")
# Превращаем строку в список чисел
ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]

# --- Учетные данные для авто-обновления Cookies ---
# ВАЖНО: Используйте отдельный аккаунт Google, а не личный!
GOOGLE_USERNAME = os.getenv("GOOGLE_USERNAME", "")
GOOGLE_PASSWORD = os.getenv("GOOGLE_PASSWORD", "")

# Настройки прокси
PROXY_URL = os.getenv("PROXY_URL", "")
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"

# Конфигурация для yt-dlp
DOWNLOADS_DIR = "downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# Cookies paths
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES_PATH", "")
SOUNDCLOUD_COOKIES_PATH = os.getenv("SOUNDCLOUD_COOKIES_PATH", "")

# --- Источники --- #
class Source(Enum):
    YOUTUBE = "YouTube"
    YOUTUBE_MUSIC = "YouTube Music"
    SOUNDCLOUD = "SoundCloud"
    JAMENDO = "Jamendo"
    ARCHIVE = "Internet Archive"
    DEEZER = "Deezer"  # Только для поиска метаданных

# --- Модели состояния (Pydantic) --- #
class TrackInfo(BaseModel):
    title: str = "Неизвестно"
    artist: str = "Неизвестно"
    duration: int = 0
    source: str = "Unknown"

class RadioStatus(BaseModel):
    is_on: bool = False
    current_genre: str = "lofi hip hop"
    current_track: Optional[TrackInfo] = None
    last_played_time: float = 0.0
    cooldown: int = 300  # 5 минут

class BotState(BaseModel):
    class ChatData(BaseModel):
        status_message_id: Optional[int] = None

    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = Field(default_factory=RadioStatus)
    active_chats: Dict[int, ChatData] = Field(default_factory=dict)

# --- Тексты и константы --- #
MESSAGES = {
    "welcome": "🎶 Привет! Я музыкальный бот. Используй /menu, чтобы начать.",
    "admin_only": "⛔ Эта команда доступна только администраторам.",
    "radio_on": "📻 Радио включено! Музыка скоро начнет играть.",
    "radio_off": "🔇 Радио выключено.",
    "play_usage": "🎵 Укажите название песни после /play, например: /play Queen - Bohemian Rhapsody",
    "searching": "🔍 Ищу трек...",
    "not_found": "😕 Трек не найден.",
    "next_track": "⏭️ Включаю следующий трек на радио...",
    "source_switched": "💿 Источник изменен на: {source}",
    "proxy_enabled": "🔄 Прокси активирован",
    "proxy_disabled": "🔁 Прокси отключен"
}

GENRES = [
    "lofi hip hop", "chillstep", "ambient", "downtempo", "jazz hop",
    "synthwave", "deep house", "liquid drum and bass", "psybient", "lounge",
    "chillout", "trance", "house", "techno", "dubstep"
]

def check_environment():
    """Проверяет необходимые переменные окружения и зависимости"""
    logger.info("Checking environment...")
    
    # Проверка переменных окружения
    required_vars = ['BOT_TOKEN']
    for var in required_vars:
        if not os.getenv(var):
            logger.error(f"Missing environment variable: {var}")
            return False
    
    # Проверка директорий
    required_dirs = ['downloads']
    for dir_name in required_dirs:
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
            logger.info(f"Created directory: {dir_name}")
    
    # Проверка доступности FFmpeg (для yt-dlp)
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        logger.info("FFmpeg is available")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("FFmpeg is not available - audio conversion may fail")
    
    # Улучшенная проверка cookies
    if YOUTUBE_COOKIES_CONTENT:
        # Эта проверка произойдет до создания файла, так как переменная уже прочитана
        logger.info("YOUTUBE_COOKIES_CONTENT environment variable is set.")
    elif YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
        logger.info(f"Using YouTube cookies from file: {YOUTUBE_COOKIES_PATH}")
    else:
        logger.warning("CRITICAL: No YouTube cookies provided via file or environment variable. Downloads will likely fail.")
        
    if PROXY_ENABLED and PROXY_URL:
        logger.info(f"Proxy enabled: {PROXY_URL}")
    elif PROXY_ENABLED:
        logger.warning("Proxy enabled but no proxy URL configured")
    
    logger.info("Environment check completed")
    return True

# --- Управление Cookies из переменной окружения ---

# Сначала читаем переменную окружения
YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES_CONTENT", "")

# Глобальная переменная для хранения пути к временному файлу
TEMP_COOKIE_PATH = None

# Если переменная с содержимым cookies установлена, создаем временный файл
if YOUTUBE_COOKIES_CONTENT:
    # ДОБАВЛЕНО ЛОГИРОВАНИЕ: Проверяем, что переменная не пустая
    logger.info(f"Found YOUTUBE_COOKIES_CONTENT with length: {len(YOUTUBE_COOKIES_CONTENT)}")
    import tempfile
    import atexit
    
    try:
        # Создаем временный файл и записываем в него содержимое
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8', suffix='.txt') as tf:
            tf.write(YOUTUBE_COOKIES_CONTENT)
            TEMP_COOKIE_PATH = tf.name
        logger.info(f"Cookies from YOUTUBE_COOKIES_CONTENT successfully stored in temporary file: {TEMP_COOKIE_PATH}")

        # Регистрируем функцию, которая удалит временный файл при выходе из программы
        @atexit.register
        def cleanup_temp_cookie():
            global TEMP_COOKIE_PATH
            if TEMP_COOKIE_PATH and os.path.exists(TEMP_COOKIE_PATH):
                try:
                    os.remove(TEMP_COOKIE_PATH)
                    logger.info(f"Successfully cleaned up temporary cookie file: {TEMP_COOKIE_PATH}")
                except Exception as e:
                    logger.error(f"Error cleaning up temporary cookie file {TEMP_COOKIE_PATH}: {e}")
    except Exception as e:
        logger.error(f"Failed to create temporary cookie file from environment variable: {e}")
else:
    # ДОБАВЛЕНО ЛОГИРОВАНИЕ: Явно сообщаем, что переменная не найдена
    logger.info("YOUTUBE_COOKIES_CONTENT environment variable not found. Falling back to file path if available.")
