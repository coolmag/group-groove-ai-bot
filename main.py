# -*- coding: utf-8 -*-
import logging
import os
import asyncio
import json
import random
import shutil
import time
import re
import yt_dlp
from pathlib import Path
from typing import List, Optional, Deque
from collections import deque
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.error import BadRequest, TelegramError
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_serializer, field_validator
from functools import wraps
from asyncio import Lock

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 60
    POLL_DURATION_SECONDS = 10
    POLL_CHECK_TIMEOUT = 10
    MAX_FILE_SIZE = 50_000_000
    MAX_DURATION = 300
    MIN_DURATION = 30
    PLAYED_URLS_MEMORY = 100
    DOWNLOAD_TIMEOUT = 30
    DEFAULT_SOURCE = "soundcloud"
    DEFAULT_GENRE = "pop"
    PAUSE_BETWEEN_TRACKS = 0
    STATUS_UPDATE_INTERVAL = 10
    STATUS_UPDATE_MIN_INTERVAL = 2
    RETRY_INTERVAL = 30
    SEARCH_LIMIT = 50
    MAX_RETRIES = 3
    REFILL_THRESHOLD = 10

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or [482549032]
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", -1002892409779))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")
PORT = int(os.getenv("PORT", 8080))

# --- Models ---
class NowPlaying(BaseModel):
    title: str
    duration: int
    url: str
    start_time: float = Field(default_factory=time.time)

class State(BaseModel):
    is_on: bool = False
    genre: str = "lo-fi hip hop"
    source: str = Constants.DEFAULT_SOURCE
    radio_playlist: Deque[str] = Field(default_factory=deque)
    played_radio_urls: Deque[str] = Field(default_factory=deque)
    active_poll_id: Optional[str] = None
    poll_message_id: Optional[int] = None
    poll_options: List[str] = Field(default_factory=list)
    status_message_id: Optional[int] = None
    last_status_update: float = 0.0
    now_playing: Optional[NowPlaying] = None
    last_error: Optional[str] = None
    votable_genres: List[str] = Field(
        default_factory=lambda: sorted(list(set([
            # Old list
            "pop", "pop 80s", "pop 90s", "pop 2000s",
            "rock", "rock 60s", "rock 70s", "rock 80s", "rock 90s",
            "hip hop", "hip hop 90s", "hip hop 2000s",
            "electronic", "electronic 90s", "electronic 2000s",
            "classical", "classical 18th century", "classical 19th century",
            "jazz", "jazz 50s", "jazz 60s",
            "blues", "blues 50s", "blues 60s",
            "country", "country 80s", "country 90s",
            "metal", "metal 80s", "metal 90s",
            "reggae", "reggae 70s", "reggae 80s",
            "folk", "folk 60s", "folk 70s",
            "indie", "indie 90s", "indie 2000s",
            "rap", "rap 80s", "rap 90s", "rap 2000s",
            "r&b", "r&b 90s", "r&b 2000s",
            "soul", "soul 60s", "soul 70s",
            "funk", "funk 70s", "funk 80s",
            "disco", "disco 70s", "disco 80s",
            # New list
            "rock 'n' roll", "doo-wop", "folk rock",
            "psychedelic rock", "hard rock", "glam rock",
            "punk rock", "heavy metal", "hip-hop", "new wave",
            "synthpop", "house", "techno", "grunge", "britpop", "industrial rock",
            "gangsta rap", "trip-hop", "pop punk", "emo", "crunk", "dubstep",
            "electropop", "trap"
        ]))) 
    )
    retry_count: int = 0

    @field_serializer('radio_playlist', 'played_radio_urls')
    def _serialize_deques(self, v: Deque[str], _info):
        return list(v)

    @field_validator('radio_playlist', 'played_radio_urls', mode='before')
    @classmethod
    def _lists_to_deques(cls, v):
        return deque(v) if isinstance(v, list) else deque()

state_lock = Lock()
status_lock = Lock()
refill_lock = Lock()

# --- State ---
def load_state() -> State:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
                return State.model_validate(data)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return State()
    logger.info("No config file found, using default state")
    return State()

async def save_state_from_botdata(bot_data: dict):
    async with state_lock:
        state: Optional[State] = bot_data.get('state')
        if state:
            try:
                CONFIG_FILE.write_text(state.model_dump_json(indent=4))
                logger.debug("State saved to config file")
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

# --- Utils ---
def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    s_int = int(seconds)
    return f"{s_int // 60:02d}:{s_int % 60:02d}"

def get_progress_bar(progress: float, width: int = 10) -> str:
    filled = int(width * progress)
    return "█" * filled + " " * (width - filled)

def escape_markdown_v2(text: str) -> str:
    if not isinstance(text, str) or not text:
        return ""
    text = text.replace('_', '\\_')
    text = text.replace('*', '\\*')
    text = text.replace('[', '\\[')
    text = text.replace(']', '\\]')
    text = text.replace('(', '\\(')
    text = text.replace(')', '\\)')
    text = text.replace('~', '\\~')
    text = text.replace('`', '\\`')
    text = text.replace('>', '\\>')
    text = text.replace('#', '\\#')
    text = text.replace('+', '\\+')
    text = text.replace('-', '\\-')
    text = text.replace('=', '\\=')
    text = text.replace('|', '\\|')
    text = text.replace('{', '\\{')
    text = text.replace('}', '\\}')
    text = text.replace('.', '\\.')
    text = text.replace('!', '\\!')
    return text

def set_escaped_error(state: State, error: str):
    state.last_error = escape_markdown_v2(error) if error else None

# --- Admin ---
async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not await is_admin(user_id):
            state: State = context.bot_data['state']
            set_escaped_error(state, "Unauthorized access attempt")
            if update.message:
                await update.message.reply_text("This command is for admins only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Music Sources ---
async def get_tracks_soundcloud(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"scsearch{Constants.SEARCH_LIMIT}:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, f"scsearch{Constants.SEARCH_LIMIT}:{genre}", download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", []) if e
        ]
        logger.info(f"Found {len(tracks)} SoundCloud tracks for '{genre}'")
        return tracks
    except Exception as e:
        logger.error(f"SoundCloud search failed for '{genre}': {e}")
        return []

async def get_tracks_youtube(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"ytsearch{Constants.SEARCH_LIMIT}:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, f"ytsearch{Constants.SEARCH_LIMIT}:{genre}", download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", []) if e
        ]
        logger.info(f"Found {len(tracks)} YouTube tracks for '{genre}'")
        return tracks
    except Exception as e:
        logger.error(f"YouTube search failed for '{genre}': {e}")
        return []

# --- Playlist refill ---
async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    
    if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY * 0.5:
        state.played_radio_urls.clear()
        logger.debug("Cleared played URLs to manage memory")

    async def attempt_refill(source: str, genre: str) -> List[dict]:
        return await get_tracks_soundcloud(genre) if source == "soundcloud" else await get_tracks_youtube(genre)

    original_genre, original_source = state.genre, state.source
    for attempt in range(Constants.MAX_RETRIES):
        try:
            tracks = await attempt_refill(state.source, state.genre)
            if not tracks:
                logger.warning(f"No tracks found on {state.source} for genre {state.genre}, attempt {attempt + 1}")
                set_escaped_error(state, f"No tracks found on {state.source} for genre {state.genre}")
                await context.bot.send_message(RADIO_CHAT_ID, f"[WARN] No tracks found on {state.source} for genre {state.genre}. Retrying ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                
                # Try alternative source on first failure
                if state.source == "soundcloud" and attempt == 0:
                    state.source = "youtube"
                # Reset to defaults on final failure
                elif attempt == Constants.MAX_RETRIES - 1:
                    state.genre = Constants.DEFAULT_GENRE
                    state.source = Constants.DEFAULT_SOURCE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            filtered_tracks = [
                t for t in tracks
                if t.get("duration") and Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION
                and t["url"] not in state.played_radio_urls
            ]
            
            if not filtered_tracks:
                logger.warning(f"No valid tracks after filtering on {state.source}")
                set_escaped_error(state, f"No valid tracks after filtering on {state.source}")
                await context.bot.send_message(RADIO_CHAT_ID, f"[WARN] No valid tracks after filtering on {state.source}. Retrying ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                state.played_radio_urls.clear()
                
                if state.source == "soundcloud" and attempt == 0:
                    state.source = "youtube"
                elif attempt == Constants.MAX_RETRIES - 1:
                    state.genre = Constants.DEFAULT_GENRE
                    state.source = Constants.DEFAULT_SOURCE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            urls = [t["url"] for t in filtered_tracks]
            random.shuffle(urls)
            state.radio_playlist.extend(urls)
            state.retry_count = 0
            state.genre = original_genre
            state.source = original_source
            logger.info(f"Added {len(urls)} tracks to playlist")
            await save_state_from_botdata(context.bot_data)
            return
            
        except Exception as e:
            logger.error(f"Playlist refill failed, attempt {attempt + 1}: {e}")
            set_escaped_error(state, f"Playlist refill error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Playlist refill error: {e}")
            state.retry_count += 1
            await asyncio.sleep(Constants.RETRY_INTERVAL)

    logger.error(f"Failed to refill playlist after {Constants.MAX_RETRIES} attempts")
    state.source = Constants.DEFAULT_SOURCE
    state.genre = Constants.DEFAULT_GENRE
    set_escaped_error(state, f"Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to {state.source}/{state.genre}.")
    await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to {state.source}/{state.genre}.")
    await save_state_from_botdata(context.bot_data)

async def _refill_playlist_if_needed(context: ContextTypes.DEFAULT_TYPE):
    """Checks if the playlist is running low and refills it in the background."""
    state: State = context.bot_data['state']
    if len(state.radio_playlist) < Constants.REFILL_THRESHOLD:
        if refill_lock.locked():
            logger.info("Refill is already in progress.")
            return
        
        async with refill_lock:
            logger.info("Playlist running low, starting background refill...")
            await refill_playlist(context)
            logger.info("Background playlist refill complete.")

# --- Download & send ---
async def check_track_validity(url: str) -> Optional[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'simulate': True
    }
    if "youtube.com" in url or "youtu.be" in url:
        if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
        return {
            "url": url,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0)
        }
    except Exception as e:
        logger.error(f"Track validity check failed for {url}: {e}")
        return None

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str) -> int:
    state: State = context.bot_data['state']
    track_info = await check_track_validity(url)
    if not track_info:
        set_escaped_error(state, "Invalid track URL")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Invalid track URL.")
        state.now_playing = None
        await update_status_panel(context, force=True)
        return 0
    
    duration = track_info.get("duration", 0)
    if not (Constants.MIN_DURATION <= duration <= Constants.MAX_DURATION):
        set_escaped_error(state, f"Duration out of range ({duration}s)")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Track duration out of range ({duration}s).")
        state.now_playing = None
        await update_status_panel(context, force=True)
        return 0

    DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        set_escaped_error(state, "Download directory not writable")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Download directory not writable.")
        state.now_playing = None
        await update_status_panel(context, force=True)
        return 0

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'ffmpeg_location': shutil.which("ffmpeg"),
        'ffprobe_location': shutil.which("ffprobe")
    }
    
    if "youtube.com" in url or "youtu.be" in url:
        if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES

    filepath = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        
        filepath_mp3 = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
        filepath_m4a = Path(ydl.prepare_filename(info)).with_suffix('.m4a')

        if filepath_mp3.exists():
            filepath = filepath_mp3
        elif filepath_m4a.exists():
            filepath = filepath_m4a
        else:
            # Fallback to m4a if mp3 failed
            ydl_opts['postprocessors'][0]['preferredcodec'] = 'm4a'
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filepath = Path(ydl.prepare_filename(info)).with_suffix('.m4a')

        if not filepath or not filepath.exists():
            set_escaped_error(state, "Failed to download track")
            await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Failed to download track.")
            state.now_playing = None
            await update_status_panel(context, force=True)
            return 0

        if filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            set_escaped_error(state, "Track exceeds max file size")
            await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Track too large to send.")
            state.now_playing = None
            await update_status_panel(context, force=True)
            return 0

        track_duration = int(info.get("duration", 0))
        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown Track"),
            duration=track_duration,
            url=url
        )
        await update_status_panel(context, force=True)
        
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                chat_id=RADIO_CHAT_ID,
                audio=f,
                title=state.now_playing.title,
                duration=state.now_playing.duration,
                performer=info.get("uploader", "Unknown Artist")
            )
        logger.info(f"Sent track: {state.now_playing.title}")
        return track_duration
        
    except asyncio.TimeoutError:
        set_escaped_error(state, "Track download timeout")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Track download timed out.")
        return 0
    except TelegramError as e:
        set_escaped_error(state, f"Telegram error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Telegram error: {e}")
        return 0
    except Exception as e:
        set_escaped_error(state, f"Track processing error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Track processing error: {e}")
        return 0
    finally:
        state.now_playing = None
        await update_status_panel(context, force=True)
        if filepath and filepath.exists():
            try:
                filepath.unlink(missing_ok=True)
            except Exception:
                pass

# --- Radio loop ---
async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info("Starting radio loop")
    
    while True:
        try:
            if not state.is_on:
                logger.info("Radio is off, sleeping")
                await asyncio.sleep(10)
                continue

            # Proactively refill playlist
            asyncio.create_task(_refill_playlist_if_needed(context))
                
            if not state.radio_playlist:
                logger.warning("Playlist is empty, waiting for refill...")
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue
            
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            
            logger.info(f"Playing track: {url}")
            track_duration = await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            
            # Wait for the track duration plus the configured pause
            # Ensure track_duration is a valid number before sleeping
            sleep_time = (track_duration or 0) + Constants.PAUSE_BETWEEN_TRACKS
            
            logger.debug(f"Waiting for {sleep_time} seconds until next track")
            await asyncio.sleep(sleep_time)
            
        except asyncio.CancelledError:
            logger.info("Radio loop cancelled")
            return
        except Exception as e:
            logger.error(f"Radio loop error: {e}")
            set_escaped_error(state, f"Radio loop error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Radio error: {e}")
            await asyncio.sleep(10)

# --- UI ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    async with status_lock:
        state: State = context.bot_data['state']
        current_time = time.time()
        
        # Throttle updates
        if not force and current_time - state.last_status_update < Constants.STATUS_UPDATE_MIN_INTERVAL:
            return

        # Prepare status text
        status_icon = '\U0001F7E2 ON' if state.is_on else '\U0001F534 OFF'
        status_lines = [
            f"\U0001F3B5 *Radio Groove AI* \U0001F3B5",
            f"**Status**: {status_icon}",
            f"**Genre**: {escape_markdown_v2(state.genre.title())}",
            f"**Source**: {escape_markdown_v2(state.source.title())}"
        ]
        
        if state.now_playing:
            elapsed = current_time - state.now_playing.start_time
            progress = min(elapsed / state.now_playing.duration, 1.0)
            progress_bar = get_progress_bar(progress)
            duration = format_duration(state.now_playing.duration)
            status_lines.append(f"**Now Playing**: {escape_markdown_v2(state.now_playing.title)}")
            status_lines.append(f"**Progress**: {progress_bar} {int(progress * 100)}%")
        else:
            status_lines.append("**Now Playing**: _Idle_")
            
        if state.active_poll_id:
            status_lines.append(f"\U0001F4DC **Active Poll** (ends in ~{Constants.POLL_DURATION_SECONDS} sec)")
            
        if state.last_error:
            status_lines.append(f"\U000026A0\U0000FE0F **Last Error**: {state.last_error}")
            
        status_text = "\n".join(status_lines)
        
        # Prepare keyboard
        start_skip_text = f'\u23ED\ufe0f Skip' if state.is_on else f'\u25B6\ufe0f Start'
        keyboard = []
        keyboard.append([
            InlineKeyboardButton("\U0001F504 Refresh", callback_data="radio:refresh"),
            InlineKeyboardButton(start_skip_text, callback_data="radio:skip" if state.is_on else "radio:on")
        ])
        
        if state.is_on and not state.active_poll_id:
            keyboard.append([InlineKeyboardButton("\U0001F4DC Vote", callback_data="vote:start")])
            
        if state.is_on:
            keyboard.append([InlineKeyboardButton("\u23F9\ufe0f Stop", callback_data="radio:off")])
            
        keyboard.append([InlineKeyboardButton("\U0001F4CB Menu", callback_data="cmd:menu")])
        
        try:
            # Delete the old message to prevent clutter
            if state.status_message_id:
                try:
                    await context.bot.delete_message(RADIO_CHAT_ID, state.status_message_id)
                except TelegramError as e:
                    logger.warning(f"Could not delete old status message: {e}")
            
            # Send a new message
            msg = await context.bot.send_message(
                chat_id=RADIO_CHAT_ID,
                text=status_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2"
            )
            state.status_message_id = msg.message_id
            state.last_status_update = current_time
            
        except Exception as e:
            logger.error(f"Status update failed: {e}")
            state.status_message_id = None # Clear message ID on failure
            try:
                # Fallback without markdown
                await context.bot.send_message(
                    RADIO_CHAT_ID,
                    re.sub(r'[*_`]', '', status_text),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as final_e:
                logger.error(f"Complete failure in status update: {final_e}")


# --- Commands ---
async def _start_radio_logic(context: ContextTypes.DEFAULT_TYPE):
    """The core logic for starting the radio, designed to be run in the background."""
    state: State = context.bot_data['state']
    
    # Ensure previous task is cancelled if it exists
    if 'radio_loop_task' in context.bot_data and not context.bot_data['radio_loop_task'].done():
        context.bot_data['radio_loop_task'].cancel()
        try:
            await context.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass # This is expected

    # Clear old playlist data
    state.now_playing = None
    state.radio_playlist.clear()
    state.played_radio_urls.clear()
    
    # Start the main radio loop
    context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    
    # Initial playlist refill
    await refill_playlist(context)
    await update_status_panel(context, force=True)

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    
    if turn_on:
        if state.is_on:
            await update.message.reply_text("Radio is already running!")
            return
        state.is_on = True
        await update.message.reply_text(f"\U0001F680 Radio starting... Searching for music.")
        asyncio.create_task(_start_radio_logic(context))
    else:
        if not state.is_on:
            await update.message.reply_text("Radio is already stopped!")
            return
        state.is_on = False
        if 'radio_loop_task' in context.bot_data:
            context.bot_data['radio_loop_task'].cancel()
            try:
                await context.bot_data['radio_loop_task']
            except asyncio.CancelledError:
                pass
            del context.bot_data['radio_loop_task']
            
        state.now_playing = None
        state.radio_playlist.clear()
        await update.message.reply_text("\U0001F517 Radio stopped!")
        
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    state.is_on = False
    
    # Cancel radio task
    if 'radio_loop_task' in context.bot_data:
        context.bot_data['radio_loop_task'].cancel()
        try:
            await context.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass
        del context.bot_data['radio_loop_task']
    
    await update.message.reply_text("\U0001F6D1 Bot stopping...")
    await save_state_from_botdata(context.bot_data)
    
    # Schedule shutdown
    asyncio.create_task(context.application.stop())

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    state.now_playing = None
    await update.message.reply_text("\u23ED\ufe0f Skipping current track...")
    await update_status_panel(context, force=True)
    await save_state_from_botdata(context.bot_data)

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_vote(context)
    await update.message.reply_text("\U0001F4DC Starting genre vote...")

@admin_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context, force=True)
    await update.message.reply_text("\U0001F504 Status refreshed!")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    
    if not context.args:
        await update.message.reply_text("Usage: /source soundcloud|youtube")
        return
        
    new_source = context.args[0].lower()
    if new_source not in ["soundcloud", "youtube"]:
        await update.message.reply_text("Invalid source. Use 'soundcloud' or 'youtube'")
        return
        
    state.source = new_source
    state.radio_playlist.clear()
    state.now_playing = None
    state.retry_count = 0
    
    await refill_playlist(context)
    await update.message.reply_text(f"Source switched to: {new_source.title()}")
    await save_state_from_botdata(context.bot_data)

@admin_only
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the config file to reset the bot's state."""
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
            await update.message.reply_text(
                "\u2705 State file (radio_config.json) deleted. "
                "Restarting the bot to apply default settings..."
            )
            # Gracefully stop the application to allow Railway to restart it.
            asyncio.create_task(context.application.stop())
        except Exception as e:
            await update.message.reply_text(f"\u274C Could not delete state file: {e}")
    else:
        await update.message.reply_text("No state file to delete.")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please specify a song title. Usage: /play <song title>")
        return
        
    query = " ".join(context.args)
    state: State = context.bot_data['state']
    message = await update.message.reply_text(f'\U0001F50D Searching for "{query}"...')

    try:
        # Determine search prefix based on current source
        search_prefix = "scsearch10" if state.source == "soundcloud" else "ytsearch10"
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'default_search': search_prefix,
            'extract_flat': True
        }
        
        if state.source == "youtube" and YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
            
        if not info or 'entries' not in info or not info['entries']:
            await message.edit_text("No tracks found. \U0001F614")
            return
            
        tracks = []
        for entry in info['entries']:
            if not entry:
                continue
            tracks.append({
                "url": entry['url'],
                "title": entry.get('title', 'Unknown Track'),
                "duration": entry.get('duration', 0)
            })
        
        if not tracks:
            await message.edit_text("No tracks found. \U0001F614")
            return
            
        # Create keyboard with track options
        keyboard = []
        for track in tracks[:5]:  # Show max 5 results
            title = track['title'][:30] + "..." if len(track['title']) > 30 else track['title']
            duration = format_duration(track['duration'])
            keyboard.append([InlineKeyboardButton(
                f"\u25B6\ufe0f {title} ({duration})",
                callback_data=f"play_track:{track['url']}"
            )])
            
        await message.edit_text(
            "Select a track:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.edit_text(f"[ERR] Search failed: {e}")

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("play_track:"):
        return
        
    url = query.data.split(":", 1)[1]
    await query.edit_message_text("\u2B07\ufe0f Downloading track...")
    
    try:
        # Download and send the track
        state: State = context.bot_data['state']
        track_info = await check_track_validity(url)
        
        if not track_info:
            await query.edit_message_text("[ERR] Invalid track URL")
            return
            
        DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
            'quiet': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        }
        
        if "youtube.com" in url or "youtu.be" in url:
            if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
                ydl_opts['cookiefile'] = YOUTUBE_COOKIES
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
            
            if not filepath.exists():
                filepath = Path(ydl.prepare_filename(info)).with_suffix('.m4a')
                
            with open(filepath, 'rb') as f:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    title=info.get('title', 'Unknown Track'),
                    duration=info.get('duration', 0),
                    performer=info.get('uploader', 'Unknown Artist')
                )
                
        await query.edit_message_text("\u2705 Track sent!")
        
    except Exception as e:
        logger.error(f"Track download failed: {e}")
        await query.edit_message_text(f"[ERR] Failed to download track: {e}")
        
    finally:
        # Clean up
        if 'filepath' in locals() and filepath.exists():
            try:
                filepath.unlink()
            except Exception:
                pass

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        command, action = query.data.split(":", 1)
    except ValueError:
        await query.answer() # Answer silently
        return
        
    state: State = context.bot_data['state']
    
    if command == "radio":
        if not await is_admin(query.from_user.id):
            await query.answer("Admin only command.", show_alert=True)
            return
            
        if action == "refresh":
            await query.answer()
            await update_status_panel(context, force=True)
            
        elif action == "skip":
            await query.answer("Skipping track...")
            state.now_playing = None
            await update_status_panel(context, force=True)
            
        elif action == "on":
            if state.is_on:
                await query.answer("Radio is already running!")
                return
            state.is_on = True
            await query.answer("Radio starting...")
            await context.bot.send_message(RADIO_CHAT_ID, "\U0001F680 Radio starting... Searching for music.")
            asyncio.create_task(_start_radio_logic(context))
            
        elif action == "off":
            if not state.is_on:
                await query.answer("Radio is already stopped!")
                return
            state.is_on = False
            if 'radio_loop_task' in context.bot_data:
                context.bot_data['radio_loop_task'].cancel()
                try:
                    await context.bot_data['radio_loop_task']
                except asyncio.CancelledError:
                    pass
                del context.bot_data['radio_loop_task']
            await update_status_panel(context, force=True)
            await query.answer("\U0001F517 Radio stopped!")
            
    elif command == "vote":
        if not await is_admin(query.from_user.id):
            await query.answer("Admin only command.", show_alert=True)
            return
            
        if action == "start":
            await query.answer()
            await start_vote(context)
            
    elif command == "cmd":
        await query.answer()
        await show_menu(update, context)

async def tally_vote(context: ContextTypes.DEFAULT_TYPE):
    """
    Called by a job to end the poll, tally votes, and change the genre.
    """
    job = context.job
    state: State = context.bot_data['state']

    # Ensure this job is for the currently active poll
    if not state.active_poll_id or state.poll_message_id != job.data['poll_message_id']:
        logger.warning("Tally job running for an outdated or invalid poll. Ignoring.")
        return

    try:
        # Stop the poll and get the final results
        stopped_poll = await context.bot.stop_poll(job.data['chat_id'], job.data['poll_message_id'])
        logger.info(f"Stopped poll {stopped_poll.id} with results: {[o.voter_count for o in stopped_poll.options]}")
    except Exception as e:
        logger.error(f"Could not stop poll: {e}")
        # Fallback or retry could be implemented here if needed
        return
    finally:
        # Always clean up poll state
        state.active_poll_id = None
        state.poll_message_id = None

    # Find winning option
    max_votes = max(option.voter_count for option in stopped_poll.options)
    
    # Check if anyone voted
    if max_votes == 0:
        await context.bot.send_message(job.data['chat_id'], "No votes received. Keeping current genre.")
        state.poll_options = []
        await save_state_from_botdata(context.bot_data)
        await update_status_panel(context, force=True)
        return

    winning_options = [i for i, option in enumerate(stopped_poll.options) if option.voter_count == max_votes]
    
    # Select random winner if tie
    winner_idx = random.choice(winning_options)
    new_genre = state.poll_options[winner_idx]
    state.genre = new_genre.lower() # Store genre in lowercase
    state.radio_playlist.clear()
    
    # Announce the winner
    await context.bot.send_message(
        job.data['chat_id'],
        f"\U0001F3C6 Vote finished! New genre: *{escape_markdown_v2(new_genre)}*",
        parse_mode="MarkdownV2"
    )
    
    # Clean up state and refill playlist
    state.poll_options = []
    logger.info(f"Genre changed to '{new_genre}'. Refilling playlist.")
    await refill_playlist(context)
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    
    if state.active_poll_id:
        await context.bot.send_message(RADIO_CHAT_ID, "\U0001F4DC There's already an active poll!")
        return
        
    if len(state.votable_genres) < 4: # Ensure we have enough genres for the poll
        await context.bot.send_message(RADIO_CHAT_ID, "[WARN] Not enough genres available for voting.")
        return
        
    # Select 4 random genres
    options = random.sample(state.votable_genres, 4)
    
    try:
        message = await context.bot.send_poll(
            chat_id=RADIO_CHAT_ID,
            question="\U0001F3B5 Choose the next music genre:",
            options=[g.title() for g in options],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=Constants.POLL_DURATION_SECONDS
        )
        
        # Save poll state
        state.active_poll_id = message.poll.id
        state.poll_message_id = message.message_id
        state.poll_options = [g.title() for g in options] # Store the exact options sent
        
        # Schedule the tally job
        context.application.job_queue.run_once(
            tally_vote, 
            Constants.POLL_DURATION_SECONDS, 
            data={'poll_message_id': message.message_id, 'chat_id': RADIO_CHAT_ID},
            name=f"vote_{message.poll.id}"
        )
        
        logger.info(f"Started poll {message.poll.id}, job scheduled for {Constants.POLL_DURATION_SECONDS} seconds.")
        await save_state_from_botdata(context.bot_data)
        
    except Exception as e:
        logger.error(f"Failed to start vote: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Failed to start vote: {e}")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    is_admin_user = await is_admin(update.effective_user.id)
    
    status_icon = '\U0001F7E2 ON' if state.is_on else '\U0001F534 OFF'
    menu_text = [
        f"\U0001F3B5 *Groove AI Radio* \U0001F3B5",
        f"**Status**: {status_icon}",
        f"**Genre**: {escape_markdown_v2(state.genre.title())}",
        f"**Source**: {escape_markdown_v2(state.source.title())}",
        f"**Now Playing**: {escape_markdown_v2(state.now_playing.title if state.now_playing else 'None')}",
        "",
        f"\U0001F4BF *Commands*:",
        "/play <query> - Search and play a track",
        "/menu - Show this menu",
    ]
    
    if is_admin_user:
        menu_text.extend([
            "",
            f"\U0001F451 *Admin Commands*:",
            "/ron - Start radio",
            "/roff - Stop radio",
            "/skip - Skip current track",
            "/vote - Start genre vote",
            "/source <sc|yt> - Change source",
            "/refresh - Update status",
            "/stopbot - Stop the bot",
        ])
    
    start_skip_text = f'\u23ED\ufe0f Skip' if state.is_on else f'\u25B6\ufe0f Start'
    keyboard = [
        [InlineKeyboardButton("\U0001F3B5 Play Track", callback_data="cmd:play")],
        [InlineKeyboardButton("\U0001F4CB Menu", callback_data="cmd:menu")]
    ]
    
    if is_admin_user:
        keyboard.insert(0, [
            InlineKeyboardButton(start_skip_text, callback_data="radio:skip" if state.is_on else "radio:on"),
            InlineKeyboardButton("\u23F9\ufe0f Stop", callback_data="radio:off")
        ])
        keyboard.insert(1, [
            InlineKeyboardButton("\U0001F504 Refresh", callback_data="radio:refresh"),
            InlineKeyboardButton("\U0001F4DC Vote", callback_data="vote:start")
        ])
    
    full_text = "\n".join(menu_text)
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        query = update.callback_query
        if query:
            # If called from a button, edit the message
            await query.edit_message_text(
                full_text,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2"
            )
        elif update.message:
            # If called from a command, reply to the message
            await update.message.reply_text(
                full_text,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2"
            )
    except Exception:
        # Fallback for any error, including Markdown issues
        fallback_text = re.sub(r'[*_`]', '', full_text)
        if 'query' in locals() and query:
            await query.edit_message_text(fallback_text, reply_markup=reply_markup)
        elif update.message:
            await update.message.reply_text(fallback_text, reply_markup=reply_markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

# --- Health Check Endpoint ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log Errors caused by Updates."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def health_check(request):
    return web.Response(text="Bot is running", status=200)

# --- Bot Lifecycle ---
async def check_bot_permissions(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the bot is an administrator in the radio chat."""
    try:
        bot_id = context.bot.id
        chat_member = await context.bot.get_chat_member(RADIO_CHAT_ID, bot_id)
        logger.info(f"DEBUG: Received chat_member object: {chat_member}")

        if chat_member.status == "administrator":
            logger.info("Bot is an administrator. Permission check passed.")
            # Optional: Check for can_manage_messages for status panel cleanup
            if not getattr(chat_member, 'can_manage_messages', False):
                logger.warning("Bot is admin but lacks 'can_manage_messages'. Status panel deletion might fail.")
            return True
        else:
            logger.error(f"Bot is not an administrator in chat {RADIO_CHAT_ID}. Current status: {chat_member.status}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Bot is not an administrator. Current status: {chat_member.status}. Please grant admin rights.")
            return False

    except Exception as e:
        logger.error(f"Unexpected error during permission check for chat {RADIO_CHAT_ID}: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Unexpected error during permission check: {e}")
        return False

async def post_init(application: Application):
    logger.info("Initializing bot...")
    
    # Load state
    application.bot_data['state'] = load_state()
    state: State = application.bot_data['state']
    
    # Set bot commands
    logger.info("Setting bot commands...")
    commands = [
        BotCommand("play", "Найти и воспроизвести трек"),
        BotCommand("menu", "Показать главное меню"),
        BotCommand("ron", "Включить радио (админ)"),
        BotCommand("roff", "Выключить радио (админ)"),
        BotCommand("skip", "Пропустить трек (админ)"),
        BotCommand("vote", "Голосование за жанр (админ)"),
        BotCommand("source", "Сменить источник: /source youtube (админ)"),
        BotCommand("refresh", "Обновить статус (админ)"),
        BotCommand("reset", "Сбросить состояние бота (админ)"),
    ]
    await application.bot.set_my_commands(commands)
    
    # Check dependencies
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.error("FFmpeg not found!")
        state.last_error = "FFmpeg or ffprobe not installed"
        await application.bot.send_message(RADIO_CHAT_ID, "[ERR] FFmpeg not installed!")
        return
        
    # 1. Check Privacy Mode first - this is the most common and blocking issue.
    try:
        bot_info = await application.bot.get_me()
        # Note: can_read_all_group_messages is False when privacy is ON. We want it to be True.
        if getattr(bot_info, 'can_read_all_group_messages', True) is False:
            logger.error("Privacy mode is enabled. Bot will not receive poll answers.")
            await application.bot.send_message(
                RADIO_CHAT_ID,
                "[ERR] **Critical Error: Privacy Mode is enabled.**\n\n" 
                "The bot cannot receive poll answers or most messages from users.\n" 
                "Please disable it via @BotFather:\n" 
                "1. Open @BotFather\n" 
                "2. Select your bot (`@Aigrooves_bot`)\n" 
                "3. Go to `Bot Settings` -> `Group Privacy`\n" 
                "4. Click '***Turn off***'.\n\n" 
                "After turning it off, please **restart the bot** on the hosting.",
                parse_mode="MarkdownV2"
            )
            return # Stop initialization if privacy is on
    except Exception as e:
        logger.error(f"Could not check privacy mode: {e}")
        # Continue anyway, but this is a bad sign

    # 2. If privacy is off, check for specific admin rights.
    if not await check_bot_permissions(application):
        logger.error("Permission check failed! See chat for details.")
        state.last_error = "Bot lacks required permissions"
        return # Stop initialization if permissions are missing
        
    # Clear any stale poll from a previous run
    if state.active_poll_id:
        logger.warning("Active poll found in state on startup. Resetting due to possible bot restart.")
        state.active_poll_id = None
        state.poll_message_id = None
        state.poll_options = []

    # Start radio if enabled
    if state.is_on:
        logger.info("Starting radio loop")
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
        await refill_playlist(application)
        
    logger.info("Bot initialized successfully")

async def on_shutdown(application: Application):
    logger.info("Shutting down bot...")
    
    # Save state
    if 'state' in application.bot_data:
        try:
            CONFIG_FILE.write_text(application.bot_data['state'].model_dump_json(indent=4))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
    # Stop radio loop
    if 'radio_loop_task' in application.bot_data:
        application.bot_data['radio_loop_task'].cancel()
        try:
            await application.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass
    
    logger.info("Shutdown complete")

def main():
    # Validate environment
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set!")
    if not ADMIN_IDS:
        raise ValueError("ADMIN_IDS not set!")
    if not RADIO_CHAT_ID:
        raise ValueError("RADIO_CHAT_ID not set!")
    
    # Ensure download directory exists
    DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
    
    # Create application
    app = Application.builder()         .token(BOT_TOKEN)         .post_init(post_init)         .post_shutdown(on_shutdown)         .build()
    
    # Register handlers
    app.add_handler(CommandHandler(["start", "menu", "m"], start_command))
    app.add_handler(CommandHandler(["ron", "r_on"], lambda u, c: radio_on_off_command(u, c, True)))
    app.add_handler(CommandHandler(["rof", "r_off", "stop", "t"], lambda u, c: radio_on_off_command(u, c, False)))
    app.add_handler(CommandHandler("stopbot", stop_bot_command))
    app.add_handler(CommandHandler(["skip", "s"], skip_command))
    app.add_handler(CommandHandler(["vote", "v"], vote_command))
    app.add_handler(CommandHandler(["refresh", "r"], refresh_command))
    app.add_handler(CommandHandler(["source", "src"], set_source_command))
    app.add_handler(CommandHandler(["reset"], reset_command))
    app.add_handler(CommandHandler(["play", "p"], play_command))
    
    app.add_handler(CallbackQueryHandler(play_button_callback, pattern=r"^play_track:"))
    app.add_handler(CallbackQueryHandler(radio_buttons_callback, pattern=r"^(radio|vote|cmd):"))
    
    app.add_error_handler(error_handler)
    
    # Create health check server
    async def run_server():
        app_web = web.Application()
        app_web.router.add_get("/", health_check)
        runner = web.AppRunner(app_web)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"Health check server running on port {PORT}")
    
    # Run bot and health server
    loop = asyncio.get_event_loop()
    loop.create_task(run_server())
    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()