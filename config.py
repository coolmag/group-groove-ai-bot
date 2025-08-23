import os
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_ENV = os.getenv("ADMINS", "").strip()
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES", "").strip()

PROXY_ENABLED = os.getenv("PROXY_ENABLED", "0").lower() in ("1","true","yes")
PROXY_URL = os.getenv("PROXY_URL", "").strip()
FFMPEG_LOCATION = os.getenv("FFMPEG_LOCATION", "").strip()

VOTE_WINDOW_SEC = int(os.getenv("VOTE_WINDOW_SEC", "180"))
SONG_COOLDOWN_SEC = int(os.getenv("SONG_COOLDOWN_SEC", "240"))
RADIO_SEARCH_QUERY_SUFFIX = os.getenv("RADIO_SEARCH_QUERY_SUFFIX", "music")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

GENRES: List[str] = [
    "Electronic", "Pop", "Rock", "Hip-Hop", "House", "Techno", "Trance", "Ambient",
    "Drum & Bass", "Dubstep", "Jazz", "Blues", "Reggae", "Disco", "Funk", "Soul",
    "Classical", "Indie", "Synthwave", "Lo-fi"
]

class Source(str, Enum):
    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "ytmusic"
    SOUNDCLOUD = "soundcloud"
    JAMENDO = "jamendo"
    ARCHIVE = "archive"

@dataclass
class TrackInfo:
    id: str
    title: str
    artist: str
    duration: int
    source: str
    url: str

@dataclass
class RadioStatus:
    is_on: bool = True
    current_genre: Optional[str] = None
    current_track: Optional[TrackInfo] = None
    last_played_time: float = 0.0
    cooldown: int = SONG_COOLDOWN_SEC

@dataclass
class ChatData:
    status_message_id: Optional[int] = None

@dataclass
class BotState:
    active_chats: Dict[int, ChatData] = field(default_factory=dict)
    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = field(default_factory=RadioStatus)
    search_results: Dict[int, List[TrackInfo]] = field(default_factory=dict)
    voting_active: bool = False
    vote_end_ts: float = 0.0
    vote_counts: Dict[str, int] = field(default_factory=dict)
    playlist: List[TrackInfo] = field(default_factory=list)

MESSAGES = {
    "welcome": "👋 Привет! Я Groove AI Bot. Включай радио, ищи треки по названию и голосуй за жанр каждый час.",
    "play_usage": "Использование: <b>/play &lt;название&gt;</b> — покажу до 10 вариантов.",
    "searching": "🔎 Ищу треки...",
    "not_found": "😕 Ничего не нашлось. Попробуй другой запрос или источник.",
    "radio_on": "📻 Радио включено! Музыка скоро начнет играть.",
    "radio_off": "⏸ Радио выключено.",
    "admin_only": "⛔ Команда доступна только администраторам.",
    "next_track": "⏭ Пропускаем текущий трек...",
    "source_switched": "🔁 Источник переключён: <b>{source}</b>",
    "vote_started": "🗳 Старт голосования за жанр! Выбирайте ниже. Окно голосования: {mins} мин.",
    "vote_accepted": "✅ Голос за жанр <b>{genre}</b> засчитан!",
    "vote_ended": "🏁 Голосование окончено. Победил жанр: <b>{genre}</b>.",
}
def check_environment() -> bool:
    ok = True
    if not BOT_TOKEN:
        logging.getLogger(__name__).error("BOT_TOKEN не задан в окружении.")
        ok = False
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    return ok

def parse_admins() -> List[int]:
    out = []
    if ADMINS_ENV:
        for p in ADMINS_ENV.split(","):
            p = p.strip()
            if p.isdigit():
                out.append(int(p))
    return out

ADMINS: List[int] = parse_admins()
