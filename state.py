from dataclasses import dataclass
from enum import Enum

class Source(Enum):
    YOUTUBE = "youtube"

@dataclass
class RadioStatus:
    is_on: bool = True
    current_genre: str = None
    current_track: str = None
    last_played_time: float = 0
    cooldown: int = 60

@dataclass
class BotState:
    active_chats: dict = None
    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = RadioStatus()
    search_results: dict = None
    voting_active: bool = False
    vote_counts: dict = None
    playlist: list = None

    def __post_init__(self):
        if self.active_chats is None:
            self.active_chats = {}
        if self.search_results is None:
            self.search_results = {}
        if self.vote_counts is None:
            self.vote_counts = {}
        if self.playlist is None:
            self.playlist = []
