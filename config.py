import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List, Dict, Optional

load_dotenv()

# --- Основные ID и токены ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip()]

DOWNLOADS_DIR = "downloads"
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

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

    radio_status: RadioStatus = Field(default_factory=RadioStatus)
    active_chats: Dict[int, ChatData] = Field(default_factory=dict)

# --- Тексты и константы --- #
MESSAGES = {
    "admin_only": "⛔ Эта команда доступна только администраторам.",
    "radio_on": "📻 Радио включено! Ищу музыку на SoundCloud...",
    "radio_off": "🔇 Радио выключено.",
    "play_usage": "🎵 Укажите название песни после /play, например: /play Queen - Bohemian Rhapsody",
    "searching": "🔍 Ищу трек на SoundCloud...",
    "not_found": "😕 Трек не найден на SoundCloud.",
    "next_track": "⏭️ Включаю следующий трек...",
}

GENRES = [
    "lofi hip hop", "chillstep", "ambient", "downtempo", "jazz hop",
    "synthwave", "deep house", "liquid drum and bass", "psybient", "lounge"
]
