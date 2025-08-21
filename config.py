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
    POLL_DURATION_SECONDS = 600 # 10 minutes
    MAX_FILE_SIZE = 48_000_000 # Telegram limit is 50MB, being safe
    MAX_DURATION = 7200 # 2 hours
    MIN_DURATION = 10 # 10 seconds
    PLAYED_URLS_MEMORY = 200
    DOWNLOAD_TIMEOUT = 60
    DEFAULT_SOURCE = "youtube"
    DEFAULT_GENRE = "lo-fi hip hop"
    PAUSE_BETWEEN_TRACKS = 2
    STATUS_UPDATE_MIN_INTERVAL = 5
    RETRY_INTERVAL = 5
    SEARCH_LIMIT = 10
    MAX_RETRIES = 3
    REFILL_THRESHOLD = 5
    SUPPORTED_SOURCES = ["youtube", "soundcloud", "vk", "archive"]

# --- Environment Variables and Paths ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID"))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")
PORT = int(os.getenv("PORT", 8080))

# For cloud deployments, cookie data is passed as env var content
# For local, it can be a file path
VK_COOKIES_DATA = os.getenv("VK_COOKIES_DATA")
YOUTUBE_COOKIES_DATA = os.getenv("YOUTUBE_COOKIES_DATA")

# --- Models ---
class NowPlaying(BaseModel):
    title: str
    duration: int
    url: str
    start_time: float = Field(default_factory=time.time)

class State(BaseModel):
    is_on: bool = False
    genre: str = Constants.DEFAULT_GENRE
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
            "pop", "80s pop", "90s pop", "2000s pop",
            "rock", "60s rock", "70s rock", "80s rock", "90s rock",
            "hip hop", "90s hip hop", "2000s hip hop",
            "electronic", "90s electronic", "2000s electronic",
            "classical", "jazz", "blues", "country", "metal",
            "reggae", "folk", "indie", "rap", "r&b", "soul", "funk", "disco",
            "lo-fi hip hop", "synthwave", "cyberpunk", "ambient"
        ])))
    )

    @field_serializer('radio_playlist', 'played_radio_urls')
    def serialize_deques(self, v, _info):
        return list(v)

    @field_validator('radio_playlist', 'played_radio_urls', mode='before')
    @classmethod
    def deques_validate(cls, v):
        return deque(v)