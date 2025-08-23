from typing import List, Optional
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def is_admin(user_id: int, admins_env: Optional[str] = None) -> bool:
    if not admins_env:
        return False
    ids = {int(x) for x in admins_env.split(",") if x.strip().isdigit()}
    return user_id in ids

def make_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Включить радио", callback_data="ron"),
         InlineKeyboardButton("⏸ Выключить", callback_data="roff")],
        [InlineKeyboardButton("🔄 Источник: YouTube", callback_data="src_youtube"),
         InlineKeyboardButton("SoundCloud", callback_data="src_soundcloud")],
        [InlineKeyboardButton("🗳 Голосовать", callback_data="vote")]
    ])

def make_search_keyboard(titles: List[str]):
    rows = []
    for i, t in enumerate(titles[:10]):
        rows.append([InlineKeyboardButton(f"{i+1}. {t[:48]}", callback_data=f"pick_{i}")])
    return InlineKeyboardMarkup(rows)

def make_vote_keyboard(genres: List[str]):
    rows = []
    for g in genres:
        rows.append([InlineKeyboardButton(g, callback_data=f"vote_{g}")])
    return InlineKeyboardMarkup(rows)

def format_status(source: str, genre: Optional[str], last_title: Optional[str]):
    return (f"🎵 Music Bot Status\n\n"
            f"Источник поиска: {source}\n"
            f"Статус радио: {'✅ Включено' if genre else '⏸ Выключено'}\n"
            f"Текущий жанр: {genre or '—'}\n"
            f"Последний трек: {last_title or '—'}\n\n"
            f"Команды: /play, /menu, /vote")
