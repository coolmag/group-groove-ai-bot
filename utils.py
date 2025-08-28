# utils.py (v9 рефакторинг)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from models import BotState

def get_menu_text(state: BotState) -> str:
    """Генерирует текст для меню статуса."""
    radio_status_icon = "🟢" if state.radio_status.is_on else "🔴"
    return (
        f"Groove AI Radio — Источник: {state.source.value}\n"
        f"Статус радио: {radio_status_icon} {'ВКЛ' if state.radio_status.is_on else 'ВЫКЛ'}\n"
        f"Текущий жанр: {state.radio_status.current_genre or '—'}\n"
        f"Трек: {state.radio_status.current_track or '—'}"
    )

def get_menu_keyboard(state: BotState) -> InlineKeyboardMarkup:
    """Генерирует клавиатуру для меню статуса."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Радио ON", callback_data="radio_on"),
            InlineKeyboardButton("⏸ Радио OFF", callback_data="radio_off"),
            InlineKeyboardButton("⏭ Пропустить", callback_data="next_track"),
        ],
        [
            InlineKeyboardButton(f"🔁 Источник: {state.source.value}", callback_data="source_switch"),
            InlineKeyboardButton("🗳 Голосование", callback_data="vote_now"),
        ]
    ])