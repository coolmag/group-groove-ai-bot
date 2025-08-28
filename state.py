from dataclasses import dataclass, field
from enum import Enum

class Source(Enum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    VK = "vk"

@dataclass
class RadioStatus:
    is_on: bool = True
    current_genre: str | None = None
    current_track: str | None = None
    last_played_time: float = 0
    cooldown: int = 60

@dataclass
class BotState:
    active_chats: dict = field(default_factory=dict)
    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = field(default_factory=RadioStatus)
    search_results: dict = field(default_factory=dict)
    voting_active: bool = False
    vote_counts: dict = field(default_factory=dict)
    playlist: list = field(default_factory=list)
