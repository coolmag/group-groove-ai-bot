import time
from typing import List
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from config import BotState, GENRES, Source

def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

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
    return InlineKeyboardMarkup([[on, off, nxt], [src, vote]])

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
        p = min(max(elapsed / float(t.duration or 1), 0.0), 1.0)
        line5 = f"{progress_bar(p)}  {int(p*100)}%"
    return "\n".join([line1, line2, line3, line4, line5]).strip()

def build_search_keyboard(titles: List[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{i+1}. {t}", callback_data=f"pick:{i}")] for i, t in enumerate(titles)])

def build_vote_keyboard(genres: List[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, g in enumerate(genres):
        row.append(InlineKeyboardButton(g, callback_data=f"vote:{g}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def is_admin(user_id: int, admins: List[int]) -> bool:
    # Если список админов пустой — считаем всех администраторами (для теста/стендов)
    return (not admins) or (user_id in admins)
