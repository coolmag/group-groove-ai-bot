import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BotState

logger = logging.getLogger(__name__)

# --- Проверка прав ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return update.effective_user.id in ADMIN_IDS

# --- Форматирование сообщений ---
def format_track_info(track) -> str:
    """Форматирует информацию о треке в красивую строку."""
    if not track:
        return "(пусто)"
    return f"{track.artist} - {track.title} ({track.duration // 60}:{track.duration % 60:02d})"

def format_status_message(state: BotState) -> str:
    """Собирает полное статус-сообщение."""
    radio_status = "✅ Включено" if state.radio_status.is_on else "❌ Выключено"
    track_info = format_track_info(state.radio_status.current_track)
    
    return (
        f"<b>🎵 Group Groove AI Status</b>\n\n"
        f"<b>Источник поиска:</b> {state.source.value}\n"
        f"<b>Статус радио:</b> {radio_status}\n"
        f"<b>Текущий жанр:</b> {state.radio_status.current_genre.capitalize()}\n"
        f"<b>Последний трек:</b> {track_info}"
    )

# --- Клавиатуры ---
async def get_menu_keyboard() -> InlineKeyboardMarkup:
    """Создает инлайн-клавиатуру меню."""
    buttons = [
        [InlineKeyboardButton("▶️ Вкл. радио", callback_data="radio_on"),
         InlineKeyboardButton("⏹️ Выкл. радио", callback_data="radio_off")],
        [InlineKeyboardButton("⏭️ След. трек", callback_data="next_track"),
         InlineKeyboardButton("💿 Сменить источник", callback_data="source_switch")]
    ]
    return InlineKeyboardMarkup(buttons)