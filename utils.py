# -*- coding: utf-8 -*-
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# --- Formatting ---
def format_duration(seconds: Optional[float]) -> str:
    """Formats seconds into a MM:SS string."""
    if not seconds or seconds <= 0:
        return "--:--"
    s_int = int(seconds)
    return f"{s_int // 60:02d}:{s_int % 60:02d}"

def get_progress_bar(progress: float, width: int = 10) -> str:
    """Creates a text-based progress bar."""
    filled = int(width * progress)
    return "\u2588" * filled + " " * (width - filled)

def escape_markdown_v2(text: str) -> str:
    """Escapes a string for use in Telegram MarkdownV2."""
    if not isinstance(text, str) or not text:
        return ""
    escape_chars = r'_[]()~`>#+-=|{}.!'
        return re.sub(f'([\\{re.escape(escape_chars)}])', r'\\1', text)