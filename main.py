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
import re
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, PollAnswer
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollHandler,
    PollAnswerHandler,
)
from telegram.error import TelegramError, BadRequest, RetryAfter
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from functools import wraps
from asyncio import Lock

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 60
    POLL_DURATION_SECONDS = 60
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

# --- Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")

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
    poll_message_id: Optional[int] = None
    poll_options: List[str] = Field(default_factory=list)
    poll_votes: List[int] = Field(default_factory=list)
    status_message_id: Optional[int] = None
    last_status_update: float = 0.0
    now_playing: Optional[NowPlaying] = None
    last_error: Optional[str] = None
    votable_genres: List[str] = Field(
        default_factory=lambda: sorted(list(set([
            "pop", "pop 80s", "pop 90s", "pop	pm 2000s",
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
            "disco", "disco 70s", "disco 80s"
        ])))
    )
    retry_count: int = 0

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
            with CONFIG_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
                return State(**data)
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
                CONFIG_FILE.write_text(state.model_dump_json(indent=4), encoding='utf-8')
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
    return "█" * filled + "▁" * (width - filled)

def escape_markdown_v2(text: str) -> str:
    if not isinstance(text, str) or not text:
        logger.debug(f"Empty or invalid input for MarkdownV2 escaping: {repr(text)}")
        return ""
    special_chars = r'([_*[\]()~`>#+-=|{}\.!])'
    return re.sub(special_chars, r'\\\1', text)

def set_escaped_error(state: State, error: str):
    state.last_error = escape_markdown_v2(error) if error else None
    logger.error(f"Error set: {error}")

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
            await update.effective_message.reply_text("This command is for admins only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Music Sources ---
async def get_tracks_soundcloud(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"scsearch{Constants.SEARCH_LIMIT}:{genre}",
        'noplaylist': True,
        'quiet': False,
        'extract_flat': 'in_playlist'
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", [])
        ]
        logger.info(f"Found {len(tracks)} tracks for genre {genre} on SoundCloud")
        return tracks
    except yt_dlp.YoutubeDLError as e:
        logger.error(f"SoundCloud search failed for genre {genre}: {e}")
        return []

async def get_tracks_youtube(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"ytsearch{Constants.SEARCH_LIMIT}:{genre}",
        'noplaylist': True,
        'quiet': False,
        'extract_flat': 'in_playlist'
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", [])
        ]
        logger.info(f"Found {len(tracks)} tracks for genre {genre} on YouTube")
        return tracks
    except yt_dlp.YoutubeDLError as e:
        logger.error(f"YouTube search failed for genre {genre}: {e}")
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
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ No tracks found on {state.source} for genre {state.genre}. Retrying ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                if state.source == "soundcloud" and attempt == 0:
                    state.source = "youtube"
                elif attempt == Constants.MAX_RETRIES - 1:
                    state.genre = Constants.DEFAULT_GENRE
                    state.source = Constants.DEFAULT_SOURCE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            filtered_tracks = [
                t for t in tracks
                if Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION
                and t["url"] not in state.played_radio_urls
            ]
            urls = [t["url"] for t in filtered_tracks]
            if urls:
                random.shuffle(urls)
                state.radio_playlist.extend(urls)
                state.retry_count = 0
                state.genre = original_genre
                state.source = original_source
                logger.info(f"Playlist refilled with {len(urls)} tracks")
                await save_state_from_botdata(context.bot_data)
                return
            else:
                logger.warning(f"No valid tracks after filtering on {state.source}")
                set_escaped_error(state, f"No valid tracks after filtering on {state.source}")
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ No valid tracks after filtering on {state.source}. Retrying ({attempt + 1}/{Constants.MAX_RETRIES}).")
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
        except Exception as e:
            logger.error(f"Playlist refill failed, attempt {attempt + 1}: {e}")
            set_escaped_error(state, f"Playlist refill error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Playlist refill error: {e}")
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

    logger.error(f"Failed to refill playlist after {Constants.MAX_RETRIES} attempts")
    state.source = Constants.DEFAULT_SOURCE
    state.genre = Constants.DEFAULT_GENRE
    set_escaped_error(state, f"Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to {state.source}/{state.genre}.")
    await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to {state.source}/{state.genre}.")
    await save_state_from_botdata(context.bot_data)

# --- Download & send ---
async def check_track_validity(url: str) -> Optional[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': False,
        'simulate': True
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
        return {"url": url, "title": info.get("title", "Unknown"), "duration": info.get("duration", 0)}
    except Exception as e:
        logger.error(f"Track validity check failed for {url}: {e}")
        return None

async def download_and_send_to_chat(context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int):
    state: State = context.bot_data['state']
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        set_escaped_error(state, "FFmpeg or ffprobe not installed")
        await context.bot.send_message(chat_id, "⚠️ Error: FFmpeg or ffprobe not installed.")
        return

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        set_escaped_error(state, "Download directory not writable")
        await context.bot.send_message(chat_id, "⚠️ Error: Download directory not writable.")
        return

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': False,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'ffmpeg_location': shutil.which("ffmpeg"),
        'ffprobe_location': shutil.which("ffprobe")
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES

    filepath = None
    try:
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
        if not filepath.exists():
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '192',
            }]
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filepath = Path(ydl.prepare_filename(info)).with_suffix('.m4a')
            if not filepath.exists():
                set_escaped_error(state, "Failed to download track in mp3 or m4a format")
                await context.bot.send_message(chat_id, "⚠️ Failed to download track in mp3 or m4a format.")
                return

        if filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            set_escaped_error(state, "Track exceeds max file size")
            await context.bot.send_message(chat_id, "⚠️ Track too large to send.")
            filepath.unlink(missing_ok=True)
            return

        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                chat_id, f,
                title=info.get("title", "Unknown"),
                duration=int(info.get("duration", 0)),
                performer=info.get("uploader", "Unknown")
            )
        logger.info(f"Sent track to chat {chat_id}: {info.get('title', 'Unknown')}")
    except asyncio.TimeoutError:
        set_escaped_error(state, "Track download timeout")
        await context.bot.send_message(chat_id, "⚠️ Track download timed out.")
    except TelegramError as e:
        set_escaped_error(state, f"Telegram error sending track: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Telegram error sending track: {e}")
    except Exception as e:
        set_escaped_error(state, f"Track download error: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Failed to process track: {e}")
    finally:
        if filepath and filepath.exists():
            filepath.unlink(missing_ok=True)
            logger.debug(f"Cleaned up file: {filepath}")

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str):
    state: State = context.bot_data['state']
    track_info = await check_track_validity(url)
    if not track_info or not (Constants.MIN_DURATION <= track_info["duration"] <= Constants.MAX_DURATION):
        set_escaped_error(state, "Invalid track or duration out of range")
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Invalid track or duration out of range.")
        state.now_playing = None
        await update_status_panel(context, force=True)
        return

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        set_escaped_error(state, "FFmpeg or ffprobe not installed")
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Error: FFmpeg or ffprobe not installed.")
        state.now_playing = None
        await update_status_panel(context, force=True)
        return

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        set_escaped_error(state, "Download directory not writable")
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Error: Download directory not writable.")
        state.now_playing = None
        await update_status_panel(context, force=True)
        return

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': False,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'ffmpeg_location': shutil.which("ffmpeg"),
        'ffprobe_location': shutil.which("ffprobe")
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES

    filepath = None
    try:
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
        if not filepath.exists():
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '192',
            }]
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filepath = Path(ydl.prepare_filename(info)).with_suffix('.m4a')
            if not filepath.exists():
                set_escaped_error(state, "Failed to download track in mp3 or m4a format")
                await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Failed to download track in mp3 or m4a format.")
                state.now_playing = None
                await update_status_panel(context, force=True)
                return

        if filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            set_escaped_error(state, "Track exceeds max file size")
            await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Track too large to send.")
            state.now_playing = None
            await update_status_panel(context, force=True)
            filepath.unlink(missing_ok=True)
            return

        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown"),
            duration=int(info.get("duration", 0)),
            url=url
        )
        await update_status_panel(context, force=True)
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                RADIO_CHAT_ID, f,
                title=state.now_playing.title,
                duration=state.now_playing.duration,
                performer=info.get("uploader", "Unknown")
            )
        logger.info(f"Sent radio track: {state.now_playing.title}")
        await update_status_panel(context, force=True)
    except asyncio.TimeoutError:
        set_escaped_error(state, "Track download timeout")
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Track download timed out.")
        state.now_playing = None
        await update_status_panel(context, force=True)
    except TelegramError as e:
        set_escaped_error(state, f"Telegram error sending track: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Telegram error sending track: {e}")
        state.now_playing = None
        await update_status_panel(context, force=True)
    except Exception as e:
        set_escaped_error(state, f"Track download error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Failed to process track: {e}")
        state.now_playing = None
        await update_status_panel(context, force=True)
    finally:
        if filepath and filepath.exists():
            filepath.unlink(missing_ok=True)
            logger.debug(f"Cleaned up file: {filepath}")

# --- Radio loop ---
async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info("Starting radio loop")
    await update_status_panel(context, force=True)
    while True:
        try:
            if not state.is_on:
                logger.info("Radio is off, sleeping")
                await asyncio.sleep(10)
                continue
            if not state.radio_playlist:
                logger.info("Playlist empty, refilling")
                await refill_playlist(context)
                if not state.radio_playlist:
                    set_escaped_error(state, "Failed to find tracks")
                    await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Failed to find tracks. Retrying.")
                    await update_status_panel(context, force=True)
                    await asyncio.sleep(Constants.RETRY_INTERVAL)
                    continue
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            logger.info(f"Playing track: {url}")
            await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            await update_status_panel(context, force=True)
            context.bot_data['skip_event'].clear()
            sleep_duration = min(
                state.now_playing.duration if state.now_playing and state.now_playing.duration > 0 else Constants.TRACK_INTERVAL_SECONDS,
                Constants.TRACK_INTERVAL_SECONDS
            )
            logger.debug(f"Waiting for {sleep_duration} seconds or skip event")
            try:
                await asyncio.wait_for(context.bot_data['skip_event'].wait(), timeout=sleep_duration)
            except asyncio.TimeoutError:
                pass
            await update_status_panel(context, force=True)
            await asyncio.sleep(Constants.PAUSE_BETWEEN_TRACKS)
            await update_status_panel(context, force=True)
        except asyncio.CancelledError:
            logger.info("Radio loop cancelled")
            break
        except Exception as e:
            logger.error(f"Radio loop error: {e}")
            set_escaped_error(state, f"Radio loop error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Radio loop error: {e}")
            await update_status_panel(context, force=True)
            await asyncio.sleep(5)

# --- UI ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    async with status_lock:
        state: State = context.bot_data['state']
        current_time = asyncio.get_event_loop().time()
        if not force and current_time - state.last_status_update < Constants.STATUS_UPDATE_MIN_INTERVAL:
            return

        genre = state.genre.title() if state.genre else "Unknown"
        source = state.source.title() if state.source else "Unknown"
        now_playing_title = state.now_playing.title if state.now_playing else "Waiting for track..."
        last_error = state.last_error or "None"

        genre_escaped = escape_markdown_v2(genre)
        source_escaped = escape_markdown_v2(source)
        now_playing_title_escaped = escape_markdown_v2(now_playing_title)
        last_error_escaped = escape_markdown_v2(last_error)

        lines = [
            "🎵 *Radio Groove AI* 🎵",
            f"**Status**: {'🟢 On' if state.is_on else '🔴 Off'}",
            f"**Genre**: {genre_escaped}",
            f"**Source**: {source_escaped}"
        ]
        if state.now_playing and state.now_playing.duration > 0:
            elapsed = current_time - state.now_playing.start_time
            progress = min(elapsed / state.now_playing.duration, 1.0)
            progress_bar = get_progress_bar(progress)
            duration = format_duration(state.now_playing.duration)
            lines.append(f"**Now Playing**: {now_playing_title_escaped} \\({duration}\\)")
            lines.append(f"**Progress**: {progress_bar} {int(progress * 100)}\\%")
        else:
            lines.append(f"**Now Playing**: {now_playing_title_escaped}")
        if state.active_poll_id:
            lines.append(f"🗳 *Poll Active* \\(ends in ~{Constants.POLL_DURATION_SECONDS} sec\\)")
        if state.last_error:
            lines.append(f"⚠️ **Last Error**: {last_error_escaped}")
        lines.append("────────────────")
        text = "\n".join(lines)

        if not text.strip():
            set_escaped_error(state, "Attempted to send empty status message")
            return

        last_status_text = context.bot_data.get('last_status_text', '')
        current_no_progress = re.sub(r'█*▁*\s*\d+%', '', text)
        last_no_progress = re.sub(r'█*▁*\s*\d+%', '', last_status_text)
        if not force and current_no_progress == last_no_progress:
            return

        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="radio:refresh"),
                InlineKeyboardButton("⏭ Skip" if state.is_on else "▶️ Start", callback_data="radio:skip" if state.is_on else "radio:on")
            ],
            [InlineKeyboardButton("🗳 Vote", callback_data="vote:start")] if state.is_on and not state.active_poll_id else [],
            [InlineKeyboardButton("⏹ Stop", callback_data="radio:off")] if state.is_on else [],
            [InlineKeyboardButton("📋 Menu", callback_data="cmd:menu")]
        ]
        try:
            if state.status_message_id:
                await context.bot.edit_message_text(
                    chat_id=RADIO_CHAT_ID,
                    message_id=state.status_message_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
                    parse_mode="MarkdownV2"
                )
            else:
                msg = await context.bot.send_message(
                    RADIO_CHAT_ID,
                    text,
                    reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
                    parse_mode="MarkdownV2"
                )
                state.status_message_id = msg.message_id
            context.bot_data['last_status_text'] = text
            state.last_status_update = current_time
            state.last_error = None
            await save_state_from_botdata(context.bot_data)
            logger.debug("Status panel updated")
        except TelegramError as e:
            logger.error(f"Status panel update failed: {e}, text: {repr(text)}")
            set_escaped_error(state, f"Status update error: {e}")
            if "Message to edit not found" in str(e):
                state.status_message_id = None
                await update_status_panel(context, force=True)
            elif "Message is not modified" in str(e):
                logger.debug("Status message unchanged, ignoring")
            elif "can't parse entities" in str(e):
                plain_text = re.sub(r'\\([_*[\]()~`>#+-=|{}\.!])|[*~_]', r'\1', text)
                try:
                    if state.status_message_id:
                        await context.bot.edit_message_text(
                            chat_id=RADIO_CHAT_ID,
                            message_id=state.status_message_id,
                            text=plain_text,
                            reply_markup=InlineKeyboardMarkup([row for row in keyboard if row])
                        )
                    else:
                        msg = await context.bot.send_message(
                            RADIO_CHAT_ID,
                            plain_text,
                            reply_markup=InlineKeyboardMarkup([row for row in keyboard if row])
                        )
                        state.status_message_id = msg.message_id
                    context.bot_data['last_status_text'] = plain_text
                    state.last_status_update = current_time
                    state.last_error = None
                    await save_state_from_botdata(context.bot_data)
                    logger.debug("Status panel updated with fallback text")
                except TelegramError as e2:
                    set_escaped_error(state, f"Fallback status update error: {e2}")
                    await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Status update error: {e2}")
            elif isinstance(e, RetryAfter):
                await asyncio.sleep(e.retry_after)
                await update_status_panel(context, force=True)
            else:
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Status update error: {e}")

# --- Commands ---
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        state: State = context.bot_data['state']
        set_escaped_error(state, "Invalid command request")
        if update.message:
            await update.message.reply_text("⚠️ Error: Invalid command request.")
        return

    user_id = update.effective_user.id
    state: State = context.bot_data['state']
    is_admin_user = await is_admin(user_id)

    if update.effective_chat.id != RADIO_CHAT_ID:
        set_escaped_error(state, f"Command sent in wrong chat: {update.effective_chat.id}")
        await update.message.reply_text(f"⚠️ This command works only in chat ID {RADIO_CHAT_ID}.")
        return

    text = [
        "🎵 *Groove AI Bot - Menu* 🎵",
        f"**Radio Status**: {'🟢 On' if state.is_on else '🔴 Off'}",
        f"**Current Genre**: {escape_markdown_v2(state.genre.title())}",
        f"**Voting**: {'🗳 Active' if state.active_poll_id else '⏳ Inactive'}",
        f"**Now Playing**: {escape_markdown_v2(state.now_playing.title if state.now_playing else 'Nothing playing')}",
        f"**Last Error**: {escape_markdown_v2(state.last_error or 'None')}",
        "",
        "📜 *Commands for all:*",
        "🎧 /play (/p) <title> - Search and play a track",
        "",
        "📜 *Admin commands:*",
        "▶️ /ron (/r_on) - Start radio",
        "⏹ /rof (/r_off, /stop, /t) - Stop radio",
        "🛑 /stopbot - Fully stop the bot",
        "⏭ /skip (/s) - Skip track",
        "🗳 /vote (/v) - Start voting",
        "🔄 /refresh (/r) - Refresh status",
        "🔧 /source (/src) <soundcloud|youtube> - Change source",
        "📋 /menu (/m) - Show this menu"
    ]
    text = "\n".join(text)
    keyboard = [
        [InlineKeyboardButton("🎧 Find Track", callback_data="cmd:play")],
        [InlineKeyboardButton("▶️ Start Radio", callback_data="radio:on"), InlineKeyboardButton("⏹ Stop Radio", callback_data="radio:off")] if is_admin_user else [],
        [InlineKeyboardButton("🛑 Stop Bot", callback_data="cmd:stopbot")] if is_admin_user else [],
        [InlineKeyboardButton("⏭ Skip", callback_data="radio:skip"), InlineKeyboardButton("🗳 Vote", callback_data="vote:start")] if is_admin_user and state.is_on and not state.active_poll_id else [],
        [InlineKeyboardButton("🔄 Refresh", callback_data="radio:refresh"), InlineKeyboardButton("🔧 Source", callback_data="cmd:source")] if is_admin_user else [],
        [InlineKeyboardButton("📋 Menu", callback_data="cmd:menu")] if is_admin_user else []
    ]
    try:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
            parse_mode="MarkdownV2"
        )
        state.last_error = None
        await save_state_from_botdata(context.bot_data)
        logger.info("Menu displayed")
    except TelegramError as e:
        set_escaped_error(state, f"Menu send error: {e}")
        plain_text = re.sub(r'\\([_*[\]()~`>#+-=|{}\.!])|[*~_]', r'\1', text)
        try:
            await update.message.reply_text(
                plain_text,
                reply_markup=InlineKeyboardMarkup([row for row in keyboard if row])
            )
            state.last_error = None
            await save_state_from_botdata(context.bot_data)
            logger.info("Menu displayed with fallback text")
        except TelegramError as e2:
            set_escaped_error(state, f"Fallback menu send error: {e2}")
            await update.message.reply_text(f"⚠️ Menu display error: {e2}")

async def toggle_radio(context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    state.is_on = turn_on
    if turn_on:
        state.now_playing = None
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        await refill_playlist(context)
    else:
        task = context.bot_data.get('radio_loop_task')
        if task:
            task.cancel()
        state.now_playing = None
        state.radio_playlist.clear()
    await save_state_from_botdata(context.bot_data)
    logger.info(f"Radio {'started' if turn_on else 'stopped'}")

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    await toggle_radio(context, turn_on)
    await update_status_panel(context, force=True)
    await update.message.reply_text("Radio started. 🎵" if turn_on else "Radio stopped. 🔇", parse_mode="Markdown")
    logger.info(f"Radio {'on' if turn_on else 'off'} command executed")

@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    state.is_on = False
    state.now_playing = None
    state.radio_playlist.clear()
    state.played_radio_urls.clear()
    task = context.bot_data.get('radio_loop_task')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await save_state_from_botdata(context.bot_data)
    await update.message.reply_text("🛑 Bot stopping. Restart it on the server.")
    logger.info("Bot stop command executed")
    await context.application.stop_running()

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await skip_track(context)
    await update.message.reply_text("Skipping track... ⏭")
    logger.info("Skip command executed")

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_vote(context)
    await update.message.reply_text("Poll started! 🗳")
    logger.info("Vote command executed")

@admin_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context, force=True)
    await update.message.reply_text("Status refreshed. 🔄")
    logger.info("Refresh command executed")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not context.args or context.args[0] not in ["soundcloud", "youtube"]:
        await update.message.reply_text("Usage: /source (/src) soundcloud|youtube")
        return
    state.source = context.args[0]
    state.radio_playlist.clear()
    state.now_playing = None
    state.retry_count = 0
    await refill_playlist(context)
    message = f"Source switched to: {escape_markdown_v2(state.source.title())}"
    if state.source == "youtube" and not YOUTUBE_COOKIES:
        message += "\n⚠️ YouTube may require authentication. Set YOUTUBE_COOKIES or use /source soundcloud."
    await update.message.reply_text(message, parse_mode="MarkdownV2")
    await save_state_from_botdata(context.bot_data)
    logger.info(f"Source switched to {state.source}")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not context.args:
        await update.message.reply_text("Please specify a song title.")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'🔍 Searching for "{query}"...')

    search_prefix = f"scsearch{Constants.SEARCH_LIMIT}" if state.source == "soundcloud" else f"ytsearch{Constants.SEARCH_LIMIT}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': False,
        'default_search': search_prefix,
        'extract_flat': 'in_playlist'
    }
    if state.source == "youtube" and YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
        if not info.get('entries'):
            set_escaped_error(state, "No tracks found")
            await message.edit_text("No tracks found. 😔")
            return

        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info['entries']
        ]
        filtered_tracks = [
            t for t in tracks
            if Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION
        ]
        if not filtered_tracks:
            set_escaped_error(state, "No tracks match duration criteria")
            await message.edit_text("No tracks match duration criteria. 😔")
            return

        keyboard = [
            [InlineKeyboardButton(f"▶️ {escape_markdown_v2(t['title'])} \\({format_duration(t['duration'])}\\)", callback_data=f"play_track:{t['url']}")]
            for t in filtered_tracks[:Constants.SEARCH_LIMIT]
        ]
        await message.edit_text('Select a track:', reply_markup=InlineKeyboardMarkup(keyboard))
        logger.info(f"Search results displayed for query: {query}")
    except Exception as e:
        set_escaped_error(state, f"Search error: {e}")
        await message.edit_text(f"Search error: {e}")
        logger.error(f"Search error for query {query}: {e}")

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state: State = context.bot_data['state']
    try:
        await query.answer()
        command, url = query.data.split(":", 1)
        if command == "play_track":
            await query.edit_message_text("Processing track...")
            await download_and_send_to_chat(context, url, query.message.chat_id)
            await query.edit_message_text("Track sent! 🎵")
            logger.info(f"Played track from callback: {url}")
    except TelegramError as e:
        set_escaped_error(state, f"Callback error: {e}")
        await query.edit_message_text(f"Callback error: {e}")
        logger.error(f"Callback error for track {url}: {e}")

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state: State = context.bot_data['state']
    try:
        await query.answer()
        command, data = query.data.split(":", 1)
    except (ValueError, TelegramError) as e:
        set_escaped_error(state, f"Invalid callback: {e}")
        await query.answer("Invalid callback.", show_alert=True)
        logger.error(f"Invalid callback: {e}")
        return

    if command == "radio":
        if not await is_admin(query.from_user.id):
            set_escaped_error(state, "Unauthorized radio command")
            await query.answer("Admin only command.", show_alert=True)
            return
        if data == "refresh":
            await update_status_panel(context, force=True)
            await query.answer("Status refreshed. 🔄")
            logger.info("Refresh button clicked")
        elif data == "skip":
            await skip_track(context)
            await query.answer("Skipping track... ⏭")
            logger.info("Skip button clicked")
        elif data == "on":
            await toggle_radio(context, True)
            await update_status_panel(context, force=True)
            await query.answer("Radio started. 🎵")
            logger.info("Radio on button clicked")
        elif data == "off":
            await toggle_radio(context, False)
            await update_status_panel(context, force=True)
            await query.answer("Radio stopped. 🔇")
            logger.info("Radio off button clicked")
    elif command == "vote":
        if not await is_admin(query.from_user.id):
            set_escaped_error(state, "Unauthorized vote command")
            await query.answer("Admin only command.", show_alert=True)
            return
        if data == "start":
            await start_vote(context)
            await query.answer("Poll started! 🗳")
            logger.info("Vote button clicked")
    elif command == "cmd":
        if data == "play":
            await query.message.reply_text("Enter /play <song title> to search for a track.")
            logger.info("Play command button clicked")
        elif data == "source" and await is_admin(query.from_user.id):
            await query.message.reply_text("Enter /source soundcloud|youtube to change source.")
            logger.info("Source command button clicked")
        elif data == "stopbot" and await is_admin(query.from_user.id):
            state.is_on = False
            state.now_playing = None
            state.radio_playlist.clear()
            state.played_radio_urls.clear()
            task = context.bot_data.get('radio_loop_task')
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await save_state_from_botdata(context.bot_data)
            await query.message.reply_text("🛑 Bot stopping. Restart it on the server.")
            logger.info("Stop bot button clicked")
            await context.application.stop_running()
        elif data == "menu" and await is_admin(query.from_user.id):
            await show_menu(update, context)
            await query.answer("Menu opened. 📋")
            logger.info("Menu button clicked")
        else:
            set_escaped_error(state, "Command not available")
            await query.answer("Command not available.", show_alert=True)
            logger.error(f"Command not available: {data}")
    else:
        set_escaped_error(state, f"Unknown command: {command}")
        await query.answer("Unknown command.", show_alert=True)
        logger.error(f"Unknown command: {command}")

async def skip_track(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.is_on:
        context.bot_data['skip_event'].set()
        state.now_playing = None
        await update_status_panel(context, force=True)
        logger.info("Track skipped")

async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.active_poll_id:
        await context.bot.send_message(RADIO_CHAT_ID, "🗳 Poll already active!")
        logger.info("Attempted to start poll while one is active")
        return

    if len(state.votable_genres) < 2:
        set_escaped_error(state, "Not enough genres for voting")
        await context.bot.send_message(RADIO_CHAT_ID, "Not enough genres for voting. 😔")
        logger.error("Not enough genres for voting")
        return

    options = random.sample(state.votable_genres, min(len(state.votable_genres), 5))
    try:
        poll = await context.bot.send_poll(
            chat_id=RADIO_CHAT_ID,
            question="🎵 Choose the next genre (poll lasts 1 minute):",
            options=[opt.title() for opt in options],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=Constants.POLL_DURATION_SECONDS
        )
        state.active_poll_id = poll.poll.id
        state.poll_message_id = poll.message_id
        state.poll_options = options
        state.poll_votes = [0] * len(options)
        await context.bot.send_message(RADIO_CHAT_ID, "🗳 Poll started! Choose a genre above.")
        await save_state_from_botdata(context.bot_data)
        logger.info(f"Poll started with ID {poll.poll.id}")

        async def close_poll_after_timeout():
            try:
                await asyncio.sleep(Constants.POLL_DURATION_SECONDS + Constants.POLL_CHECK_TIMEOUT)
                if state.active_poll_id != poll.poll.id:
                    return
                try:
                    poll_update = await context.bot.stop_poll(RADIO_CHAT_ID, state.poll_message_id)
                    await handle_poll(Update(poll=poll_update), context)
                except TelegramError as e:
                    if "Poll has already been closed" in str(e):
                        updates = await context.bot.get_updates(allowed_updates=["poll"])
                        for update in updates:
                            if update.poll and update.poll.id == state.active_poll_id:
                                await handle_poll(Update(poll=update.poll), context)
                                break
                    else:
                        logger.error(f"Failed to close poll {poll.poll.id}: {e}")
                        set_escaped_error(state, f"Poll close error: {e}")
                        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Poll close error: {e}")
                finally:
                    state.active_poll_id = None
                    state.poll_message_id = None
                    state.poll_options = []
                    state.poll_votes = []
                    await save_state_from_botdata(context.bot_data)
                    logger.info(f"Poll {poll.poll.id} closed")
            except Exception as e:
                logger.error(f"Poll timeout error for {poll.poll.id}: {e}")
                set_escaped_error(state, f"Poll timeout error: {e}")
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Poll timeout error: {e}")
                state.active_poll_id = None
                state.poll_message_id = None
                state.poll_options = []
                state.poll_votes = []
                await save_state_from_botdata(context.bot_data)

        asyncio.create_task(close_poll_after_timeout())
    except TelegramError as e:
        set_escaped_error(state, f"Poll start error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Failed to start poll: {e}")
        logger.error(f"Poll start error: {e}")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    poll_answer: PollAnswer = update.poll_answer
    if poll_answer.poll_id == state.active_poll_id and poll_answer.option_ids:
        option_id = poll_answer.option_ids[0]
        if 0 <= option_id < len(state.poll_votes):
            state.poll_votes[option_id] += 1
            await save_state_from_botdata(context.bot_data)
            logger.info(f"Poll vote recorded for option {option_id}")

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if update.poll.id != state.active_poll_id or not update.poll.is_closed:
        return

    max_votes = max(o.voter_count for o in update.poll.options)
    winning_options = [o.text.lower() for o in update.poll.options if o.voter_count == max_votes]

    if max_votes == 0:
        await context.bot.send_message(RADIO_CHAT_ID, "No votes in poll. 😔")
        logger.info("Poll closed with no votes")
    else:
        selected_genre = random.choice(winning_options)
        state.genre = selected_genre
        state.radio_playlist.clear()
        state.now_playing = None
        await context.bot.send_message(RADIO_CHAT_ID, f"🎵 New genre: *{escape_markdown_v2(state.genre.title())}*")
        await refill_playlist(context)
        if not state.is_on:
            state.is_on = True
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        elif context.bot_data.get('radio_loop_task'):
            try:
                context.bot_data['radio_loop_task'].cancel()
                await context.bot_data['radio_loop_task']
            except asyncio.CancelledError:
                pass
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        logger.info(f"Poll closed, new genre: {selected_genre}")

    state.active_poll_id = None
    state.poll_message_id = None
    state.poll_options = []
    state.poll_votes = []
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    state.now_playing = None
    await save_state_from_botdata(context.bot_data)
    await show_menu(update, context)
    logger.info("Start command executed")

# --- Bot Lifecycle ---
async def check_bot_permissions(context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = await context.bot.get_chat(RADIO_CHAT_ID)
        bot_member = await context.bot.get_chat_member(RADIO_CHAT_ID, context.bot.id)
        if not bot_member.can_send_messages or not bot_member.can_send_audio:
            logger.error(f"Bot lacks permissions in chat {RADIO_CHAT_ID}: {bot_member.status}")
            return False
        logger.info(f"Bot permissions verified in chat {RADIO_CHAT_ID}")
        return True
    except TelegramError as e:
        logger.error(f"Failed to verify bot permissions in chat {RADIO_CHAT_ID}: {e}")
        return False

async def post_init(application: Application):
    logger.info("Starting post_init")
    try:
        application.bot_data['state'] = load_state()
        application.bot_data['skip_event'] = asyncio.Event()
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            logger.error("FFmpeg or ffprobe not installed")
            application.bot_data['state'].last_error = "FFmpeg or ffprobe not installed"
            await application.bot.send_message(RADIO_CHAT_ID, "⚠️ FFmpeg or ffprobe not installed.")
            return
        if not await check_bot_permissions(application):
            application.bot_data['state'].last_error = f"Bot lacks permissions in chat {RADIO_CHAT_ID}"
            await application.bot.send_message(RADIO_CHAT_ID, f"⚠️ Bot lacks permissions in chat {RADIO_CHAT_ID}.")
            return
        if application.bot_data['state'].is_on:
            logger.info("Radio is on, starting radio loop")
            application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
            await refill_playlist(application)
        logger.info("Post_init completed successfully")
    except Exception as e:
        logger.error(f"Post_init failed: {e}")
        application.bot_data['state'].last_error = f"Post_init error: {e}"
        await application.bot.send_message(RADIO_CHAT_ID, f"⚠️ Bot initialization error: {e}")

async def on_shutdown(application: Application):
    logger.info("Starting shutdown")
    task = application.bot_data.get('radio_loop_task')
    if task:
        task.cancel()
    await save_state_from_botdata(application.bot_data)
    logger.info("Shutdown completed")

def main():
    logger.info("Starting bot")
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not BOT_TOKEN or not RADIO_CHAT_ID or not ADMIN_IDS:
        logger.error("Missing required environment variables: BOT_TOKEN, RADIO_CHAT_ID, or ADMIN_IDS")
        raise ValueError("BOT_TOKEN, RADIO_CHAT_ID, or ADMIN_IDS not set!")
    try:
        import yt_dlp
    except ImportError:
        logger.error("yt-dlp not installed")
        raise ImportError("yt-dlp not installed. Install it with 'pip install yt-dlp'")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler(["start", "menu", "m"], start_command))
    app.add_handler(CommandHandler(["ron", "r_on"], lambda u, c: radio_on_off_command(u, c, True)))
    app.add_handler(CommandHandler(["rof", "r_off", "stop", "t"], lambda u, c: radio_on_off_command(u, c, False)))
    app.add_handler(CommandHandler(["stopbot"], stop_bot_command))
    app.add_handler(CommandHandler(["skip", "s"], skip_command))
    app.add_handler(CommandHandler(["vote", "v"], vote_command))
    app.add_handler(CommandHandler(["refresh", "r"], refresh_command))
    app.add_handler(CommandHandler(["source", "src"], set_source_command))
    app.add_handler(CommandHandler(["play", "p"], play_command))
    app.add_handler(CallbackQueryHandler(play_button_callback, pattern="^play_track:"))
    app.add_handler(CallbackQueryHandler(radio_buttons_callback, pattern="^(radio|vote|cmd):"))
    app.add_handler(PollHandler(handle_poll))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    logger.info("Handlers registered, starting polling")
    app.run_polling(timeout=3)

if __name__ == "__main__":
    main()
