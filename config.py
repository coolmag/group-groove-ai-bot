import os
from dataclasses import dataclass, field
from typing import Dict, Optional
from enum import Enum

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() in ("1","true","yes")
PROXY_URL = os.getenv("PROXY_URL", "")

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

class Source(str, Enum):
    YOUTUBE = "youtube"
    YTMUSIC = "ytmusic"
    SOUNDCLOUD = "soundcloud"
    JAMENDO = "jamendo"
    ARCHIVE = "archive"

@dataclass
class TrackInfo:
    title: str = ""
    artist: Optional[str] = None
    duration: Optional[int] = None
    source: Optional[str] = None
    url: Optional[str] = None

@dataclass
class RadioStatus:
    is_on: bool = True
    current_genre: Optional[str] = None
    current_track: Optional[TrackInfo] = None
    last_played_time: float = 0.0
    cooldown: int = 60

@dataclass
class BotState:
    @dataclass
    class ChatData:
        status_message_id: Optional[int] = None

    active_chats: Dict[int, ChatData] = field(default_factory=dict)
    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = field(default_factory=RadioStatus)
    search_results: Dict[int, list] = field(default_factory=dict)
    voting_active: bool = False
    vote_counts: Dict[str, int] = field(default_factory=dict)
    playlist: list = field(default_factory=list)

MESSAGES = {
    "welcome": "👋 Привет! Я Groove AI Bot.",
    "play_usage": "Использование: /play <название>",
    "searching": "🔎 Ищу треки...",
    "not_found": "😕 Ничего не найдено.",
    "radio_on": "📻 Радио включено! Музыка скоро начнет играть.",
    "radio_off": "⏸ Радио выключено.",
    "admin_only": "⛔ Команда доступна только администраторам.",
    "next_track": "⏭ Пропускаем текущий трек...",
    "source_switched": "🔁 Источник переключён: {source}",
    "proxy_enabled": "🔗 Proxy включён.",
    "proxy_disabled": "🔗 Proxy отключён.",
}
def check_environment() -> bool:
    ok = True
    if not BOT_TOKEN:
        print("BOT_TOKEN not set in environment")
        ok = False
    return ok
