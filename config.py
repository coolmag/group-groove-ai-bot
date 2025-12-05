import os
import tempfile
from typing import List, Optional
from enum import Enum
from dotenv import load_dotenv

load_dotenv()


class Source(str, Enum):
    """Источники музыки"""
    YOUTUBE = "YouTube"
    YOUTUBE_MUSIC = "YouTube Music"
    DEEZER = "Deezer"


class TrackInfo:
    """Информация о треке"""
    
    def __init__(self, title: str, artist: str, duration: int, source: str):
        self.title = title[:100] + "..." if len(title) > 100 else title
        self.artist = artist[:100] + "..." if len(artist) > 100 else artist
        self.duration = duration
        self.source = source
    
    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title}"


class Settings:
    """Настройки приложения"""
    
    # Обязательные
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    COOKIES_TEXT = os.getenv("COOKIES_TEXT", "")
    
    # Админы
    ADMIN_IDS = []
    admin_str = os.getenv("ADMIN_IDS", "")
    if admin_str:
        try:
            ADMIN_IDS = [int(id.strip()) for id in admin_str.split(",") if id.strip().isdigit()]
        except (ValueError, TypeError):
            ADMIN_IDS = []
    
    # Пути
    if os.path.exists("/tmp"):
        DOWNLOADS_DIR = "/tmp/music_bot_downloads"
    else:
        DOWNLOADS_DIR = "downloads"
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    # Лимиты
    MAX_QUERY_LENGTH = 200
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
    DOWNLOAD_TIMEOUT = 45
    
    # Повторные попытки
    MAX_RETRIES = 3
    RETRY_DELAY = 2.0
    
    # Радио
    RADIO_COOLDOWN = 300  # 5 минут
    RADIO_GENRES = [
        "lofi hip hop",
        "chillhop",
        "synthwave",
        "jazz",
        "ambient",
        "electronic",
    ]
    
    # Кэш
    CACHE_TTL = 3600 * 24 * 7  # 7 дней


settings = Settings()