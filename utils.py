# -*- coding: utf-8 -*-
import json
import logging
import re
from functools import wraps
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import config
from locks import state_lock

logger = logging.getLogger(__name__)

# --- Admin ---
async def is_admin(user_id: int) -> bool:
    """Checks if a user ID belongs to an admin."""
    return user_id in config.ADMIN_IDS

def admin_only(func):
    """Decorator to restrict a command to admins only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not await is_admin(user_id):
            if update.message:
                await update.message.reply_text("This command is for admins only.")
            elif update.callback_query:
                await update.callback_query.answer("This command is for admins only.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- State ---
def load_state() -> config.State:
    """Loads the bot's state from the config file."""
    if config.CONFIG_FILE.exists():
        try:
            data = json.loads(config.CONFIG_FILE.read_text(encoding='utf-8'))
            return config.State.model_validate(data)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return config.State()
    return config.State()

async def save_state_from_botdata(bot_data: dict):
    """Saves the bot's state to a file."""
    async with state_lock:
        state: Optional[config.State] = bot_data.get('state')
        if state:
            try:
                config.CONFIG_FILE.write_text(state.model_dump_json(indent=4))
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

def set_escaped_error(state: config.State, error: str):
    """Sets the last_error field in the state with Markdown V2 escaped text."""
    state.last_error = escape_markdown_v2(str(error)) if error else None

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
