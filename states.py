from typing import Dict, Optional
from config import Source


class RadioState:
    """Состояние радио"""
    def __init__(self):
        self.is_on = False
        self.current_genre = None
        self.last_played = 0


class ChatState:
    """Состояние чата"""
    def __init__(self):
        self.status_message_id = None


class BotState:
    """Глобальное состояние бота"""
    def __init__(self):
        self.source = Source.DEEZER
        self.radio = RadioState()
        self.chats: Dict[int, ChatState] = {}