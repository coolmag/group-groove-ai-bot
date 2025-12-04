import os
import logging
import asyncio
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from enum import Enum
from typing import List, Dict, Optional
import subprocess
import tempfile
import atexit
import time

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Основные ID и токены ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Читаем переменную ADMIN_IDS, ожидая строку с ID через запятую
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]

# Настройки прокси
PROXY_URL = os.getenv("PROXY_URL", "")
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"

# Конфигурация для yt-dlp
DOWNLOADS_DIR = "downloads"
DOWNLOAD_TIMEOUT = 60
MAX_QUERY_LENGTH = 200
MAX_AUDIO_SIZE_MB = 50  # Максимальный размер аудиофайла в МБ

if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# Cookies paths
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES_PATH", "")
SOUNDCLOUD_COOKIES_PATH = os.getenv("SOUNDCLOUD_COOKIES_PATH", "")

# --- Источники ---
class Source(Enum):
    YOUTUBE = "YouTube"
    YOUTUBE_MUSIC = "YouTube Music"
    SOUNDCLOUD = "SoundCloud"
    JAMENDO = "Jamendo"
    ARCHIVE = "Internet Archive"
    DEEZER = "Deezer"

# --- Модели состояния ---
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
    cooldown: int = 300

class BotState(BaseModel):
    class ChatData(BaseModel):
        status_message_id: Optional[int] = None

    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = Field(default_factory=RadioStatus)
    active_chats: Dict[int, ChatData] = Field(default_factory=dict)

# --- Тексты и константы ---
MESSAGES = {
    "welcome": "🎶 Привет! Я музыкальный бот. Используй /menu, чтобы начать.",
    "admin_only": "⛔ Эта команда доступна только администраторам.",
    "radio_on": "📻 Радио включено! Музыка скоро начнет играть.",
    "radio_off": "🔇 Радио выключено.",
    "play_usage": "🎵 Укажите название песни после /play, например: /play Queen - Bohemian Rhapsody",
    "audiobook_usage": "📚 Укажите название аудиокниги после /audiobook",
    "searching": "🔍 Ищу трек...",
    "searching_audiobook": "📖 Ищу аудиокнигу...",
    "not_found": "😕 Трек не найден.",
    "audiobook_not_found": "😕 Аудиокнига не найдена.",
    "next_track": "⏭️ Включаю следующий трек на радио...",
    "source_switched": "💿 Источник изменен на: {source}",
    "proxy_enabled": "🔄 Прокси активирован",
    "proxy_disabled": "🔁 Прокси отключен",
    "query_too_long": f"❌ Запрос слишком длинный. Максимальная длина: {MAX_QUERY_LENGTH} символов.",
    "file_too_large": f"❌ Файл слишком большой. Максимум: {MAX_AUDIO_SIZE_MB} МБ."
}

GENRES = [
    "lofi hip hop", "chillstep", "ambient", "downtempo", "jazz hop",
    "synthwave", "deep house", "liquid drum and bass", "psybient", "lounge",
    "chillout", "trance", "house", "techno", "dubstep"
]

# --- Управление Cookies ---
YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES_CONTENT", "")
TEMP_COOKIE_PATH = None

def create_temp_cookie_file():
    """Создает временный файл с куки из переменной окружения"""
    global TEMP_COOKIE_PATH
    
    if not YOUTUBE_COOKIES_CONTENT:
        return None
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8') as f:
            f.write(YOUTUBE_COOKIES_CONTENT)
            TEMP_COOKIE_PATH = f.name
        logger.info(f"Created temporary cookie file: {TEMP_COOKIE_PATH}")
        return TEMP_COOKIE_PATH
    except Exception as e:
        logger.error(f"Failed to create temporary cookie file: {e}")
        return None

def cleanup_temp_files():
    """Очистка временных файлов"""
    global TEMP_COOKIE_PATH
    
    if TEMP_COOKIE_PATH and os.path.exists(TEMP_COOKIE_PATH):
        try:
            os.remove(TEMP_COOKIE_PATH)
            logger.info(f"Cleaned up temporary cookie file: {TEMP_COOKIE_PATH}")
        except Exception as e:
            logger.error(f"Failed to clean up cookie file: {e}")

# Регистрируем очистку при завершении
atexit.register(cleanup_temp_files)

def check_environment() -> bool:
    """Проверяет необходимые переменные окружения и зависимости"""
    logger.info("Проверка окружения...")
    
    # Проверка обязательных переменных
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        return False
    
    if not ADMIN_IDS or ADMIN_IDS == [0]:
        logger.warning("⚠️ ADMIN_IDS не установлены или установлены в 0")
    
    # Проверка директорий
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    # Проверка FFmpeg
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        logger.info("✅ FFmpeg доступен")
    except Exception as e:
        logger.error(f"❌ FFmpeg не найден: {e}")
        return False
    
    # Проверка cookies (НЕ создаем файлы здесь, только проверяем наличие)
    cookie_source = None
    if YOUTUBE_COOKIES_CONTENT:
        cookie_source = "переменная окружения"
        # Файл будет создан позже, когда это действительно нужно
    elif YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
        cookie_source = f"файл: {YOUTUBE_COOKIES_PATH}"
    
    if cookie_source:
        logger.info(f"✅ Будут использоваться cookies из {cookie_source}")
    else:
        logger.warning("⚠️ Cookies не предоставлены, возможны ограничения при скачивании")
    
    # Проверка прокси
    if PROXY_ENABLED:
        if PROXY_URL:
            logger.info(f"✅ Прокси включен: {PROXY_URL}")
        else:
            logger.warning("⚠️ Прокси включен, но URL не указан")
    
    logger.info("✅ Проверка окружения завершена успешно")
    return True