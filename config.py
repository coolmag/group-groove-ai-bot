import os
import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Deque, Tuple
from collections import deque

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")

PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL") or ""

YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES_PATH") or ""
SOUNDCLOUD_COOKIES_PATH = os.getenv("SOUNDCLOUD_COOKIES_PATH") or ""

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

GENRES = [
    "Chillout","Ambient","Lo-fi","Electronic","Techno","House","Soul","Jazz",
    "Hip-Hop","Trap","Drum and Bass","Trance","Retrowave","Synthwave","Chillstep"
]

DEFAULT_GENRE = "Chillout"

class Source(enum.Enum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"

@dataclass
class TrackInfo:
    title: str
    artist: Optional[str] = None
    duration: Optional[int] = None
    source: Optional[str] = None
    url: Optional[str] = None

@dataclass
class RadioStatus:
    is_on: bool = True
    current_genre: Optional[str] = None
    current_track: Optional[TrackInfo] = None
    last_sent_ts: float = 0.0

@dataclass
class BotState:
    active_chats: Dict[int, int] = field(default_factory=dict)
    source: Source = Source.YOUTUBE
    radio: RadioStatus = field(default_factory=RadioStatus)
    search_results: Dict[int, List[TrackInfo]] = field(default_factory=dict)
    voting_active: bool = False
    vote_counts: Dict[str, int] = field(default_factory=dict)
    vote_end_ts: float = 0.0
    history: Deque[str] = field(default_factory=lambda: deque(maxlen=20))

MESSAGES = {
    "radio_on": "📻 Радио включено! Музыка скоро начнет играть.",
    "radio_off": "⏸ Радио выключено.",
    "nothing_found": "⚠️ Для жанра '{genre}' ничего не найдено, пробую следующий...",
}

def is_valid() -> bool:
    return bool(BOT_TOKEN) and bool(LASTFM_API_KEY)
