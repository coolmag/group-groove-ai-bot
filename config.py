# -*- coding: utf-8 -*-
import os
import time
from pathlib import Path
from typing import List, Optional, Deque
from collections import deque

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_serializer, field_validator

# --- Setup ---
load_dotenv()

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 60
    POLL_DURATION_SECONDS = 10
    POLL_CHECK_TIMEOUT = 10
    MAX_FILE_SIZE = 50_000_000
    MAX_DURATION = 300
    MIN_DURATION = 30
    PLAYED_URLS_MEMORY = 100
    DOWNLOAD_TIMEOUT = 30
    DEFAULT_SOURCE = "soundcloud"
    DEFAULT_GENRE = "pop"
    PAUSE_BETWEEN_TRACKS = 0
    STATUS_UPDATE_INTERVAL = 10
    STATUS_UPDATE_MIN_INTERVAL = 2
    RETRY_INTERVAL = 30
    SEARCH_LIMIT = 50
    MAX_RETRIES = 3
    REFILL_THRESHOLD = 10

# --- Environment Variables and Paths ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or [482549032]
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", -1002892409779))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")
PORT = int(os.getenv("PORT", 8080))

# --- Models ---
class NowPlaying(BaseModel):
    title: str
    duration: int
    url: str
    start_time: float = Field(default_factory=time.time)

class State(BaseModel):
    is_on: bool = False
    genre: str = "lo-fi hip hop"
    source: str = Constants.DEFAULT_SOURCE
    radio_playlist: Deque[str] = Field(default_factory=deque)
    played_radio_urls: Deque[str] = Field(default_factory=deque)
    active_poll_id: Optional[str] = None
    poll_message_id: Optional[int] = None
    poll_options: List[str] = Field(default_factory=list)
    poll_votes: List[int] = Field(default_factory=list)
    status_message_id: Optional[int] = None
    last_status_update: float = 0.0
    now_playing: Optional[NowPlaying] = None
    last_error: Optional[str] = None
    votable_genres: List[str] = Field(
        default_factory=lambda: sorted(list(set([
            # Old list
            "pop", "pop 80s", "pop 90s", "pop 2000s",
            "rock", "rock 60s", "rock 70s", "rock 80s", "rock 90s",
            "hip hop", "hip hop 90s", "hip hop 2000s",
            "electronic", "electronic 90s", "electronic 2000s",
            "classical", "classical 18th century", "classical 19th century",
            "jazz", "jazz 50s", "jazz 60s",
            "blues", "blues 50s", "blues 60s",
            "country", "country 80s", "country 90s",
            "metal", "metal 80s", "metal 90s",
            "reggae", "reggae 70s", "reggae 80s",
            "folk", "folk 60s", "folk 70s",
            "indie", "indie 90s", "indie 2000s",
            "rap", "rap 80s", "rap 90s", "rap 2000s",
            "r&b", "r&b 90s", "r&b 2000s",
            "soul", "soul 60s", "soul 70s",
            "funk", "funk 70s", "funk 80s",
            "disco", "disco 70s", "disco 80s",
            # New list
            "rock 'n' roll", "doo-wop", "folk rock",
            "psychedelic rock", "hard rock", "glam rock",
            "punk rock", "heavy metal", "hip-hop", "new wave",
            "synthpop", "house", "techno", "grunge", "britpop", "industrial rock",
            "gangsta rap", "trip-hop", "pop punk", "emo", "crunk", "dubstep",
            "electropop", "trap"
        ])))
    )
    retry_count: int = 0

    @field_serializer('radio_playlist', 'played_radio_urls', 'poll_votes')
    def _serialize_deques(self, v, _info):
        if isinstance(v, list):
            return v
        return list(v)

    @field_validator('radio_playlist', 'played_radio_urls', 'poll_votes', mode='before')
    @classmethod
    def _lists_to_deques(cls, v, info):
        if isinstance(v, list):
            return deque(v) if 'votes' not in info.field_name else v
        return deque() if 'votes' not in info.field_name else []
