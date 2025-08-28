from dataclasses import dataclass, field
from enum import Enum
from typing import List

class Source(Enum):
    YOUTUBE = "youtube"

@dataclass
class TrackInfo:
    title: str
    url: str

@dataclass
class AudioBookChapter:
    title: str
    url: str # Ссылка на mp3 файл

@dataclass
class AudioBook:
    id: int
    title: str
    author: str
    chapters: List[AudioBookChapter] = field(default_factory=list)