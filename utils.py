import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BotState

logger = logging.getLogger(__name__)

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_user.id in ADMIN_IDS

def format_track_info(track) -> str:
    if not track:
        return "(пусто)"
    return f"{track.artist} - {track.title} ({track.duration // 60}:{track.duration % 60:02d})"

def format_status_message(state: BotState) -> str:
    radio_status = "✅ Включено" if state.radio_status.is_on else "❌ Выключено"
    track_info = format_track_info(state.radio_status.current_track)
    
    commands_list = (
        "<b>Доступные команды:</b>\n"
        "<code>/play &lt;название&gt;</code> - заказать трек\n"
        "<code>/menu</code> - показать это меню"
    )

    return (
        f"<b>🎵 Group Groove AI (SoundCloud)</b>\n\n"
        f"<b>Статус радио:</b> {radio_status}\n"
        f"<b>Последний трек:</b> {track_info}\n\n"
        f"{commands_list}"
    )

async def get_menu_keyboard(is_radio_on: bool) -> InlineKeyboardMarkup:
    if is_radio_on:
        buttons = [
            [InlineKeyboardButton("⏹️ Выключить радио", callback_data="radio_off"),
             InlineKeyboardButton("⏭️ Следующий трек", callback_data="next_track")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton("▶️ Включить радио", callback_data="radio_on")]
        ]
    return InlineKeyboardMarkup(buttons)
