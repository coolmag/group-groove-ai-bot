from typing import Dict, Optional
import asyncio
from config import Source


class RadioState:
    """Состояние радио"""
    def __init__(self):
        self.is_on = False
        self.current_genre: Optional[str] = None
        self.skip_event = asyncio.Event()


class BotState:
    """Глобальное состояние бота"""
    def __init__(self):
        self.source = Source.YOUTUBE  # YouTube по умолчанию для лучшего опыта
        self.radio = RadioState()