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
from telegram.error import TelegramError
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_serializer, field_validator
from functools import wraps
from asyncio import Lock

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 10
    POLL_DURATION_SECONDS = 60
    POLL_CHECK_TIMEOUT = 10
    MAX_FILE_SIZE = 50_000_000
    MAX_DURATION = 1800  # 30 минут
    MIN_DURATION = 30  # 30 секунд
    PLAYED_URLS_MEMORY = 100
    DOWNLOAD_TIMEOUT = 30
    DEFAULT_SOURCE = "soundcloud"
    DEFAULT_GENRE = "pop"
    PAUSE_BETWEEN_TRACKS = 90
    STATUS_UPDATE_INTERVAL = 10
    STATUS_UPDATE_MIN_INTERVAL = 2
    RETRY_INTERVAL = 90
    SEARCH_LIMIT = 20
    MAX_RETRIES = 3

# --- Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG)
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
        default_factory=lambda: [
            "pop", "rock", "hip hop", "electronic", "classical", "jazz", "blues", "country",
            "metal", "reggae", "folk", "indie", "rap", "r&b", "soul", "funk", "disco"
        ]
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

def escape_markdown_v2(text: str) -> str:
    """Escape all MarkdownV2 reserved characters, including periods and ampersands."""
    if not isinstance(text, str) or not text:
        logger.debug(f"Invalid or empty input for MarkdownV2 escaping: {repr(text)}")
        return ""
    # All MarkdownV2 reserved characters
    special_chars = r'([_*[\]()~`>#+-=|{}\.!&])'
    escaped = re.sub(special_chars, r'\\\1', str(text))
    logger.debug(f"Escaped MarkdownV2 text: {repr(text)} -> {repr(escaped)}")
    return escaped

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
        logger.debug(f"SoundCloud returned {len(tracks)} tracks for genre {genre}: {[t['title'] for t in tracks]}")
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
        logger.debug(f"Using YouTube cookies from {YOUTUBE_COOKIES}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", [])
        ]
        logger.debug(f"YouTube returned {len(tracks)} tracks for genre {genre}: {[t['title'] for t in tracks]}")
        return tracks
    except yt_dlp.YoutubeDLError as e:
        logger.error(f"YouTube search failed for genre {genre}: {e}")
        if "Sign in to confirm you’re not a bot" in str(e):
            logger.warning("YouTube requires authentication. Consider setting YOUTUBE_COOKIES environment variable.")
        return []

# --- Playlist refill ---
async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    logger.debug(f"Played URLs size: {len(state.played_radio_urls)}")
    if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY * 0.5:
        state.played_radio_urls.clear()
        logger.info("Cleared played_radio_urls to prevent over-filtering")

    async def attempt_refill(source: str, genre: str) -> List[dict]:
        tracks = []
        if source == "soundcloud":
            tracks = await get_tracks_soundcloud(genre)
        elif source == "youtube":
            tracks = await get_tracks_youtube(genre)
        logger.debug(f"Attempted refill from {source}, found {len(tracks)} tracks")
        return tracks

    original_genre = state.genre
    for attempt in range(Constants.MAX_RETRIES):
        try:
            tracks = await attempt_refill(state.source, state.genre)
            if not tracks and state.source == "youtube":
                logger.warning(f"No tracks found on YouTube, switching to SoundCloud")
                state.source = "soundcloud"
                tracks = await attempt_refill(state.source, state.genre)
            
            if not tracks:
                logger.warning(f"No tracks found on {state.source} for genre {state.genre} after attempt {attempt + 1}")
                state.last_error = f"Не удалось найти треки на {state.source} для жанра {state.genre}"
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Нет подходящих треков на {state.source} после фильтрации. Попробую снова ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                if attempt == Constants.MAX_RETRIES - 1:
                    logger.info(f"Switching to default genre: {Constants.DEFAULT_GENRE}")
                    state.genre = Constants.DEFAULT_GENRE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            logger.debug(f"Tracks before filtering: {[{'title': t['title'], 'duration': t['duration'], 'url': t['url']} for t in tracks]}")
            filtered_tracks = [
                t for t in tracks
                if Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION
                and t["url"] not in state.played_radio_urls
            ]
            logger.debug(f"Filtered tracks: {[{'title': t['title'], 'duration': t['duration'], 'url': t['url']} for t in filtered_tracks]}")
            urls = [t["url"] for t in filtered_tracks]
            if urls:
                random.shuffle(urls)
                state.radio_playlist.extend(urls)
                state.retry_count = 0
                state.genre = original_genre
                await save_state_from_botdata(context.bot_data)
                logger.info(f"Added {len(urls)} new tracks (filtered from {len(tracks)}). URLs: {urls}")
                return
            else:
                logger.warning(f"No valid tracks after filtering on {state.source}. Reasons: {['Duration out of range' if not (Constants.MIN_DURATION <= t['duration'] <= Constants.MAX_DURATION) else 'Already played' for t in tracks]}")
                state.last_error = f"Нет подходящих треков на {state.source} после фильтрации"
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Нет подходящих треков на {state.source} после фильтрации. Попробую снова ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                if attempt == Constants.MAX_RETRIES - 1:
                    logger.info(f"Switching to default genre: {Constants.DEFAULT_GENRE}")
                    state.genre = Constants.DEFAULT_GENRE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                await asyncio.sleep(Constants.RETRY_INTERVAL)
        except Exception as e:
            logger.error(f"Playlist refill failed on attempt {attempt + 1}: {e}", exc_info=True)
            state.last_error = f"Ошибка при заполнении плейлиста: {e}"
            await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Ошибка при заполнении плейлиста: {e}")
            state.retry_count += 1
            if attempt == Constants.MAX_RETRIES - 1:
                logger.info(f"Switching to default genre: {Constants.DEFAULT_GENRE}")
                state.genre = Constants.DEFAULT_GENRE
                state.radio_playlist.clear()
                state.played_radio_urls.clear()
            await asyncio.sleep(Constants.RETRY_INTERVAL)

    logger.error(f"Failed to refill playlist after {Constants.MAX_RETRIES} attempts. Switching to SoundCloud and default genre.")
    state.source = "soundcloud"
    state.genre = Constants.DEFAULT_GENRE
    state.last_error = f"Не удалось найти треки после нескольких попыток. Переключено на {state.source} с жанром {state.genre}."
    await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Не удалось найти треки после нескольких попыток. Переключено на {state.source} с жанром {state.genre}.")
    await save_state_from_botdata(context.bot_data)
    await refill_playlist(context)

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
        logger.debug(f"Track valid: {url}, title: {info.get('title', 'Unknown')}")
        return {"url": url, "title": info.get("title", "Unknown"), "duration": info.get("duration", 0)}
    except Exception as e:
        logger.error(f"Failed to check track validity {url}: {e}")
        return None

async def download_and_send_to_chat(context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int):
    state: State = context.bot_data['state']
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.error("FFmpeg or ffprobe not found in system")
        state.last_error = "FFmpeg или ffprobe не установлен"
        await context.bot.send_message(chat_id, "⚠️ Ошибка: FFmpeg или ffprobe не установлен на сервере.")
        return

    if not DOWNLOAD_DIR.exists():
        logger.error(f"Download directory {DOWNLOAD_DIR} does not exist")
        DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        logger.error(f"Download directory {DOWNLOAD_DIR} is not writable")
        state.last_error = "Нет доступа к директории downloads"
        await context.bot.send_message(chat_id, "⚠️ Нет доступа к директории для загрузки.")
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
    try:
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
        logger.debug(f"Downloaded file: {filepath}, format: {info.get('ext', 'unknown')}, audio codec: {info.get('acodec', 'unknown')}")
        if not filepath.exists():
            logger.error(f"MP3 file not found after conversion: {filepath}")
            state.last_error = "Ошибка конвертации трека в MP3"
            await context.bot.send_message(chat_id, "⚠️ Ошибка конвертации трека в MP3.")
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '192',
            }]
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                filepath = Path(ydl.prepare_filename(info)).with_suffix('.m4a')
                logger.debug(f"Fallback to m4a: {filepath}")
                if not filepath.exists():
                    logger.error(f"Fallback m4a file not found: {filepath}")
                    state.last_error = "Ошибка загрузки трека даже в формате m4a"
                    await context.bot.send_message(chat_id, "⚠️ Не удалось загрузить трек в формате m4a.")
                    return
            except Exception as e:
                logger.error(f"Fallback m4a download failed: {e}")
                state.last_error = f"Ошибка загрузки трека в формате m4a: {e}"
                await context.bot.send_message(chat_id, f"⚠️ Не удалось загрузить трек в формате m4a: {e}")
                return
        file_size = filepath.stat().st_size
        if file_size > Constants.MAX_FILE_SIZE:
            logger.warning(f"Track {url} exceeds max file size: {file_size} bytes")
            state.last_error = "Трек слишком большой"
            await context.bot.send_message(chat_id, "⚠️ Трек слишком большой для отправки.")
            filepath.unlink(missing_ok=True)
            return
        with open(filepath, 'rb') as f:
            logger.debug(f"Sending audio to chat {chat_id}: {info.get('title', 'Unknown')}")
            await context.bot.send_audio(
                chat_id, f,
                title=info.get("title", "Unknown"),
                duration=int(info.get("duration", 0)),
                performer=info.get("uploader", "Unknown")
            )
        filepath.unlink(missing_ok=True)
    except asyncio.TimeoutError:
        logger.error(f"Download timeout for track {url}")
        state.last_error = "Таймаут загрузки трека"
        await context.bot.send_message(chat_id, "⚠️ Время ожидания загрузки трека истекло.")
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}", exc_info=True)
        state.last_error = f"Ошибка загрузки трека: {e}"
        error_msg = f"⚠️ Не удалось обработать трек: {e}"
        if "Sign in to confirm you’re not a bot" in str(e):
            error_msg += "\nYouTube требует авторизации. Используйте /source soundcloud или настройте YOUTUBE_COOKIES."
        await context.bot.send_message(chat_id, error_msg)

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str):
    state: State = context.bot_data['state']
    track_info = await check_track_validity(url)
    if not track_info or not (Constants.MIN_DURATION <= track_info["duration"] <= Constants.MAX_DURATION):
        logger.warning(f"Track {url} is invalid or out of duration range")
        state.last_error = "Недопустимый трек или неверная длительность"
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Недопустимый трек или неверная длительность.")
        return

    if not shutil.which("ffmpeg"):
        logger.error("FFmpeg not found in system")
        state.last_error = "FFmpeg не установлен"
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Ошибка: FFmpeg не установлен на сервере.")
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
        'ffmpeg_location': shutil.which("ffmpeg")
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
        if not filepath.exists():
            logger.error(f"MP3 file not found after conversion: {filepath}")
            state.last_error = "Ошибка конвертации трека в MP3"
            await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Ошибка конвертации трека в MP3.")
            ydl_opts['postprocessors'] = []
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                filepath = Path(ydl.prepare_filename(info))
                if not filepath.exists():
                    logger.error(f"Fallback download failed: {filepath}")
                    state.last_error = "Ошибка загрузки трека даже без конвертации"
                    await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось загрузить трек.")
                    return
            except Exception as e:
                logger.error(f"Fallback download failed: {e}")
                state.last_error = f"Ошибка загрузки трека: {e}"
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Не удалось загрузить трек: {e}")
                return
        file_size = filepath.stat().st_size
        if file_size > Constants.MAX_FILE_SIZE:
            logger.warning(f"Track {url} exceeds max file size: {file_size} bytes")
            state.last_error = "Трек слишком большой"
            await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Трек слишком большой для отправки.")
            filepath.unlink(missing_ok=True)
            return
        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown"),
            duration=int(info.get("duration", 0)),
            url=url
        )
        await update_status_panel(context, force=True)
        with open(filepath, 'rb') as f:
            logger.debug(f"Sending MP3 audio to chat {RADIO_CHAT_ID}: {state.now_playing.title}")
            await context.bot.send_audio(
                RADIO_CHAT_ID, f,
                title=state.now_playing.title,
                duration=state.now_playing.duration,
                performer=info.get("uploader", "Unknown")
            )
        filepath.unlink(missing_ok=True)
        await update_status_panel(context, force=True)
    except asyncio.TimeoutError:
        logger.error(f"Download timeout for track {url}")
        state.last_error = "Таймаут загрузки трека"
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Время ожидания загрузки трека истекло.")
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}", exc_info=True)
        state.last_error = f"Ошибка загрузки трека: {e}"
        error_msg = f"⚠️ Не удалось обработать трек: {e}"
        if "Sign in to confirm you’re not a bot" in str(e):
            error_msg += "\nYouTube требует авторизации. Используйте /source soundcloud или настройте YOUTUBE_COOKIES."
        await context.bot.send_message(RADIO_CHAT_ID, error_msg)

# --- Radio loop ---
async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context, force=True)
    while True:
        try:
            state: State = context.bot_data['state']
            if not state.is_on:
                logger.debug("Radio is off, sleeping for 10 seconds")
                await asyncio.sleep(10)
                continue
            if not state.radio_playlist:
                logger.debug("Playlist is empty, refilling")
                await refill_playlist(context)
                if not state.radio_playlist:
                    logger.warning("Playlist still empty after refill")
                    state.last_error = "Не удалось найти треки"
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
            await update_status_panel(context, force=True)
        except asyncio.CancelledError:
            logger.debug("Radio loop cancelled")
            break
        except Exception as e:
            logger.error(f"radio_loop error: {e}", exc_info=True)
            state.last_error = f"Ошибка radio_loop: {e}"
            await asyncio.sleep(5)

# --- UI ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    async with status_lock:
        state: State = context.bot_data['state']
        current_time = asyncio.get_event_loop().time()
        if not force and current_time - state.last_status_update < Constants.STATUS_UPDATE_MIN_INTERVAL:
            logger.debug("Skipping status update due to rate limit")
            return

        lines = [
            "🎵 *Радио Groove AI* 🎵",
            f"**Статус**: {'🟢 Включено' if state.is_on else '🔴 Выключено'}",
            f"**Жанр**: {escape_markdown_v2(state.genre.title())}",
            f"**Источник**: {escape_markdown_v2(state.source.title())}"
        ]
        if state.now_playing:
            elapsed = current_time - state.now_playing.start_time
            progress = min(elapsed / state.now_playing.duration, 1.0) if state.now_playing.duration > 0 else 0
            progress_bar = get_progress_bar(progress)
            title = escape_markdown_v2(state.now_playing.title)
            duration = format_duration(state.now_playing.duration)
            lines.append(f"**Сейчас играет**: {title} \\({duration}\\)")
            lines.append(f"**Прогресс**: {progress_bar} {int(progress * 100)}\\%")
        else:
            lines.append("**Сейчас играет**: Ожидание трека...")
        if state.active_poll_id:
            lines.append(f"🗳 *Голосование активно* \\(осталось ~{Constants.POLL_DURATION_SECONDS} сек\\)")
        if state.last_error:
            lines.append(f"⚠️ **Последняя ошибка**: {escape_markdown_v2(state.last_error)}")
        lines.append("────────────────")
        text = "\n".join(lines)

        if not text.strip():
            logger.error(f"Generated empty status message: {lines}")
            state.last_error = "Попытка отправки пустого сообщения статуса"
            return

        logger.debug(f"Preparing to update status panel with text: {repr(text)}")

        last_status_text = context.bot_data.get('last_status_text', '')
        current_no_progress = re.sub(r'█*▁*\s*\d+%', '', text)
        last_no_progress = re.sub(r'█*▁*\s*\d+%', '', last_status_text)
        if not force and current_no_progress == last_no_progress:
            logger.debug("Status text unchanged (ignoring progress bar and percentage), skipping update")
            return

        keyboard = [
            [
                InlineKeyboardButton("🔄 Обновить", callback_data="radio:refresh"),
                InlineKeyboardButton("⏭ Пропустить" if state.is_on else "▶️ Включить", callback_data="radio:skip" if state.is_on else "radio:on")
            ],
            [InlineKeyboardButton("🗳 Голосовать", callback_data="vote:start")] if state.is_on and not state.active_poll_id else [],
            [InlineKeyboardButton("⏹ Стоп", callback_data="radio:off")] if state.is_on else [],
            [InlineKeyboardButton("📋 Меню", callback_data="cmd:menu")]
        ]
        try:
            if state.status_message_id:
                logger.debug(f"Editing message {state.status_message_id} with text: {repr(text)}")
                await context.bot.edit_message_text(
                    chat_id=RADIO_CHAT_ID,
                    message_id=state.status_message_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
                    parse_mode="MarkdownV2"
                )
            else:
                logger.debug(f"Sending new status message with text: {repr(text)}")
                msg = await context.bot.send_message(
                    RADIO_CHAT_ID,
                    text,
                    reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
                    parse_mode="MarkdownV2"
                )
                state.status_message_id = msg.message_id
            context.bot_data['last_status_text'] = text
            state.last_status_update = current_time
            await save_state_from_botdata(context.bot_data)
        except TelegramError as e:
            logger.error(f"Failed to update status panel: {e}, problematic text: {repr(text)}")
            state.last_error = f"Ошибка обновления статуса: {e}"
            if "Message to edit not found" in str(e):
                state.status_message_id = None
                await update_status_panel(context, force=True)
            elif "Message is not modified" in str(e):
                logger.debug("Message not modified, ignoring")
            elif "can't parse entities" in str(e):
                logger.error(f"Markdown parsing error: {e}, text: {repr(text)}")
                # Fallback to plain text
                plain_text = re.sub(r'\\([_*[\]()~`>#+-=|{}\.!&])', r'\1', text)
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
                    logger.debug("Fallback to plain text succeeded")
                    context.bot_data['last_status_text'] = plain_text
                    state.last_status_update = current_time
                    await save_state_from_botdata(context.bot_data)
                except TelegramError as e2:
                    logger.error(f"Fallback to plain text failed: {e2}, plain text: {repr(plain_text)}")
                    state.last_error = f"Ошибка обновления без Markdown: {e2}"
                    await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Ошибка при обновлении статуса: {e2}")
            else:
                logger.error(f"Unexpected Telegram error: {e}")
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Ошибка при обновлении статуса: {e}")

# --- Commands ---
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        logger.error("Invalid update: missing effective_user or message")
        state: State = context.bot_data['state']
        state.last_error = "Ошибка: недопустимый запрос команды"
        if update.message:
            await update.message.reply_text("⚠️ Ошибка: недопустимый запрос команды.")
        return

    user_id = update.effective_user.id
    logger.debug(f"Received /start or /menu command from user {user_id} in chat {update.effective_chat.id}")
    state: State = context.bot_data['state']
    is_admin_user = await is_admin(user_id)

    if update.effective_chat.id != RADIO_CHAT_ID:
        logger.warning(f"Command received in unauthorized chat {update.effective_chat.id}, expected {RADIO_CHAT_ID}")
        state.last_error = f"Команда отправлена в неверный чат: {update.effective_chat.id}"
        await update.message.reply_text(f"⚠️ Эта команда работает только в чате с ID {RADIO_CHAT_ID}.")
        return

    text = [
        "🎵 *Groove AI Bot - Меню* 🎵",
        f"**Статус радио**: {'🟢 Включено' if state.is_on else '🔴 Выключено'}",
        f"**Текущий жанр**: {escape_markdown_v2(state.genre.title())}",
        f"**Голосование**: {'🗳 Активно' if state.active_poll_id else '⏳ Не активно'}",
        f"**Сейчас играет**: {escape_markdown_v2(state.now_playing.title if state.now_playing else 'Ничего не играет')}",
        f"**Последняя ошибка**: {escape_markdown_v2(state.last_error or 'Отсутствует')}",
        "",
        "📜 *Команды для всех:*",
        "🎧 /play (/p) <название> - Поиск и воспроизведение трека",
        "",
        "📜 *Команды для админов:*",
        "▶️ /ron (/r_on) - Включить радио",
        "⏹ /rof (/r_off, /stop, /t) - Выключить радио",
        "🛑 /stopbot - Полностью остановить бота",
        "⏭ /skip (/s) - Пропустить трек",
        "🗳 /vote (/v) - Запустить голосование",
        "🔄 /refresh (/r) - Обновить статус",
        "🔧 /source (/src) <soundcloud|youtube> - Сменить источник",
        "📋 /menu (/m) - Показать это меню"
    ]
    text = "\n".join(text)
    keyboard = [
        [InlineKeyboardButton("🎧 Найти трек", callback_data="cmd:play")],
        [InlineKeyboardButton("▶️ Вкл радио", callback_data="radio:on"), InlineKeyboardButton("⏹ Выкл радио", callback_data="radio:off")] if is_admin_user else [],
        [InlineKeyboardButton("🛑 Стоп бот", callback_data="cmd:stopbot")] if is_admin_user else [],
        [InlineKeyboardButton("⏭ Пропустить", callback_data="radio:skip"), InlineKeyboardButton("🗳 Голосовать", callback_data="vote:start")] if is_admin_user and state.is_on and not state.active_poll_id else [],
        [InlineKeyboardButton("🔄 Обновить", callback_data="radio:refresh"), InlineKeyboardButton("🔧 Источник", callback_data="cmd:source")] if is_admin_user else [],
        [InlineKeyboardButton("📋 Меню", callback_data="cmd:menu")] if is_admin_user else []
    ]
    logger.debug(f"Sending menu to user {user_id} in chat {RADIO_CHAT_ID} with text: {repr(text)}")
    try:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]),
            parse_mode="MarkdownV2"
        )
    except TelegramError as e:
        logger.error(f"Failed to send menu: {e}, text: {repr(text)}")
        state.last_error = f"Ошибка отправки меню: {e}"
        plain_text = re.sub(r'\\([_*[\]()~`>#+-=|{}\.!&])', r'\1', text)
        try:
            await update.message.reply_text(
                plain_text,
                reply_markup=InlineKeyboardMarkup([row for row in keyboard if row])
            )
            logger.debug("Fallback to plain text menu succeeded")
        except TelegramError as e2:
            logger.error(f"Fallback to plain text menu failed: {e2}")
            state.last_error = f"Ошибка отправки меню без Markdown: {e2}"
            await update.message.reply_text(f"⚠️ Ошибка при открытии меню: {e2}")

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

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    user_id = update.effective_user.id
    logger.debug(f"Received /{'ron' if turn_on else 'rof'} command from user {user_id}")
    await toggle_radio(context, turn_on)
    await update_status_panel(context, force=True)
    message = "Радио включено. 🎵" if turn_on else "Радио выключено. 🔇"
    logger.debug(f"Sending message to {RADIO_CHAT_ID}: {message}")
    await update.message.reply_text(message, parse_mode="Markdown")

@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Received /stopbot command from user {user_id}")
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
            logger.debug("Radio loop task cancelled via /stopbot")
    await save_state_from_botdata(context.bot_data)
    await update.message.reply_text("🛑 Бот останавливается. Вы можете перезапустить его на сервере.")
    logger.info("Stopping bot via /stopbot command")
    await context.application.stop_running()

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Received /skip command from user {user_id}")
    await skip_track(context)
    logger.debug(f"Sending skip message to {RADIO_CHAT_ID}")
    await update.message.reply_text("Пропускаю трек... ⏭")

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Received /vote command from user {user_id}")
    await start_vote(context)
    logger.debug(f"Sending vote message to {RADIO_CHAT_ID}")
    await update.message.reply_text("Голосование запущено! 🗳")

@admin_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Received /refresh command from user {user_id}")
    await update_status_panel(context, force=True)
    logger.debug(f"Sending refresh message to {RADIO_CHAT_ID}")
    await update.message.reply_text("Статус обновлен. 🔄")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Received /source command from user {user_id}")
    if not context.args or context.args[0] not in ["soundcloud", "youtube"]:
        logger.debug(f"Sending source usage message to {RADIO_CHAT_ID}")
        await update.message.reply_text("Использование: /source (/src) soundcloud|youtube")
        return
    state: State = context.bot_data['state']
    state.source = context.args[0]
    state.radio_playlist.clear()
    state.now_playing = None
    state.retry_count = 0
    logger.info(f"Source switched to {state.source}, playlist cleared")
    await refill_playlist(context)
    message = f"Источник переключен на: {escape_markdown_v2(state.source.title())}"
    if state.source == "youtube" and not YOUTUBE_COOKIES:
        message += "\n⚠️ Для YouTube может потребоваться авторизация. Настройте YOUTUBE_COOKIES или используйте /source soundcloud."
    logger.debug(f"Sending source message to {RADIO_CHAT_ID}: {message}")
    await update.message.reply_text(message, parse_mode="MarkdownV2")
    await save_state_from_botdata(context.bot_data)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /play command from user {user_id}")
    if not context.args:
        logger.debug(f"Sending play usage message to {RADIO_CHAT_ID}")
        await update.message.reply_text("Пожалуйста, укажите название песни.")
        return

    query = " ".join(context.args)
    logger.info(f"Searching for '{query}' for user {user_id}")
    message = await update.message.reply_text(f'🔍 Поиск "{query}"...')

    state: State = context.bot_data['state']
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
            logger.debug(f"No tracks found for query '{query}'")
            state.last_error = "Треки не найдены"
            await message.edit_text("Треки не найдены. 😔")
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
            logger.debug(f"No valid tracks found after filtering for query '{query}'")
            state.last_error = "Нет подходящих треков по длительности"
            await message.edit_text("Нет подходящих треков по длительности. 😔")
            return

        keyboard = [
            [InlineKeyboardButton(f"▶️ {escape_markdown_v2(t['title'])} \\({format_duration(t['duration'])}\\)", callback_data=f"play_track:{t['url']}")]
            for t in filtered_tracks[:Constants.SEARCH_LIMIT]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        logger.debug(f"Sending track selection message with {len(filtered_tracks)} tracks to {RADIO_CHAT_ID}")
        await message.edit_text('Выберите трек:', reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in /play search: {e}", exc_info=True)
        state.last_error = f"Ошибка поиска трека: {e}"
        error_msg = f"Произошла ошибка при поиске: {e}"
        if "Sign in to confirm you’re not a bot" in str(e):
            error_msg += "\nYouTube требует авторизации. Используйте /source soundcloud или настройте YOUTUBE_COOKIES."
        await message.edit_text(error_msg)

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    query = update.callback_query
    logger.debug(f"Received play button callback from user {user_id}: {query.data}")
    try:
        await query.answer()
    except TelegramError as e:
        logger.error(f"Failed to answer play button callback: {e}")
        state: State = context.bot_data['state']
        state.last_error = f"Ошибка ответа на callback: {e}"
        return

    command, data = query.data.split(":", 1)

    if command == "play_track":
        url = data
        await query.edit_message_text(text="Обработка трека...")
        try:
            await download_and_send_to_chat(context, url, query.message.chat_id)
            logger.debug(f"Sending track sent message to {query.message.chat_id}")
            await query.edit_message_text(text="Трек отправлен! 🎵")
        except Exception as e:
            logger.error(f"Failed to process play button callback: {e}", exc_info=True)
            state: State = context.bot_data['state']
            state.last_error = f"Ошибка обработки трека: {e}"
            error_msg = f"Не удалось обработать трек: {e}"
            if "Sign in to confirm you’re not a bot" in str(e):
                error_msg += "\nYouTube требует авторизации. Используйте /source soundcloud или настройте YOUTUBE_COOKIES."
            await query.edit_message_text(error_msg)

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    state: State = context.bot_data['state']
    logger.debug(f"Received callback query from user {user_id}: {query.data}")

    try:
        await query.answer()
    except TelegramError as e:
        logger.error(f"Failed to answer callback query: {e}")
        state.last_error = f"Ошибка ответа на callback: {e}"
        return

    try:
        command, data = query.data.split(":", 1)
    except ValueError:
        logger.error(f"Invalid callback data format: {query.data}")
        state.last_error = "Недопустимый формат callback"
        await query.answer("Ошибка обработки команды.", show_alert=True)
        return

    if command == "radio":
        if not await is_admin(user_id):
            logger.warning(f"User {user_id} attempted radio command but is not admin")
            state.last_error = "Попытка неавторизованного доступа"
            await query.answer("Эта команда только для администраторов.", show_alert=True)
            return
        if data == "refresh":
            logger.debug("Processing radio:refresh callback")
            await update_status_panel(context, force=True)
            await query.answer("Статус обновлен. 🔄")
        elif data == "skip":
            logger.debug("Processing radio:skip callback")
            await skip_track(context)
            await query.answer("Пропускаю трек... ⏭")
        elif data == "on":
            logger.debug("Processing radio:on callback")
            await toggle_radio(context, True)
            await update_status_panel(context, force=True)
            await query.answer("Радио включено. 🎵")
        elif data == "off":
            logger.debug("Processing radio:off callback")
            await toggle_radio(context, False)
            await update_status_panel(context, force=True)
            await query.answer("Радио выключено. 🔇")
    elif command == "vote":
        if not await is_admin(user_id):
            logger.warning(f"User {user_id} attempted vote command but is not admin")
            state.last_error = "Попытка неавторизованного доступа"
            await query.answer("Эта команда только для администраторов.", show_alert=True)
            return
        if data == "start":
            logger.debug("Processing vote:start callback")
            await start_vote(context)
            await query.answer("Голосование запущено! 🗳")
    elif command == "cmd":
        if data == "play":
            logger.debug(f"Sending play command prompt to {query.message.chat_id}")
            await query.message.reply_text("Введите /play <название песни> для поиска трека.")
        elif data == "source" and await is_admin(user_id):
            logger.debug(f"Sending source command prompt to {query.message.chat_id}")
            await query.message.reply_text("Введите /source soundcloud|youtube для смены источника.")
        elif data == "stopbot" and await is_admin(user_id):
            logger.debug(f"Processing stopbot callback from user {user_id}")
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
                    logger.debug("Radio loop task cancelled via stopbot callback")
            await save_state_from_botdata(context.bot_data)
            await query.message.reply_text("🛑 Бот останавливается. Вы можете перезапустить его на сервере.")
            logger.info("Stopping bot via stopbot callback")
            await context.application.stop_running()
        elif data == "menu" and await is_admin(user_id):
            logger.debug(f"Showing menu for user {user_id}")
            await show_menu(update, context)
            await query.answer("Меню открыто. 📋")
        else:
            state.last_error = "Команда недоступна"
            await query.answer("Команда недоступна.", show_alert=True)
    else:
        logger.warning(f"Unknown callback command: {command}")
        state.last_error = f"Неизвестная команда: {command}"
        await query.answer("Неизвестная команда.")

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
        state.last_error = "Недостаточно жанров для голосования"
        await context.bot.send_message(RADIO_CHAT_ID, "Недостаточно жанров для голосования. 😔")
        return

    options = random.sample(state.votable_genres, min(len(state.votable_genres), 5))
    logger.debug(f"Starting poll with options: {options}")
    try:
        poll = await context.bot.send_poll(
            chat_id=RADIO_CHAT_ID,
            question="🎵 Выберите следующий жанр (голосование длится 1 минуту):",
            options=[escape_markdown_v2(opt.title()) for opt in options],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=Constants.POLL_DURATION_SECONDS
        )
        state.active_poll_id = poll.poll.id
        state.poll_message_id = poll.message_id
        state.poll_options = options
        state.poll_votes = [0] * len(options)
        logger.debug(f"Poll started with ID: {poll.poll.id}, message_id: {poll.message_id}")
        await context.bot.send_message(RADIO_CHAT_ID, "🗳 Голосование началось! Выберите жанр выше.")
        await save_state_from_botdata(context.bot_data)

        async def close_poll_after_timeout():
            try:
                await asyncio.sleep(Constants.POLL_DURATION_SECONDS + Constants.POLL_CHECK_TIMEOUT)
                if state.active_poll_id == poll.poll.id and state.poll_message_id:
                    logger.debug(f"Checking poll {poll.poll.id} status after timeout")
                    for attempt in range(3):
                        try:
                            updates = await context.bot.get_updates(allowed_updates=["poll"])
                            for update in updates:
                                if update.poll and update.poll.id == state.active_poll_id:
                                    logger.debug(f"Poll update received: {update.poll}")
                                    if update.poll.is_closed:
                                        await handle_poll(Update(poll=update.poll), context)
                                        return
                            logger.debug(f"Attempt {attempt + 1}: Forcing poll {poll.poll.id} to close")
                            poll_update = await context.bot.stop_poll(RADIO_CHAT_ID, state.poll_message_id)
                            logger.debug(f"Forced poll {poll.poll.id} to close: {poll_update}")
                            await handle_poll(Update(poll=poll_update), context)
                            break
                        except TelegramError as e:
                            if "Poll has already been closed" in str(e):
                                logger.debug(f"Poll {poll.poll.id} already closed, fetching final updates")
                                updates = await context.bot.get_updates(allowed_updates=["poll"])
                                for update in updates:
                                    if update.poll and update.poll.id == state.active_poll_id:
                                        logger.debug(f"Poll update received: {update.poll}")
                                        await handle_poll(Update(poll=update.poll), context)
                                        break
                                break
                            logger.error(f"Attempt {attempt + 1}: Failed to force close poll {poll.poll.id}: {e}")
                            await asyncio.sleep(2)
                    else:
                        logger.warning(f"Failed to close poll {poll.poll.id} after 3 attempts")
                        max_votes = max(state.poll_votes) if state.poll_votes else 0
                        if max_votes > 0:
                            winning_indices = [i for i, v in enumerate(state.poll_votes) if v == max_votes]
                            selected_genre = state.poll_options[random.choice(winning_indices)]
                            state.genre = selected_genre
                            state.radio_playlist.clear()
                            state.now_playing = None
                            logger.debug(f"Selected genre from votes: {selected_genre}")
                            await context.bot.send_message(RADIO_CHAT_ID, f"🎵 Новый жанр: *{escape_markdown_v2(state.genre.title())}*")
                            await refill_playlist(context)
                            if state.is_on and context.bot_data.get('radio_loop_task'):
                                context.bot_data['radio_loop_task'].cancel()
                                context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
                        else:
                            await context.bot.send_message(RADIO_CHAT_ID, "⚠️ В голосовании никто не участвовал.")
                state.active_poll_id = None
                state.poll_message_id = None
                state.poll_options = []
                state.poll_votes = []
                await save_state_from_botdata(context.bot_data)
            except Exception as e:
                logger.error(f"Error in close_poll_after_timeout for poll {poll.poll.id}: {e}", exc_info=True)
                state.last_error = f"Критическая ошибка голосования: {e}"
                await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Критическая ошибка при завершении голосования: {e}")

        asyncio.create_task(close_poll_after_timeout())
    except TelegramError as e:
        logger.error(f"Failed to start poll: {e}")
        state.last_error = f"Ошибка запуска голосования: {e}"
        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Не удалось запустить голосование: {e}")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    poll_answer: PollAnswer = update.poll_answer
    logger.debug(f"Received poll answer for poll {poll_answer.poll_id}, option: {poll_answer.option_ids}")
    if poll_answer.poll_id == state.active_poll_id and poll_answer.option_ids:
        option_id = poll_answer.option_ids[0]
        if 0 <= option_id < len(state.poll_votes):
            state.poll_votes[option_id] += 1
            logger.debug(f"Updated votes for poll {poll_answer.poll_id}: {state.poll_votes}")
            await save_state_from_botdata(context.bot_data)

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    winning_options = [o.text.lower() for o in update.poll.options if o.voter_count == max_votes]

    if max_votes == 0:
        logger.debug("No votes in poll.")
        await context.bot.send_message(RADIO_CHAT_ID, "В голосовании никто не участвовал. 😔")
    else:
        selected_genre = random.choice(winning_options)
        state.genre = selected_genre
        logger.debug("Clearing playlist before refilling for new genre")
        state.radio_playlist.clear()
        state.now_playing = None
        logger.debug(f"Selected genre: {selected_genre}")
        await context.bot.send_message(RADIO_CHAT_ID, f"🎵 Новый жанр: *{escape_markdown_v2(state.genre.title())}*")
        await refill_playlist(context)
        if not state.is_on:
            logger.debug("Radio is off, turning on after poll")
            state.is_on = True
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        elif context.bot_data.get('radio_loop_task'):
            try:
                context.bot_data['radio_loop_task'].cancel()
                await context.bot_data['radio_loop_task']
                logger.debug("Previous radio_loop task cancelled")
            except asyncio.CancelledError:
                pass
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
            logger.debug("New radio_loop task started")

    state.active_poll_id = None
    state.poll_message_id = None
    state.poll_options = []
    state.poll_votes = []
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Received /start command from user {user_id}")
    state: State = context.bot_data['state']
    state.now_playing = None
    await save_state_from_botdata(context.bot_data)
    await show_menu(update, context)

# --- Bot Lifecycle ---
async def post_init(application: Application):
    application.bot_data['state'] = load_state()
    application.bot_data['skip_event'] = asyncio.Event()
    if application.bot_data['state'].is_on:
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
        await refill_playlist(application)

    try:
        webhook_info = await application.bot.get_webhook_info()
        logger.debug(f"Webhook info: {webhook_info}")
        if webhook_info.url:
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
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not BOT_TOKEN or not RADIO_CHAT_ID or not ADMIN_IDS:
        logger.critical(f"Configuration error: BOT_TOKEN={bool(BOT_TOKEN)}, RADIO_CHAT_ID={RADIO_CHAT_ID}, ADMIN_IDS={ADMIN_IDS}")
        raise ValueError("BOT_TOKEN, RADIO_CHAT_ID или ADMIN_IDS не заданы!")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler(["start", "menu", "m"], show_menu))
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
    logger.info("Starting bot polling...")
    app.run_polling(timeout=3)

if __name__ == "__main__":
    main()
