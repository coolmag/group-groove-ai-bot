# utils.py (v8 фикс)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_menu_keyboard(state):
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
