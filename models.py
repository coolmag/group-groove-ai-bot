from dataclasses import dataclass, field
from enum import Enum

class Source(Enum):
    YOUTUBE = "youtube"

@dataclass
class TrackInfo:
    title: str
    url: str

@dataclass
class RadioStatus:
    is_on: bool = True
    current_genre: str = None
    current_track: str = None
    last_played_time: float = 0
    cooldown: int = 60

@dataclass
class BotState:
    source: Source = Source.YOUTUBE
    radio_status: RadioStatus = field(default_factory=RadioStatus)
    playlist: list[TrackInfo] = field(default_factory=list)
    # Голосование пока оставим для будущих фич
    voting_active: bool = False
    vote_counts: dict = field(default_factory=dict)
