BOT_TOKEN = "YOUR_BOT_TOKEN"
DOWNLOADS_DIR = "downloads"
PROXY_ENABLED = False
PROXY_URL = ""
YOUTUBE_COOKIES_PATH = None

from enum import Enum
from dataclasses import dataclass

class Source(Enum):
    YOUTUBE = "youtube"

@dataclass
class TrackInfo:
    title: str
    url: str
