import logging
import os
import asyncio
import json
import random
import re
import uuid
from pathlib import Path
from typing import List, Optional
from collections import deque
from datetime import datetime
import yt_dlp
import shutil
import httpx
import ffmpeg

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from telegram.helpers import escape_markdown
from telegram.error import TelegramError, RetryAfter
from dotenv import load_dotenv
from pydantic import BaseModel, model_validator
from cachetools import TTLCache
from aiolimiter import AsyncLimiter
from functools import wraps
from asyncio import Lock

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 30
    POLL_DURATION_SECONDS = 60
    MESSAGE_CLEANUP_LIMIT = 30
    MAX_RETRIES = 5
    MIN_DISK_SPACE = 1_000_000_000  # 1GB
    MAX_FILE_SIZE = 50_000_000      # 50MB
    DEBOUNCE_SECONDS = 5
    MAX_DURATION = 900              # 15 minutes
    MIN_DURATION = 30               # 30 seconds

# --- Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"
PROXY_URL = os.getenv("PROXY_URL", None)

GENRE_KEYWORDS = {
    "electronic": ["electronic", "synth", "synthwave", "edm", "house", "techno", "trance"],
    "rock": ["rock", "punk", "metal", "grunge", "alternative"],
    "pop": ["pop", "dance pop", "electropop"],
    "hip": ["hip hop", "rap", "trap", "r&b"],
    "jazz": ["jazz", "swing", "smooth jazz"],
    "blues": ["blues", "rhythm and blues"],
    "classical": ["classical", "orchestra", "symphony"],
    "reggae": ["reggae", "ska", "dub"],
    "country": ["country", "bluegrass", "folk"],
    "metal": ["metal", "heavy metal", "death metal"],
}

# --- Pydantic Models ---
class RadioConfig(BaseModel):
    is_on: bool = False
    genre: str = "lo-fi hip hop"
    radio_playlist: List[str] = []
    played_radio_urls: List[str] = []
    radio_message_ids: List[int] = []
    voting_interval_seconds: int = Constants.VOTING_INTERVAL_SECONDS
    track_interval_seconds: int = Constants.TRACK_INTERVAL_SECONDS
    message_cleanup_limit: int = Constants.MESSAGE_CLEANUP_LIMIT
    poll_duration_seconds: int = Constants.POLL_DURATION_SECONDS
    active_poll: Optional[dict] = None
    votable_genres: List[str] = ["pop", "rock", "hip hop", "electronic", "classical", "jazz", "blues", "country", "metal", "reggae", "folk", "indie", "rap", "r&b", "soul", "funk", "disco", "punk rock", "alternative rock", "post-punk", "ambient", "drum and bass", "techno", "trance", "house", "dubstep", "grime", "trip-hop", "acid jazz", "swing", "bluegrass", "blues rock", "folk rock", "post-rock", "shoegaze", "garage rock", "britpop", "k-pop", "j-pop", "latin", "bossa nova", "samba", "reggaeton", "rockabilly"]
    status_message_id: Optional[int] = None
    now_playing: Optional[dict] = None
    last_toggle: float = 0.0

    @model_validator(mode='before')
    @classmethod
    def validate_poll_and_status(cls, values):
        if not values.get('is_on', False):
            values['active_poll'] = None
            values['status_message_id'] = None
            values['now_playing'] = None
        return values

# --- Globals ---
status_lock = Lock()
poll_lock = Lock()
rate_limiter = AsyncLimiter(5, 1)
search_cache = TTLCache(maxsize=100, ttl=3600)
application_instance = None

# --- Helper Functions ---
def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "--:--"
    minutes, seconds = divmod(int(float(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"

def build_search_queries(genre: str):
    queries = [
        f"{genre} song -live -stream -playlist -mix -album",
        f"{genre} track -live -stream -playlist -mix -album",
        f"{genre} best tracks -live -stream -playlist -mix -album",
        f"{genre} music -live -stream -playlist -mix -album"
    ]
    if genre.lower() == "lo-fi hip hop":
        queries.extend([
            "lofi song -live -stream -playlist -mix -album",
            "chill song -live -stream -playlist -mix -album",
            "chillhop track -live -stream -playlist -mix -album"
        ])
    return queries

async def notify_admins(application: Application, message: str):
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(admin_id, escape_markdown(message, version=2), parse_mode='MarkdownV2')
        except TelegramError:
            try:
                await application.bot.send_message(admin_id, message, parse_mode=None)
            except Exception as e:
                logger.error(f"Failed to send fallback notification to admin {admin_id}: {e}")

def load_config() -> RadioConfig:
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        with config_path.open('r', encoding='utf-8') as f:
            try:
                return RadioConfig(**json.load(f))
            except Exception as e:
                backup_path = config_path.with_suffix(f'.bak.{int(datetime.now().timestamp())}')
                config_path.rename(backup_path)
                logger.error(f"Configuration error, backed up to {backup_path}: {e}")
                if application_instance:
                    asyncio.create_task(notify_admins(application_instance, f"Ошибка конфигурации: {backup_path}"))
    return RadioConfig()

def save_config(config: RadioConfig):
    config_path = Path(CONFIG_FILE)
    temp_path = config_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(config.model_dump(), f, indent=4, ensure_ascii=False)
        temp_path.replace(config_path)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        logger.error(f"Failed to save config: {e}")
        raise

def ensure_download_dir():
    Path(DOWNLOAD_DIR).mkdir(exist_ok=True)

# --- Admin Check ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    logger.info(f"Checking admin status for user_id: {user_id}")

    if user_id in ADMIN_IDS:
        logger.info(f"User {user_id} found in ADMIN_IDS. Granting admin access.")
        return True

    logger.info(f"User {user_id} not in ADMIN_IDS. Checking chat admin status in chat_id: {RADIO_CHAT_ID}.")
    if RADIO_CHAT_ID == 0:
        logger.warning("RADIO_CHAT_ID is not set. Cannot check for chat admin status.")
        return False

    try:
        member = await context.bot.get_chat_member(RADIO_CHAT_ID, user_id)
        status = member.status
        logger.info(f"User {user_id} has status '{status}' in chat {RADIO_CHAT_ID}.")
        if status in ('administrator', 'creator'):
            logger.info("Granting admin access based on chat status.")
            return True
        else:
            logger.info("User is not a chat admin.")
            return False
    except Exception as e:
        logger.error(f"Failed to check chat admin status for user {user_id}: {e}")
        return False

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update, context):
            if update.callback_query:
                await update.callback_query.answer("Эта команда только для администраторов.", show_alert=True)
            elif update.message:
                await update.message.reply_text("Эта команда только для администраторов.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵\nИспользуй /play или /ron.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """*Команды:*
/play <название> - Поиск трека
/id - ID чата

*Админ-команды:*
/ron <жанр> - Включить радио
/rof - Выключить радио
/votestart - Голосование
/status - Панель управления
/skip - Пропустить трек"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID чата: `{update.message.chat_id}`", parse_mode='Markdown')

async def get_paginated_keyboard(search_id: str, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    page_size = 5
    results = context.bot_data.get('paginated_searches', {}).get(search_id, [])
    if not results:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Поиск не найден.", callback_data="noop:0")]])
    start_index = page * page_size
    keyboard = []
    for entry in results[start_index : start_index + page_size]:
        title = entry.get('title', 'Unknown')
        duration = format_duration(entry.get('duration'))
        cache_key = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('track_urls', {})[cache_key] = entry.get('url')
        keyboard.append([InlineKeyboardButton(f"▶️ {title} ({duration})", callback_data=f"play_track:{cache_key}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{search_id}:{page-1}"))
    if (page + 1) * page_size < len(results):
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page:{search_id}:{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    return InlineKeyboardMarkup(keyboard)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи название: `/play <название>`", parse_mode='Markdown')
        return
    query = " ".join(context.args)
    message = await update.message.reply_text(f'Ищу "{query}"...')
    ydl_opts = {
        'format': 'bestaudio', 'noplaylist': True, 'quiet': True,
        'default_search': 'ytsearch30', 'extract_flat': 'in_playlist',
        'match_filter': lambda info: None if Constants.MIN_DURATION < info.get('duration', 0) <= Constants.MAX_DURATION else 'Duration out of range',
        'retries': Constants.MAX_RETRIES, 'proxy': PROXY_URL,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
        if not info.get('entries'):
            await message.edit_text("Треки не найдены.")
            return
        search_id = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('paginated_searches', {})[search_id] = [
            {'url': t['url'], 'title': t['title'], 'duration': t['duration']} for t in info['entries']
        ]
        reply_markup = await get_paginated_keyboard(search_id, context)
        await message.edit_text(f'Найдено: {len(info["entries"])}. Выбери трек:', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Search error for query '{query}': {e}")
        await message.edit_text("Ошибка поиска. Попробуйте другую фразу.")

@admin_only
async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    current_time = datetime.now().timestamp()
    if current_time - config.last_toggle < Constants.DEBOUNCE_SECONDS:
        await update.effective_message.reply_text(f"Пожалуйста, подождите...")
        return
    config.last_toggle = current_time
    config.is_on = True
    genre = " ".join(context.args) if context.args else config.genre
    if genre.lower() not in GENRE_KEYWORDS and genre.lower() != "lo-fi hip hop":
        await update.effective_message.reply_text(f"Недопустимый жанр: {genre}.")
        return
    config.genre = genre
    config.radio_playlist = []
    config.played_radio_urls = []
    config.now_playing = None
    save_config(config)
    if 'radio_task' not in context.bot_data or context.bot_data['radio_task'].done():
        context.bot_data['radio_task'] = asyncio.create_task(radio_loop(context.application))
    if 'voting_task' not in context.bot_data or context.bot_data['voting_task'].done():
        context.bot_data['voting_task'] = asyncio.create_task(hourly_voting_loop(context.application))
    await update.effective_message.reply_text(f"Радио включено. Жанр: {genre}")
    async with status_lock:
        await send_status_panel(context.application, update.effective_chat.id)

@admin_only
async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    current_time = datetime.now().timestamp()
    if current_time - config.last_toggle < Constants.DEBOUNCE_SECONDS:
        await update.effective_message.reply_text("Пожалуйста, подождите...")
        return
    config.last_toggle = current_time
    config.is_on = False
    config.now_playing = None
    save_config(config)
    if 'radio_task' in context.bot_data and not context.bot_data['radio_task'].done():
        context.bot_data['radio_task'].cancel()
    if 'voting_task' in context.bot_data and not context.bot_data['voting_task'].done():
        context.bot_data['voting_task'].cancel()
    await update.effective_message.reply_text("Радио выключено.")
    async with status_lock:
        await send_status_panel(context.application, update.effective_chat.id, config.status_message_id)

@admin_only
async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not config.is_on:
        await update.effective_message.reply_text("Радио выключено.")
        return
    async with poll_lock:
        if config.active_poll:
            await update.effective_message.reply_text("Голосование уже идет.")
            return
        if await _create_and_send_poll(context.application):
            await update.effective_message.reply_text("Голосование запущено.")
        else:
            await update.effective_message.reply_text("Ошибка голосования.")

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with status_lock:
        await send_status_panel(context.application, update.effective_chat.id)

@admin_only
async def skip_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    config.now_playing = None
    save_config(config)
    if 'radio_task' in context.bot_data and not context.bot_data['radio_task'].done():
        context.bot_data['radio_task'].cancel()
        context.bot_data['radio_task'] = asyncio.create_task(radio_loop(context.application))
    await update.effective_message.reply_text("Пропускаем трек...")
    async with status_lock:
        await send_status_panel(context.application, update.effective_chat.id, config.status_message_id)

# --- Callback & Poll Handlers ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    
    try:
        command, data = query.data.split(":", 1)
    except ValueError:
        logger.warning(f"Received invalid callback_data: {query.data}")
        return

    if command == "play_track":
        track_url = context.bot_data.get('track_urls', {}).get(data)
        if not track_url:
            await query.edit_message_text("Трек устарел.")
            return
        await query.edit_message_text("Обрабатываю...")
        try:
            track_info = await download_track(track_url)
            if track_info:
                await send_track(track_info, query.message.chat_id, context.bot)
                await query.edit_message_text("Трек отправлен!")
            else:
                await query.edit_message_text("Не удалось загрузить трек.")
        except Exception as e:
            logger.error(f"Error in play_track for URL {track_url}: {e}")
            await query.edit_message_text(f"Ошибка: {e}")
    
    elif command == "page":
        search_id, page_num_str = data.split(":")
        page = int(page_num_str)
        reply_markup = await get_paginated_keyboard(search_id, context, page)
        await query.edit_message_text('Выбери трек:', reply_markup=reply_markup)

    elif command == "toggle_radio":
        config = load_config()
        if config.is_on:
            await radio_off_command(update, context)
        else:
            await radio_on_command(update, context)

    elif command == "skip_track":
        await skip_track(update, context)

    elif command == "start_vote":
        await start_vote_command(update, context)

    elif command == "status_refresh":
        async with status_lock:
            await send_status_panel(context.application, query.message.chat_id, query.message.message_id)

async def send_status_panel(application: Application, chat_id: int, message_id: int = None):
    async with rate_limiter:
        config = load_config()
        now_playing = config.now_playing
        is_on = config.is_on
        status_icon = "🟢" if is_on else "🔴"
        status_text = "В ЭФИРЕ" if is_on else "ВЫКЛЮЧЕНО"
        genre = config.genre

        original_text = f"Панель управления\nСтатус: {status_icon} {status_text}\nЖанр: {genre}"
        if is_on and now_playing and isinstance(now_playing, dict) and 'title' in now_playing:
            title = now_playing.get('title', 'Unknown')
            duration = format_duration(now_playing.get('duration', 0))
            original_text += f"\nСейчас играет: {title} ({duration})"
        else:
            original_text += "\nСейчас играет: — Подготовка следующего трека..."

        keyboard = []
        if is_on:
            keyboard.append([
                InlineKeyboardButton("⏭ Пропустить", callback_data="skip_track:0"),
                InlineKeyboardButton("⏹️ Стоп", callback_data="toggle_radio:0")
            ])
            keyboard.append([InlineKeyboardButton("🗳️ Голосование", callback_data="start_vote:0")])
        else:
            keyboard.append([InlineKeyboardButton("▶️ Запустить", callback_data="toggle_radio:0")])
        keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="status_refresh:0")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        async def send_or_edit(text_content, parse_mode=None):
            try:
                if message_id:
                    await application.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=text_content,
                        reply_markup=reply_markup, parse_mode=parse_mode
                    )
                else:
                    new_msg = await application.bot.send_message(
                        chat_id=chat_id, text=text_content, reply_markup=reply_markup, parse_mode=parse_mode
                    )
                    config.status_message_id = new_msg.message_id
                    save_config(config)
            except TelegramError as e:
                if "message is not modified" in str(e).lower(): return
                logger.warning(f"Editing message {message_id} failed, sending new one. Error: {e}")
                new_msg = await application.bot.send_message(
                    chat_id=chat_id, text=text_content, reply_markup=reply_markup, parse_mode=parse_mode
                )
                config.status_message_id = new_msg.message_id
                save_config(config)

        try:
            await send_or_edit(escape_markdown(original_text, version=2), parse_mode='MarkdownV2')
        except (TelegramError, RetryAfter) as e:
            if isinstance(e, RetryAfter):
                await asyncio.sleep(e.retry_after)
                await send_or_edit(original_text, parse_mode=None)
            elif "message is not modified" not in str(e).lower():
                logger.warning(f"MarkdownV2 failed for status panel, falling back to plain text. Error: {e}")
                await send_or_edit(original_text, parse_mode=None)

async def download_track(url: str, max_retries: int = Constants.MAX_RETRIES) -> Optional[dict]:
    ensure_download_dir()
    temp_file_tmpl = Path(DOWNLOAD_DIR) / f'{uuid.uuid4()}.%(ext)s'
    
    ydl_opts = {
        'format': 'bestaudio/best', 'outtmpl': str(temp_file_tmpl),
        'noplaylist': True, 'quiet': True, 'noprogress': True,
        'socket_timeout': 30, 'fragment_retries': 10, 'retries': max_retries,
        'no_check_certificate': True, 'geo_bypass': True, 'proxy': PROXY_URL,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'}],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filename_str = str(temp_file_tmpl).replace('.%(ext)s', '.mp3')
            filename = Path(filename_str)
            if not filename.exists():
                logger.error(f"Download failed: File {filename} not found")
                return None
            if filename.stat().st_size > Constants.MAX_FILE_SIZE:
                filename.unlink()
                logger.error(f"File {filename} exceeds size limit")
                return None
            return {'filepath': str(filename), 'title': info.get('title', 'Unknown'), 'duration': info.get('duration', 0), 'url': url}
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        if "Private video" in str(e):
            await notify_admins(application_instance, f"Не удалось скачать приватное видео: {url}")
        return None

async def send_track(track_info: dict, chat_id: int, bot):
    filepath = Path(track_info.get('filepath'))
    if not filepath.exists():
        logger.error(f"Track file missing: {filepath}")
        return None
    try:
        with open(filepath, 'rb') as audio_file:
            return await bot.send_audio(
                chat_id=chat_id, audio=audio_file,
                title=track_info.get('title', 'Unknown'),
                duration=int(track_info.get('duration', 0))
            )
    except Exception as e:
        logger.error(f"Failed to send track {filepath.name}: {e}")
        return None
    finally:
        if filepath.exists():
            filepath.unlink()

async def clear_old_tracks(app: Application):
    config = load_config()
    while len(config.radio_message_ids) > Constants.MESSAGE_CLEANUP_LIMIT:
        msg_id = config.radio_message_ids.pop(0)
        try: await app.bot.delete_message(RADIO_CHAT_ID, msg_id)
        except Exception: pass
    save_config(config)

async def refill_playlist(application: Application):
    config = load_config()
    search_queries = build_search_queries(config.genre)
    played = set(config.played_radio_urls)
    
    for query in search_queries:
        ydl_opts = {
            'format': 'bestaudio', 'noplaylist': True, 'quiet': True,
            'default_search': 'ytsearch50', 'extract_flat': 'in_playlist',
            'match_filter': lambda info: None if Constants.MIN_DURATION < info.get('duration', 0) <= Constants.MAX_DURATION else 'Duration out of range',
            'retries': Constants.MAX_RETRIES, 'proxy': PROXY_URL,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
            
            entries = info.get('entries', [])
            unplayed_tracks = [t for t in entries if t and t.get('url') and t.get('url') not in played]
            
            if unplayed_tracks:
                random.shuffle(unplayed_tracks)
                config.radio_playlist.extend([t['url'] for t in unplayed_tracks])
                logger.info(f"Refilled playlist with {len(unplayed_tracks)} tracks for genre '{config.genre}'.")
                save_config(config)
                return
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
    
    logger.warning(f"Could not find any tracks for genre '{config.genre}'.")
    await notify_admins(application, f"Не удалось найти треки для жанра '{config.genre}'.")


async def radio_loop(application: Application):
    while True:
        try:
            config = load_config()
            if not config.is_on:
                await asyncio.sleep(10)
                continue

            if not config.radio_playlist:
                await refill_playlist(application)
                config = load_config() # Re-load config after refill
                if not config.radio_playlist:
                    logger.warning("Playlist is still empty after refill attempt. Waiting.")
                    await asyncio.sleep(30)
                    continue

            track_url = config.radio_playlist.pop(0)
            logger.info(f"Playing track: {track_url}")
            track_info = await download_track(track_url)
            
            if track_info:
                sent_msg = await send_track(track_info, RADIO_CHAT_ID, application.bot)
                if sent_msg:
                    config.radio_message_ids.append(sent_msg.message_id)
                    config.played_radio_urls.append(track_url)
                    if len(config.played_radio_urls) > 200: # Keep last 200 played
                        config.played_radio_urls = config.played_radio_urls[-200:]
                    
                    config.now_playing = {'title': track_info.get('title'), 'duration': track_info.get('duration'), 'url': track_url}
                    save_config(config)
                    await clear_old_tracks(application)
                    async with status_lock:
                        await send_status_panel(application, RADIO_CHAT_ID, config.status_message_id)
                    await asyncio.sleep(config.track_interval_seconds)
                else:
                    save_config(config) # Save popped playlist
            else:
                save_config(config) # Save popped playlist
        except asyncio.CancelledError:
            logger.info("Radio loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in radio_loop: {e}", exc_info=True)
            await notify_admins(application, f"Критическая ошибка в radio_loop: {e}")
            await asyncio.sleep(10)

async def hourly_voting_loop(application: Application):
    while True:
        try:
            config = load_config()
            if not config.is_on:
                await asyncio.sleep(60)
                continue
            
            async with poll_lock:
                if not config.active_poll:
                    await _create_and_send_poll(application)
            
            await asyncio.sleep(config.voting_interval_seconds)
        except asyncio.CancelledError:
            logger.info("Voting loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in hourly_voting_loop: {e}", exc_info=True)
            await notify_admins(application, f"Критическая ошибка в hourly_voting_loop: {e}")
            await asyncio.sleep(60)

async def _create_and_send_poll(application: Application):
    config = load_config()
    try:
        votable_genres = config.votable_genres
        if len(votable_genres) < 2: return

        decades = ["70-х", "80-х", "90-х", "2000-х", "2010-х"]
        special = {f"{random.choice(votable_genres)} {random.choice(decades)}" for _ in range(5)}
        options = list(special | set(random.sample(votable_genres, k=min(5, len(votable_genres)))))
        random.shuffle(options)
        
        message = await application.bot.send_poll(
            RADIO_CHAT_ID, "Выбери жанр на следующий час!", options[:10],
            is_anonymous=False, open_period=config.poll_duration_seconds
        )
        poll_data = message.poll.to_dict()
        poll_data['close_timestamp'] = datetime.now().timestamp() + config.poll_duration_seconds
        config.active_poll = poll_data
        save_config(config)
        asyncio.create_task(schedule_poll_processing(application, message.poll.id, config.poll_duration_seconds))
        return True
    except Exception as e:
        logger.error(f"Error creating poll: {e}")
        return False

async def schedule_poll_processing(application: Application, poll_id: str, delay: int):
    await asyncio.sleep(delay + 5) # 5s grace period
    config = load_config()
    if config.active_poll and config.active_poll.get('id') == poll_id:
        logger.info(f"Processing poll {poll_id} after scheduled delay.")
        # The poll object is fetched via receive_poll_update, we just need to trigger processing
        # by setting active_poll to None and saving. The receive_poll_update will handle the final result.
        # This is a fallback in case the final poll update is missed.
        await process_poll_results(Poll.from_dict(config.active_poll, application.bot), application)


async def receive_poll_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not config.active_poll or config.active_poll.get('id') != update.poll.id:
        return
    
    config.active_poll = update.poll.to_dict()
    save_config(config)

    if update.poll.is_closed:
        logger.info(f"Processing closed poll: {update.poll.id}")
        await process_poll_results(update.poll, context.application)

async def process_poll_results(poll: Poll, application: Application):
    async with poll_lock:
        config = load_config()
        if not config.active_poll or config.active_poll.get('id') != poll.id:
            return # Already processed
        
        config.active_poll = None # Mark as processed
        
        if not config.is_on:
            save_config(config)
            return

        winning_options = []
        max_votes = 0
        for option in poll.options:
            if option.voter_count > max_votes:
                max_votes = option.voter_count
                winning_options = [option.text]
            elif option.voter_count == max_votes and max_votes > 0:
                winning_options.append(option.text)
        
        final_winner = config.genre
        if winning_options:
            final_winner = random.choice(winning_options)
        
        config.genre = final_winner
        config.radio_playlist = []
        config.now_playing = None
        save_config(config)

        msg = f"Голосование завершено! Играет: {final_winner}"
        await application.bot.send_message(RADIO_CHAT_ID, msg)
        
        if 'radio_task' in application.bot_data and not application.bot_data['radio_task'].done():
            application.bot_data['radio_task'].cancel()
        application.bot_data['radio_task'] = asyncio.create_task(radio_loop(application))


async def cleanup_routines():
    while True:
        # Cleanup download directory
        try:
            total, used, free = shutil.disk_usage(DOWNLOAD_DIR)
            if free < Constants.MIN_DISK_SPACE:
                for file in Path(DOWNLOAD_DIR).glob('*'):
                    try: file.unlink()
                    except OSError: pass
            
            for file in Path(DOWNLOAD_DIR).glob('*'):
                if file.stat().st_mtime < datetime.now().timestamp() - 3600:
                    try: file.unlink()
                    except OSError: pass
        except Exception as e:
            logger.error(f"Error in cleanup_download_dir: {e}")

        # Cleanup cache
        try:
            search_cache.clear()
        except Exception as e:
            logger.error(f"Error in cleanup_cache: {e}")
            
        await asyncio.sleep(3600)

# --- Bot Lifecycle ---
async def post_init(application: Application):
    global application_instance
    application_instance = application
    await application.bot.delete_webhook(drop_pending_updates=True)
    
    config = load_config()
    if config.is_on:
        application.bot_data['radio_task'] = asyncio.create_task(radio_loop(application))
        application.bot_data['voting_task'] = asyncio.create_task(hourly_voting_loop(application))
    
    if config.active_poll:
        remaining_time = config.active_poll.get('close_timestamp', 0) - datetime.now().timestamp()
        if remaining_time > 0:
            asyncio.create_task(schedule_poll_processing(application, config.active_poll['id'], remaining_time))

    asyncio.create_task(cleanup_routines())

async def shutdown(application: Application):
    logger.info("Shutting down...")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    save_config(load_config())

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

def main() -> None:
    ensure_download_dir()
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN not set.")
        return
    if not shutil.which("ffmpeg"):
        logger.critical("FATAL: ffmpeg binary not found.")
        return

    application = (
        Application.builder().token(BOT_TOKEN)
        .post_init(post_init).post_shutdown(shutdown)
        .read_timeout(60).write_timeout(60).build()
    )

    application.add_error_handler(error_handler)
    
    handlers = [
        CommandHandler("start", start_command),
        CommandHandler(["help", "h"], help_command),
        CommandHandler("id", id_command),
        CommandHandler(["play", "p"], play_command),
        CommandHandler("ron", radio_on_command),
        CommandHandler("rof", radio_off_command),
        CommandHandler("votestart", start_vote_command),
        CommandHandler("status", status_command),
        CommandHandler("skip", skip_track),
        CallbackQueryHandler(button_callback),
        PollHandler(receive_poll_update)
    ]
    application.add_handlers(handlers)
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except telegram.error.Conflict:
        logger.critical("="*50)
        logger.critical("ОШИБКА: ОБНАРУЖЕНА ДРУГАЯ КОПИЯ БОТА!")
        logger.critical("Убедитесь, что бот с этим токеном запущен ТОЛЬКО В ОДНОМ МЕСТЕ.")
        logger.critical("Остановите все другие копии и перезапустите эту.")
        logger.critical("="*50)
    except Exception as e:
        logger.critical(f"A critical error occurred while running the bot: {e}", exc_info=True)

if __name__ == "__main__":
    main()