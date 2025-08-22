import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from enum import Enum
from typing import List, Dict, Optional

# Загрузка переменных окружения
load_dotenv()

# --- Основные ID и токены ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Читаем переменную ADMIN_IDS, ожидая строку с ID через запятую
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "0")
# Превращаем строку в список чисел
ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]


# Конфигурация для yt-dlp
DOWNLOADS_DIR = "downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

# --- Источники --- #
class Source(Enum):
    YOUTUBE = "YouTube"
    VK = "VK"
    SOUNDCLOUD = "SoundCloud"
    ARCHIVE = "Internet Archive"

# --- Модели состояния (Pydantic) --- #
class TrackInfo(BaseModel):
    title: str = "Неизвестно"
    artist: str = "Неизвестно"
    duration: int = 0

class RadioStatus(BaseModel):
    is_on: bool = False
    current_genre: str = "lounge"
    current_track: Optional[TrackInfo] = None
    last_played_time: float = 0.0
    cooldown: int = 180 # 3 минуты

class BotState(BaseModel):
    class ChatData(BaseModel):
        status_message_id: Optional[int] = None

    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = Field(default_factory=RadioStatus)
    active_chats: Dict[int, ChatData] = Field(default_factory=dict)

# --- Тексты и константы --- #
MESSAGES = {
    "welcome": "🎶 Привет! Я Group Groove AI. Используй /menu, чтобы начать.",
    "admin_only": "⛔ Эта команда доступна только администраторам.",
    "radio_on": "📻 Радио включено! Музыка скоро начнет играть.",
    "radio_off": "🔇 Радио выключено.",
    "play_usage": "🎵 Укажите название песни после /play, например: /play Queen - Bohemian Rhapsody",
    "searching": "🔍 Ищу трек...",
    "not_found": "😕 Трек не найден.",
    "next_track": "⏭️ Включаю следующий трек на радио...",
    "source_switched": "💿 Источник изменен на: {source}"
}

GENRES = [
    "lofi hip hop", "chillstep", "ambient", "downtempo", "jazz hop",
    "synthwave", "deep house", "liquid drum and bass", "psybient", "lounge"
]
