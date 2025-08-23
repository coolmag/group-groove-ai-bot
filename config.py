import os
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

# === Environment ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_ENV = os.getenv("ADMINS", "").strip()  # comma-separated user IDs
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES", "").strip()

PROXY_ENABLED = os.getenv("PROXY_ENABLED", "0") in ("1", "true", "True")
PROXY_URL = os.getenv("PROXY_URL", "").strip()
FFMPEG_LOCATION = os.getenv("FFMPEG_LOCATION", "").strip()  # optional custom path to ffmpeg/ffprobe

# Voting settings
VOTE_WINDOW_SEC = int(os.getenv("VOTE_WINDOW_SEC", "180"))  # 3 minutes by default
SONG_COOLDOWN_SEC = int(os.getenv("SONG_COOLDOWN_SEC", "240"))  # 4 minutes between radio sends
RADIO_SEARCH_QUERY_SUFFIX = os.getenv("RADIO_SEARCH_QUERY_SUFFIX", "music")

# === Constants ===
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
    duration: int  # seconds
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
    "radio_on": "📻 Радио включено.",
    "radio_off": "⏸ Радио выключено.",
    "admin_only": "⛔ Команда доступна только администраторам.",
    "next_track": "⏭ Пропускаем текущий трек...",
    "source_switched": "🔁 Источник переключён: <b>{source}</b>",
    "proxy_enabled": "🌐 Прокси включён (см. переменные окружения).",
    "proxy_disabled": "🌐 Прокси выключен.",
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
    ids: List[int] = []
    if ADMINS_ENV:
        for p in ADMINS_ENV.split(","):
            p = p.strip()
            if p.isdigit():
                ids.append(int(p))
    return ids

ADMINS: List[int] = parse_admins()
