# -*- coding: utf-8 -*-
import json
import logging
from typing import Optional

from config import State, CONFIG_FILE
from locks import state_lock

logger = logging.getLogger(__name__)

def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    s_int = int(seconds)
    return f"{s_int // 60:02d}:{s_int % 60:02d}"

def get_progress_bar(progress: float, width: int = 10) -> str:
    filled = int(width * progress)
    return "█" * filled + " " * (width - filled)

def escape_markdown_v2(text: str) -> str:
    """Escapes a string for use in Telegram MarkdownV2."""
    if not isinstance(text, str) or not text:
        return ""
    escape_chars = r'_[]()~`>#+-=|{}.!'
    import re
    return re.sub(f'([\\{re.escape(escape_chars)}])', r'\\\\1', text)

def set_escaped_error(state: State, error: str):
    state.last_error = escape_markdown_v2(error) if error else None

async def save_state_from_botdata(bot_data: dict):
    async with state_lock:
        state: Optional[State] = bot_data.get('state')
        if state:
            try:
                CONFIG_FILE.write_text(state.model_dump_json(indent=4))
                logger.debug("State saved to config file")
            except Exception as e:
                logger.error(f"Failed to save state: {e}")