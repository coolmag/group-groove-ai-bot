import time
from typing import List, Optional
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from config import RadioStatus

def is_admin(user_id: int, admins_env: Optional[str] = None) -> bool:
    if not admins_env:
        return False
    ids = {int(x) for x in admins_env.split(",") if x.strip().isdigit()}
    return user_id in ids

def get_menu_keyboard(state) -> InlineKeyboardMarkup:
    on = InlineKeyboardButton("▶️ Радио ON", callback_data="radio_on")
    off = InlineKeyboardButton("⏸ Радио OFF", callback_data="radio_off")
    nxt = InlineKeyboardButton("⏭ Пропустить", callback_data="next_track")
    src = InlineKeyboardButton(f"🔁 Источник: {state.source.value}", callback_data="source_switch")
    vote = InlineKeyboardButton("🗳 Голосование", callback_data="vote_now")
    return InlineKeyboardMarkup([[on, off, nxt], [src, vote]])

def format_status_message(state) -> str:
    rs: RadioStatus = state.radio_status
    line1 = f"<b>Groove AI Radio</b> — источник: <b>{state.source.value}</b>"
    line2 = f"Статус радио: {'🟢 ВКЛ' if rs.is_on else '🔴 ВЫКЛ'}"
    line3 = f"Текущий жанр: <b>{rs.current_genre or '—'}</b>"
    line4 = "Трек: —"
    line5 = ""
    if rs.current_track:
        t = rs.current_track
        dur = t.duration or 0
        elapsed = int(time.time() - rs.last_played_time)
        line4 = f"Трек: <b>{t.artist or '—'} — {t.title}</b> ({elapsed}s / {dur}s)"
    return "\n".join([line1, line2, line3, line4, line5]).strip()
