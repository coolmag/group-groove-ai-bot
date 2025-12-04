import os
import logging
from enum import Enum
from typing import Dict, Optional
from pydantic import BaseModel
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в .env файле!")
    raise ValueError("BOT_TOKEN обязателен")

# Определяем директорию для загрузок (используем /tmp на Railway)
if os.path.exists("/tmp"):
    DOWNLOADS_DIR = "/tmp/music_bot_downloads"
else:
    DOWNLOADS_DIR = "downloads"

# Создаем директорию если её нет
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Прокси
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL", "")

# Админы
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

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

class BotState:
    class Config:
        arbitrary_types_allowed = True
    
    def __init__(self):
        self.source: Source = Source.YOUTUBE
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
    'not_found': "❌ Трек не найден. Попробуйте другой запрос.",
    'audiobook_not_found': "❌ Аудиокнига не найдена.",
    'file_too_large': "❌ Файл слишком большой для отправки.",
    'radio_on': "📻 Радио включено! Музыка скоро начнет играть.",
    'radio_off': "📻 Радио выключено.",
    'next_track': "⏭️ Пропускаю текущий трек...",
    'source_switched': "💿 Источник изменен на: {source}",
    'proxy_enabled': "🌐 Прокси включен.",
    'proxy_disabled': "🌐 Прокси выключен.",
    'admin_only': "⛔ Эта команда только для администраторов.",
    'error': "⚠️ Произошла ошибка. Попробуйте позже."
}

def check_environment() -> bool:
    """Проверяет наличие необходимых зависимостей."""
    try:
        # Проверка FFmpeg
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            logger.info("✅ FFmpeg доступен")
        else:
            logger.error("❌ FFmpeg не найден!")
            return False
        
        # Проверка cookies (если есть)
        cookies_text = os.getenv("COOKIES_TEXT", "")
        if cookies_text:
            logger.info("✅ Будут использоваться cookies из переменных окружения")
        else:
            logger.warning("⚠️ COOKIES_TEXT не задан, YouTube может блокировать запросы")
        
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
        
        for filepath in glob.glob(os.path.join(DOWNLOADS_DIR, "*.mp3")):
            try:
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > 3600:  # Удаляем файлы старше 1 часа
                    os.remove(filepath)
                    logger.debug(f"Удален старый файл: {os.path.basename(filepath)}")
            except:
                pass
    except Exception as e:
        logger.error(f"Ошибка при очистке файлов: {e}")
