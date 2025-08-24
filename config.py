# config.py (v8 фикс)
from dataclasses import dataclass
from enum import Enum
from typing import Optional

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

DOWNLOADS_DIR = "./downloads"

class Source(Enum):
    YOUTUBE = "youtube"
    VKMUSIC = "vk"

@dataclass
class TrackInfo:
    title: str
    url: str

@dataclass
class RadioStatus:
    is_on: bool
    current_genre: Optional[str]
    current_track: Optional[str]
    last_played_time: float
    cooldown: int

@dataclass
class BotState:
    active_chats: dict
    source: Source
    radio_status: RadioStatus
    search_results: dict
    voting_active: bool
    vote_counts: dict
    playlist: list

GENRES = [
    "Lo-fi",
    "Jazz",
    "Hip-hop",
    "Electronic",
    "Classical",
    "Rock",
]
