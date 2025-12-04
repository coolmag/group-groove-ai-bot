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
    
    # Проверка cookies файлов
    if YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
        logger.info("YouTube cookies file found")
    else:
        logger.warning("YouTube cookies file not found or not configured")
        
    if PROXY_ENABLED and PROXY_URL:
        logger.info(f"Proxy enabled: {PROXY_URL}")
    elif PROXY_ENABLED:
        logger.warning("Proxy enabled but no proxy URL configured")
    
    logger.info("Environment check completed")
    return True
