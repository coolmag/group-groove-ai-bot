# -*- coding: utf-8 -*-
import os
from pathlib import Path
from typing import List, Deque
from collections import deque

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import Optional
import time

# --- Setup ---
load_dotenv()

# --- Constants ---
class Constants:
    # Bot behavior
    DEFAULT_SOURCE = "youtube"
    DEFAULT_GENRE = "lo-fi hip hop"
    SUPPORTED_SOURCES = ["youtube", "soundcloud", "vk", "archive"]
    
    # Radio loop
    REFILL_THRESHOLD = 5
    PAUSE_BETWEEN_TRACKS = 2
    PLAYED_URLS_MEMORY = 200

    # Downloader
    SEARCH_LIMIT = 10
    MAX_RETRIES = 3
    DOWNLOAD_TIMEOUT = 60
    MAX_FILE_SIZE = 48 * 1024 * 1024  # 48 MB
    MAX_DURATION = 7200  # 2 hours
    MIN_DURATION = 10   # 10 seconds

    # UI/UX
    VOTING_INTERVAL_SECONDS = 3600 # 1 hour
    POLL_DURATION_SECONDS = 600 # 10 minutes
    STATUS_UPDATE_MIN_INTERVAL = 5 # seconds

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID"))
PORT = int(os.getenv("PORT", 8080))

# --- Paths ---
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")

# --- Credentials (as environment variables) ---
VK_COOKIES_DATA = os.getenv("VK_COOKIES_DATA")
YOUTUBE_COOKIES_DATA = os.getenv("YOUTUBE_COOKIES_DATA")

# --- Pydantic Models for State Management ---
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
            "rock", "70s rock", "80s rock", "90s rock",
            "hip hop", "90s hip hop", "2000s hip hop",
            "electronic", "ambient", "synthwave", "cyberpunk",
            "classical", "jazz", "blues", "country", "metal",
            "reggae", "folk", "indie", "rap", "r&b", "soul", "funk", "disco",
            "lo-fi hip hop"
        ])))
    )

    class Config:
        arbitrary_types_allowed = True
