from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def get_main_keyboard():
    """Главная клавиатура"""
    keyboard = [
        [
            InlineKeyboardButton("📻 Вкл радио", callback_data='radio_on'),
            InlineKeyboardButton("🔇 Выкл радио", callback_data='radio_off'),
        ],
        [
            InlineKeyboardButton("⏭️ След. трек", callback_data='next_track'),
            InlineKeyboardButton("💿 Источник", callback_data='source_switch'),
        ],
        [
            InlineKeyboardButton("🔄 Обновить", callback_data='menu_refresh'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_source_keyboard():
    """Клавиатура выбора источника"""
    keyboard = [
        [
            InlineKeyboardButton("YouTube", callback_data='source_youtube'),
            InlineKeyboardButton("YT Music", callback_data='source_ytmusic'),
        ],
        [
            InlineKeyboardButton("Deezer", callback_data='source_deezer'),
        ],
        [
            InlineKeyboardButton("↩️ Назад", callback_data='menu_refresh'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)