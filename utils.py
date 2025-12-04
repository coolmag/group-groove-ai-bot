import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BotState, PROXY_ENABLED, MESSAGES, MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

# --- Проверка прав ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь администратором"""
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return False
    
    return user_id in ADMIN_IDS

# --- Форматирование сообщений ---
def format_duration(seconds: int) -> str:
    """Форматирует длительность в читаемый вид"""
    if seconds <= 0:
        return "0:00"
    
    # Ограничиваем очень большие значения
    if seconds > 86400 * 7:  # Больше 7 дней
        days = seconds // 86400
        return f"{days} д."
    
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"

def format_track_info(track) -> str:
    """Форматирует информацию о треке"""
    if not track:
        return "—"
    
    duration = format_duration(track.duration)
    return f"{track.artist} - {track.title} ({duration})"

def format_status_message(state: BotState) -> str:
    """Собирает статус-сообщение"""
    # Статус радио
    radio_status = "✅ Включено" if state.radio_status.is_on else "❌ Выключено"
    
    # Информация о треке
    track_info = format_track_info(state.radio_status.current_track)
    
    # Статус прокси
    proxy_status = "✅ Вкл" if PROXY_ENABLED else "❌ Выкл"
    
    # Список команд
    commands_list = (
        "<b>📋 Доступные команды:</b>\n"
        "<code>/play &lt;название&gt;</code> - заказать трек\n"
        "<code>/audiobook &lt;название&gt;</code> - найти аудиокнигу\n"
        "<code>/menu</code> - показать это меню\n"
        "<code>/status</code> - обновить статус\n"
        "<code>/next</code> - следующий трек (админ)\n"
        "<code>/source</code> - сменить источник (админ)\n"
        "<code>/ron</code> - включить радио (админ)\n"
        "<code>/roff</code> - выключить радио (админ)\n"
        f"<code>/proxy</code> - статус прокси"
    )

    # Формируем итоговое сообщение
    message = (
        f"<b>🎵 Music Bot Status</b>\n\n"
        f"<b>📊 Статистика:</b>\n"
        f"• <b>Источник поиска:</b> {state.source.value}\n"
        f"• <b>Статус радио:</b> {radio_status}\n"
        f"• <b>Текущий жанр:</b> {state.radio_status.current_genre.capitalize()}\n"
        f"• <b>Последний трек:</b> {track_info}\n"
        f"• <b>Прокси:</b> {proxy_status}\n"
        f"• <b>Активных чатов:</b> {len(state.active_chats)}\n\n"
        f"{commands_list}"
    )
    
    return message

# --- Клавиатуры ---
def get_menu_keyboard():
    """Создает инлайн-клавиатуру меню"""
    buttons = [
        [InlineKeyboardButton(▶️ Вкл. радио", callback_data="radio_on"),
         InlineKeyboardButton("⏹️ Выкл. радио", callback_data="radio_off")],
        [InlineKeyboardButton("⏭️ След. трек", callback_data="next_track"),
         InlineKeyboardButton("💿 Сменить источник", callback_data="source_switch")],
        [InlineKeyboardButton("🔄 Обновить статус", callback_data="refresh_status")]
    ]
    return InlineKeyboardMarkup(buttons)

# --- Валидация ---
def validate_query_length(query: str) -> tuple[bool, str]:
    """Проверяет длину запроса"""
    if len(query) > MAX_QUERY_LENGTH:
        return False, MESSAGES['query_too_long']
    return True, ""

def validate_query_not_empty(query: str) -> tuple[bool, str]:
    """Проверяет, что запрос не пустой"""
    query = query.strip()
    if not query:
        return False, "❌ Запрос не может быть пустым"
    return True, ""