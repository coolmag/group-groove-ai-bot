# main.py
import logging
import os
import asyncio
import json
import random
import shutil
from pathlib import Path
from typing import List, Optional
from collections import deque
from datetime import datetime
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from telegram.error import TelegramError
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_serializer, field_validator
from functools import wraps
from asyncio import Lock

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 10
    POLL_DURATION_SECONDS = 60  # 1 минута для голосования
    MAX_FILE_SIZE = 50_000_000
    MAX_DURATION = 1200
    MIN_DURATION = 60
    PLAYED_URLS_MEMORY = 200
    DOWNLOAD_TIMEOUT = 15
    DEFAULT_SOURCE = "soundcloud"
    PAUSE_BETWEEN_TRACKS = 1.5
    STATUS_UPDATE_INTERVAL = 10
    RETRY_INTERVAL = 3

# --- Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")

# --- Models ---
class NowPlaying(BaseModel):
    title: str
    duration: int
    url: str
    start_time: float = Field(default_factory=lambda: asyncio.get_event_loop().time())

class State(BaseModel):
    is_on: bool = False
    genre: str = "lo-fi hip hop"
    source: str = Constants.DEFAULT_SOURCE
    radio_playlist: deque[str] = Field(default_factory=deque)
    played_radio_urls: deque[str] = Field(default_factory=deque)
    active_poll_id: Optional[str] = None
    status_message_id: Optional[int] = None
    now_playing: Optional[NowPlaying] = None
    votable_genres: List[str] = Field(default_factory=lambda: [
        "pop", "rock", "hip hop", "electronic", "classical", "jazz", "blues", "country",
        "metal", "reggae", "folk", "indie", "rap", "r&b", "soul", "funk", "disco"
    ])

    @field_serializer('radio_playlist', 'played_radio_urls')
    def _serialize_deques(self, v: deque[str], _info):
        return list(v)

    @field_validator('radio_playlist', 'played_radio_urls', mode='before')
    @classmethod
    def _lists_to_deques(cls, v):
        return deque(v) if isinstance(v, list) else deque()

state_lock = Lock()
status_lock = Lock()

# --- State ---
def load_state() -> State:
    if CONFIG_FILE.exists():
        try:
            return State(**json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
        except Exception as e:
            logger.error(f"Config load error: {e}")
    return State()

async def save_state_from_botdata(bot_data: dict):
    async with state_lock:
        state: Optional[State] = bot_data.get('state')
        if state:
            CONFIG_FILE.write_text(state.model_dump_json(indent=4), encoding='utf-8')

# --- Utils ---
def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    s_int = int(seconds)
    return f"{s_int // 60:02d}:{s_int % 60:02d}"

def get_progress_bar(progress: float, width: int = 10) -> str:
    filled = int(width * progress)
    return "█" * filled + "▁" * (width - filled)

# --- Admin ---
async def is_admin(user_id: int) -> bool:
    logger.debug(f"Checking if user {user_id} is admin. Admin IDs: {ADMIN_IDS}")
    return user_id in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not await is_admin(user_id):
            logger.warning(f"User {user_id} attempted admin command but is not authorized")
            await update.effective_message.reply_text("Эта команда только для администраторов.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Music Sources ---
async def get_tracks_soundcloud(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"scsearch10:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
        return [{"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
                for e in info.get("entries", [])]
    except yt_dlp.YoutubeDLError as e:
        logger.error(f"SoundCloud search failed for genre {genre}: {e}")
        return []

async def get_tracks_youtube(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"ytsearch10:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
        return [{"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
                for e in info.get("entries", [])]
    except yt_dlp.YoutubeDLError as e:
        logger.error(f"YouTube search failed for genre {genre}: {e}")
        return []

# --- Playlist refill ---
async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    try:
        tracks = []
        if state.source == "soundcloud":
            tracks = await get_tracks_soundcloud(state.genre)
        if not tracks:
            logger.warning(f"No tracks found on {state.source}, trying YouTube")
            state.source = "youtube"
            tracks = await get_tracks_youtube(state.genre)
            if not tracks:
                logger.warning(f"No tracks found on YouTube for genre {state.genre}")
                await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось найти треки. Попробую снова.")
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                await refill_playlist(context)
                return

        filtered_tracks = [t for t in tracks if Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION]
        urls = [t["url"] for t in filtered_tracks if t["url"] not in state.played_radio_urls]
        if urls:
            random.shuffle(urls)
            state.radio_playlist.extend(urls)
            await save_state_from_botdata(context.bot_data)
            logger.info(f"Added {len(urls)} new tracks (filtered from {len(tracks)}).")
        else:
            logger.warning(f"No valid tracks found after filtering. Retrying in {Constants.RETRY_INTERVAL} seconds.")
            await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось найти треки. Попробую снова.")
            await asyncio.sleep(Constants.RETRY_INTERVAL)
            await refill_playlist(context)
    except Exception as e:
        logger.error(f"Playlist refill failed: {e}")

# --- Download & send ---
async def check_track_validity(url: str) -> Optional[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'simulate': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
        return {"url": url, "title": info.get("title", "Unknown"), "duration": info.get("duration", 0)}
    except Exception as e:
        logger.error(f"Failed to check track validity {url}: {e}")
        return None

async def download_and_send_to_chat(context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int):
    state: State = context.bot_data['state']
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True
    }
    try:
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info))
        file_size = filepath.stat().st_size
        if file_size > Constants.MAX_FILE_SIZE:
            logger.warning(f"Track {url} exceeds max file size: {file_size} bytes")
            await context.bot.send_message(chat_id, "⚠️ Трек слишком большой для отправки.")
            filepath.unlink(missing_ok=True)
            return
        with open(filepath, 'rb') as f:
            logger.debug(f"Sending audio to chat {chat_id}: {info.get('title', 'Unknown')}")
            await context.bot.send_audio(
                chat_id, f,
                title=info.get("title", "Unknown"),
                duration=int(info.get("duration", 0))
            )
        filepath.unlink(missing_ok=True)
    except asyncio.TimeoutError:
        logger.error(f"Download timeout for track {url}")
        await context.bot.send_message(chat_id, "⚠️ Время ожидания загрузки трека истекло.")
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, "⚠️ Не удалось обработать трек.")

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str):
    state: State = context.bot_data['state']
    track_info = await check_track_validity(url)
    if not track_info or not (Constants.MIN_DURATION <= track_info["duration"] <= Constants.MAX_DURATION):
        logger.warning(f"Track {url} is invalid or out of duration range")
        return

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True
    }
    try:
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info))
        file_size = filepath.stat().st_size
        if file_size > Constants.MAX_FILE_SIZE:
            logger.warning(f"Track {url} exceeds max file size: {file_size} bytes")
            await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Трек слишком большой для отправки.")
            filepath.unlink(missing_ok=True)
            return
        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown"),
            duration=int(info.get("duration", 0)),
            url=url
        )
        await update_status_panel(context)
        with open(filepath, 'rb') as f:
            logger.debug(f"Sending audio to chat {RADIO_CHAT_ID}: {state.now_playing.title}")
            await context.bot.send_audio(
                RADIO_CHAT_ID, f,
                title=state.now_playing.title,
                duration=state.now_playing.duration
            )
        filepath.unlink(missing_ok=True)
    except asyncio.TimeoutError:
        logger.error(f"Download timeout for track {url}")
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Время ожидания загрузки трека истекло.")
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}", exc_info=True)
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось обработать трек.")

# --- Radio loop ---
async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    """The main loop for the radio function."""
    await update_status_panel(context)
    while True:
        try:
            state: State = context.bot_data['state']
            if not state.is_on:
                await asyncio.sleep(10)
                continue
            if not state.radio_playlist:
                await refill_playlist(context)
                if not state.radio_playlist:
                    await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось найти треки. Попробую снова.")
                    await asyncio.sleep(Constants.RETRY_INTERVAL)
                    continue
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            logger.info(f"Playing track {url}")
            await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            
            context.bot_data['skip_event'].clear()
            sleep_duration = state.now_playing.duration if state.now_playing and state.now_playing.duration > 0 else Constants.TRACK_INTERVAL_SECONDS
            logger.info(f"Waiting for {sleep_duration} seconds for track.")
            try:
                await asyncio.wait_for(context.bot_data['skip_event'].wait(), timeout=sleep_duration)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(Constants.PAUSE_BETWEEN_TRACKS)
            logger.info(f"Paused for {Constants.PAUSE_BETWEEN_TRACKS} seconds between tracks.")
            
            if state.now_playing:
                elapsed = asyncio.get_event_loop().time() - state.now_playing.start_time
                if elapsed < state.now_playing.duration:
                    await update_status_panel(context)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"radio_loop error: {e}", exc_info=True)
            await asyncio.sleep(5)

# --- UI ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE):
    async with status_lock:
        state: State = context.bot_data['state']
        lines = [
            "🎵 *Радио Groove AI* 🎵",
            f"**Статус**: {'🟢 Включено' if state.is_on else '🔴 Выключено'}",
            f"**Жанр**: {state.genre.title()}",
            f"**Источник**: {state.source.title()}"
        ]
        if state.now_playing:
            elapsed = asyncio.get_event_loop().time() - state.now_playing.start_time
            progress = min(elapsed / state.now_playing.duration, 1.0) if state.now_playing.duration > 0 else 0
            progress_bar = get_progress_bar(progress)
            lines.append(f"**Сейчас играет**: {state.now_playing.title} ({format_duration(state.now_playing.duration)})")
            lines.append(f"**Прогресс**: {progress_bar} {int(progress * 100)}%")
        else:
            lines.append("**Сейчас играет**: Ожидание трека...")
        if state.active_poll_id:
            lines.append(f"🗳 *Голосование активно* (осталось ~{Constants.POLL_DURATION_SECONDS} сек)")
        lines.append("────────────────")
        text = "\n".join(lines)

        logger.debug(f"Updating status panel with text: {repr(text)}")

        if not text.strip():
            logger.error("Attempted to send empty status message!")
            return

        last_status_text = context.bot_data.get('last_status_text')
        if text == last_status_text:
            logger.debug("Status text unchanged, skipping update.")
            return

        keyboard = [
            [
                InlineKeyboardButton("🔄", callback_data="radio:refresh"),
                InlineKeyboardButton("⏭" if state.is_on else "▶️", callback_data="radio:skip" if state.is_on else "radio:on")
            ],
            [InlineKeyboardButton("🗳 Голосовать", callback_data="vote:start")] if state.is_on and not state.active_poll_id else [],
            [InlineKeyboardButton("⏹", callback_data="radio:off")] if state.is_on else []
        ]
        try:
            if state.status_message_id:
                logger.debug(f"Editing message {state.status_message_id} with text: {repr(text)}")
                await context.bot.edit_message_text(
                    chat_id=RADIO_CHAT_ID,
                    message_id=state.status_message_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
                    parse_mode="Markdown"
                )
            else:
                logger.debug(f"Sending new status message with text: {repr(text)}")
                msg = await context.bot.send_message(
                    RADIO_CHAT_ID,
                    text,
                    reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
                    parse_mode="Markdown"
                )
                state.status_message_id = msg.message_id
            context.bot_data['last_status_text'] = text
        except TelegramError as e:
            logger.warning(f"Failed to update status panel: {e}")
            if "Message to edit not found" in str(e):
                state.status_message_id = None
                await update_status_panel(context)
            elif "Message is not modified" in str(e):
                await asyncio.sleep(0.5)
            else:
                raise

# --- Commands ---
async def toggle_radio(context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    state.is_on = turn_on
    if turn_on:
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    else:
        task = context.bot_data.get('radio_loop_task')
        if task:
            task.cancel()
        state.now_playing = None
    await save_state_from_botdata(context.bot_data)

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    logger.debug(f"Received /{'ron' if turn_on else 'rof'} command from user {update.effective_user.id}")
    await toggle_radio(context, turn_on)
    await update_status_panel(context)
    message = "Радио включено. 🎵" if turn_on else "Радио выключено. 🔇"
    logger.debug(f"Sending message: {message}")
    await update.message.reply_text(message, parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🎵 *Привет! Я Groove AI Bot!* 🎵\n\n"
        "Я умею проигрывать музыку и запускать радио.\n"
        "- Используйте /play <название песни> для поиска треков.\n"
        "- Админы могут включать/выключать радио с /ron и /rof."
    )
    logger.debug(f"Sending start message: {message}")
    await update.message.reply_text(message, parse_mode="Markdown")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in ["soundcloud", "youtube"]:
        await update.message.reply_text("Использование: /source soundcloud|youtube")
        return
    state: State = context.bot_data['state']
    state.source = context.args[0]
    message = f"Источник переключен на: {state.source.title()}"
    logger.debug(f"Sending source message: {message}")
    await update.message.reply_text(message)
    await save_state_from_botdata(context.bot_data)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /play command from user {user_id}")
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите название песни.")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'🔍 Поиск "{query}"...')
    logger.info(f"Searching for '{query}' for user {user_id}")

    state: State = context.bot_data['state']
    search_prefix = "scsearch5" if state.source == "soundcloud" else "ytsearch5"
    ydl_opts = {
        'format': 'bestaudio',
        'noplaylist': True,
        'quiet': True,
        'default_search': search_prefix,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info.get('entries'):
                await message.edit_text("Треки не найдены. 😔")
                return

        keyboard = []
        for i, entry in enumerate(info['entries'][:5]):
            title = entry.get('title', 'Unknown Title')
            video_id = entry.get('id')
            keyboard.append([InlineKeyboardButton(f"▶️ {title}", callback_data=f"play_track:{video_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.edit_text('Выберите трек:', reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in /play search: {e}", exc_info=True)
        await message.edit_text("Произошла ошибка при поиске. 😔")

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    logger.debug(f"Received play button callback from user {user_id}: {query.data}")
    await query.answer()

    command, data = query.data.split(":", 1)

    if command == "play_track":
        video_id = data
        await query.edit_message_text(text=f"Обработка трека...")
        try:
            await download_and_send_to_chat(context, video_id, query.message.chat_id)
            await query.edit_message_text(text=f"Трек отправлен! 🎵")
        except Exception as e:
            logger.error(f"Failed to process play button callback: {e}", exc_info=True)
            await query.edit_message_text(f"Не удалось обработать трек: {e}")

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    logger.debug(f"Received radio button callback from user {user_id}: {query.data}")
    
    try:
        await query.answer()
    except TelegramError as e:
        logger.error(f"Failed to answer callback query: {e}")
        return

    command, data = query.data.split(":", 1)

    if command == "radio":
        if not await is_admin(user_id):
            logger.warning(f"User {user_id} attempted radio command but is not admin")
            await query.answer("Эта команда только для администраторов.", show_alert=True)
            return
        if data == "refresh":
            await update_status_panel(context)
            await query.answer("Статус обновлен.")
        elif data == "skip":
            await skip_track(context)
            await query.answer("Пропускаю трек...")
        elif data == "on":
            await toggle_radio(context, True)
            await update_status_panel(context)
            await query.answer("Радио включено. 🎵")
        elif data == "off":
            await toggle_radio(context, False)
            await update_status_panel(context)
            await query.answer("Радио выключено. 🔇")
    elif command == "vote":
        if not await is_admin(user_id):
            logger.warning(f"User {user_id} attempted vote command but is not admin")
            await query.answer("Эта команда только для администраторов.", show_alert=True)
            return
        if data == "start":
            await start_vote(context)
            await query.answer("Голосование запущено! 🗳")

async def skip_track(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.is_on:
        logger.debug("Skipping track")
        context.bot_data['skip_event'].set()

async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.active_poll_id:
        logger.debug("Poll already active, ignoring start_vote.")
        await context.bot.send_message(RADIO_CHAT_ID, "🗳 Голосование уже идет!")
        return

    if len(state.votable_genres) < 2:
        logger.debug("Not enough genres for voting.")
        await context.bot.send_message(RADIO_CHAT_ID, "Недостаточно жанров для голосования. 😔")
        return

    options = random.sample(state.votable_genres, min(len(state.votable_genres), 5))
    logger.debug(f"Starting poll with options: {options}")
    try:
        poll = await context.bot.send_poll(
            chat_id=RADIO_CHAT_ID,
            question="🎵 Выберите следующий жанр (голосование длится 1 минуту):",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=Constants.POLL_DURATION_SECONDS
        )
        state.active_poll_id = poll.poll.id
        logger.debug(f"Poll started with ID: {poll.poll.id}")
        await context.bot.send_message(RADIO_CHAT_ID, "🗳 Голосование началось! Выберите жанр выше.")
        await save_state_from_botdata(context.bot_data)
    except TelegramError as e:
        logger.error(f"Failed to start poll: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось запустить голосование.")

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the result of a poll."""
    state: State = context.bot_data['state']
    logger.debug(f"Received poll update: ID {update.poll.id}, active poll ID: {state.active_poll_id}, is_closed: {update.poll.is_closed}")
    
    if update.poll.id != state.active_poll_id:
        logger.debug(f"Ignoring poll update for ID {update.poll.id}, active poll ID is {state.active_poll_id}")
        return

    if not update.poll.is_closed:
        logger.debug(f"Poll {update.poll.id} is not closed yet, ignoring.")
        return

    logger.debug(f"Processing closed poll {update.poll.id}")
    max_votes = max(o.voter_count for o in update.poll.options)
    winning_options = [o.text for o in update.poll.options if o.voter_count == max_votes]
    
    if max_votes == 0:
        logger.debug("No votes in poll.")
        await context.bot.send_message(RADIO_CHAT_ID, "В голосовании никто не участвовал. 😔")
    else:
        selected_genre = random.choice(winning_options)
        state.genre = selected_genre
        state.radio_playlist.clear()
        logger.debug(f"Selected genre: {selected_genre}")
        await context.bot.send_message(RADIO_CHAT_ID, f"🎵 Новый жанр: *{state.genre.title()}*")
        if state.is_on and context.bot_data.get('radio_loop_task'):
            context.bot_data['radio_loop_task'].cancel()
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    
    state.active_poll_id = None
    await save_state_from_botdata(context.bot_data)

# --- Bot Lifecycle ---
async def post_init(application: Application):
    application.bot_data['state'] = load_state()
    application.bot_data['skip_event'] = asyncio.Event()
    if application.bot_data['state'].is_on:
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
    
    # Проверка и сброс webhook
    try:
        await application.bot.set_webhook("")
        logger.info("Webhook disabled, using polling mode")
    except TelegramError as e:
        logger.error(f"Failed to disable webhook: {e}")

async def on_shutdown(application: Application):
    task = application.bot_data.get('radio_loop_task')
    if task:
        task.cancel()
    await save_state_from_botdata(application.bot_data)

def main():
    """Starts the bot."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not BOT_TOKEN or not RADIO_CHAT_ID:
        logger.critical("BOT_TOKEN или RADIO_CHAT_ID не заданы!")
        return
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ron", lambda u, c: radio_on_off_command(u, c, True)))
    app.add_handler(CommandHandler("rof", lambda u, c: radio_on_off_command(u, c, False)))
    app.add_handler(CommandHandler("source", set_source_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CallbackQueryHandler(play_button_callback, pattern="^play_track:"))
    app.add_handler(CallbackQueryHandler(radio_buttons_callback, pattern="^(radio|vote):"))
    app.add_handler(PollHandler(handle_poll))
    logger.info("Starting bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
