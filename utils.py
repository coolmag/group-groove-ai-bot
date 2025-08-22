import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BotState, Source

logger = logging.getLogger(__name__)

# --- Проверка прав ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь администратором."""
    return update.effective_user.id in ADMIN_IDS

# --- Форматирование сообщений ---
def format_track_info(track) -> str:
    """Форматирует информацию о треке в красивую строку."""
    if not track:
        return "—"
    
    minutes, seconds = divmod(track.duration, 60)
    return f"{track.artist} - {track.title} ({minutes}:{seconds:02d})"

def format_status_message(state: BotState) -> str:
    """Собирает полное статус-сообщение."""
    radio_status = "✅ Включено" if state.radio_status.is_on else "❌ Выключено"
    track_info = format_track_info(state.radio_status.current_track)
    
    commands_list = (
        "<b>Доступные команды:</b>\n"
        "<code>/play &lt;название&gt;</code> - заказать трек\n"
        "<code>/menu</code> - показать это меню\n"
        "<code>/next</code> - следующий трек (админ)\n"
        "<code>/source</code> - сменить источник (админ)\n"
        "<code>/ron</code> - включить радио (админ)\n"
        "<code>/roff</code> - выключить радио (админ)"
    )

    return (
        f"<b>🎵 Music Bot Status</b>\n\n"
        f"<b>Источник поиска:</b> {state.source.value}\n"
        f"<b>Статус радио:</b> {radio_status}\n"
        f"<b>Текущий жанр:</b> {state.radio_status.current_genre.capitalize()}\n"
        f"<b>Последний трек:</b> {track_info}\n\n"
        f"{commands_list}"
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