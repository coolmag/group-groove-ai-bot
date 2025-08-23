import time
from typing import List
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from config import BotState, GENRES, Source

def is_admin(user_id: int, admins: List[int]) -> bool:
    # Если список админов пустой — считаем всех администраторами (для теста)
    return (not admins) or (user_id in admins)

def fmt_duration(seconds: int) -> str:
    m, s = divmod(int(seconds or 0), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def progress_bar(percent: float, width: int = 20) -> str:
    percent = max(0.0, min(1.0, percent))
    filled = int(round(width * percent))
    return "█" * filled + "░" * (width - filled)

def get_menu_keyboard(state: BotState) -> InlineKeyboardMarkup:
    on = InlineKeyboardButton("▶️ Радио ON", callback_data="radio_on")
    off = InlineKeyboardButton("⏸ Радио OFF", callback_data="radio_off")
    nxt = InlineKeyboardButton("⏭ Пропустить", callback_data="next_track")
    src = InlineKeyboardButton(f"🔁 Источник: {state.source.value}", callback_data="source_switch")
    vote = InlineKeyboardButton("🗳 Голосование", callback_data="vote_now")

    rows = [
        [on, off, nxt],
        [src, vote],
    ]
    return InlineKeyboardMarkup(rows)

def format_status_message(state: BotState) -> str:
    rs = state.radio_status
    line1 = f"<b>Groove AI Radio</b> — источник: <b>{state.source.value}</b>"
    line2 = f"Статус радио: {'🟢 ВКЛ' if rs.is_on else '🔴 ВЫКЛ'}"
    line3 = f"Текущий жанр: <b>{rs.current_genre or '—'}</b>"
    line4 = "Трек: —"
    line5 = ""
    if rs.current_track:
        t = rs.current_track
        line4 = f"Трек: <b>{t.artist} — {t.title}</b> ({fmt_duration(t.duration)})"
        elapsed = time.time() - rs.last_played_time
        p = 0.0
        if t.duration:
            p = min(max(elapsed / float(t.duration), 0.0), 1.0)
        bar = progress_bar(p)
        line5 = f"{bar}  {int(p*100)}%"
    return "\n".join([line1, line2, line3, line4, line5]).strip()

def build_search_keyboard(titles: List[str]) -> InlineKeyboardMarkup:
    # Каждая кнопка — индекс трека
    buttons = []
    for idx, title in enumerate(titles):
        buttons.append([InlineKeyboardButton(f"{idx+1}. {title}", callback_data=f"pick:{idx}")])
    return InlineKeyboardMarkup(buttons)

def build_vote_keyboard(genres: List[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, g in enumerate(genres):
        row.append(InlineKeyboardButton(g, callback_data=f"vote:{g}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)
