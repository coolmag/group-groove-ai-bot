import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from config import MESSAGES, ADMIN_IDS, BotState, MAX_QUERY_LENGTH

logger = logging.getLogger(__name__)

async def is_admin(update: Update, context) -> bool:
    user_id = update.effective_user.id
    return user_id in ADMIN_IDS

def get_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📻 Включить радио", callback_data='radio_on'),
            InlineKeyboardButton("🔇 Выключить радио", callback_data='radio_off'),
        ],
        [
            InlineKeyboardButton("⏭️ Следующий трек", callback_data='next_track'),
            InlineKeyboardButton("💿 Сменить источник", callback_data='source_switch'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_status_message(state: BotState) -> str:
    status_text = f"""
🎵 Music Bot Status

📊 Статистика:
• Источник поиска: {state.source.value}
• Статус радио: {'✅ Включено' if state.radio_status.is_on else '❌ Выключено'}
• Текущий жанр: {state.radio_status.current_genre or '—'}
• Последний трек: {state.radio_status.current_track.title if state.radio_status.current_track else '—'}
• Активных чатов: {len(state.active_chats)}

📋 Доступные команды:
/play [название] - заказать трек
/audiobook [название] - найти аудиокнигу
/menu - показать это меню
/status - обновить статус
/next - следующий трек (админ)
/source - сменить источник (админ)
/ron - включить радио (админ)
/roff - выключить радио (админ)
/proxy - статус прокси
    """
    return status_text.strip()

def validate_query_length(query: str):
    if len(query) > MAX_QUERY_LENGTH:
        return False, f"❌ Запрос слишком длинный (максимум {MAX_QUERY_LENGTH} символов)"
    if len(query.strip()) < 2:
        return False, "❌ Запрос слишком короткий"
    return True, ""