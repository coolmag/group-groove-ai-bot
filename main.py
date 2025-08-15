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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from dotenv import load_dotenv
from pydantic import BaseModel, model_validator
from cachetools import TTLCache
from aiolimiter import AsyncLimiter
from telegram.error import TelegramError, RetryAfter
from functools import wraps

class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 60
    POLL_DURATION_SECONDS = 60
    MESSAGE_CLEANUP_LIMIT = 30
    MAX_RETRIES = 3
    MIN_DISK_SPACE = 1_000_000_000
    MAX_FILE_SIZE = 50_000_000  # 50 MB

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

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
    "lo-fi": ["lo-fi", "lofi", "chillhop", "chill"],
    "disco": ["disco", "funk", "boogie"],
}

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
    votable_genres: List[str] = list(GENRE_KEYWORDS.keys())
    status_message_id: Optional[int] = None
    now_playing: Optional[dict] = None

    @model_validator(mode='before')
    @classmethod
    def validate_poll_and_status(cls, values):
        if not values.get('is_on', False):
            values['active_poll'] = None
            values['status_message_id'] = None
            values['now_playing'] = None
        return values

def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "--:--"
    minutes, seconds = divmod(int(float(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"

def build_search_queries(genre: str):
    return [f"{genre} music", f"{genre} best tracks", f"{genre} playlist", genre]

def escape_markdown(text: str) -> str:
    special_chars = r'([_*[\]()~`>#+=|{}\.-])'
    return re.sub(special_chars, r'\\\1', text)

async def notify_admins(application: Application, message: str):
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(admin_id, message, parse_mode='MarkdownV2')
        except TelegramError:
            await application.bot.send_message(admin_id, message, parse_mode=None)

def load_config() -> RadioConfig:
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        with config_path.open('r', encoding='utf-8') as f:
            try:
                return RadioConfig(**json.load(f))
            except Exception as e:
                logger.error(f"Error loading config: {e}")
                backup_path = config_path.with_suffix(f'.bak.{int(datetime.now().timestamp())}')
                config_path.rename(backup_path)
                asyncio.create_task(notify_admins(application, f"Ошибка конфигурации: {backup_path}"))
    return RadioConfig()

def save_config(config: RadioConfig):
    config_path = Path(CONFIG_FILE)
    temp_path = config_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(config.model_dump(), f, indent=4, ensure_ascii=False)
        temp_path.replace(config_path)
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        if temp_path.exists():
            temp_path.unlink()
        raise

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵\nИспользуй /play или /ron.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """*Команды*
/play <название> - Поиск трека
/id - ID чата
*Админ-команды*
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
        return InlineKeyboardMarkup([[InlineKeyboardButton("Поиск не найден.", callback_data="noop")]])
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
        'format': 'bestaudio',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'scsearch30',
        'extract_flat': 'in_playlist',
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info.get('entries'):
            await message.edit_text("Треки не найдены.")
            return
        search_id = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('paginated_searches', {})[search_id] = info['entries']
        reply_markup = await get_paginated_keyboard(search_id, context)
        await message.edit_text(f'Найдено: {len(info["entries"])}. Выбери трек:', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in /play: {e}")
        await message.edit_text("Ошибка поиска.")

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update, context):
            if isinstance(update, Update) and update.callback_query:
                await update.callback_query.answer("Только для админов.", show_alert=True)
            else:
                await update.message.reply_text("Только админы могут использовать эту команду.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

@admin_only
async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    genre = " ".join(context.args) if context.args else load_config().genre
    config = load_config()
    config.is_on = True
    config.genre = genre
    config.radio_playlist = []
    config.played_radio_urls = []
    save_config(config)
    if 'radio_task' not in context.bot_data or context.bot_data['radio_task'].done():
        context.bot_data['radio_task'] = asyncio.create_task(radio_loop(context.application))
    if 'voting_task' not in context.bot_data or context.bot_data['voting_task'].done():
        context.bot_data['voting_task'] = asyncio.create_task(hourly_voting_loop(context.application))
    msg = f"Радио включено. Жанр: {genre}"
    try:
        await update.effective_message.reply_text(escape_markdown(msg), parse_mode='MarkdownV2')
    except TelegramError:
        await update.effective_message.reply_text(msg, parse_mode=None)
    await send_status_panel(context.application, update.effective_chat.id, config.status_message_id)
    logger.info(f"Radio started with genre: {genre}")

@admin_only
async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    config.is_on = False
    config.now_playing = None
    save_config(config)
    if 'radio_task' in context.bot_data and not context.bot_data['radio_task'].done():
        context.bot_data['radio_task'].cancel()
    if 'voting_task' in context.bot_data and not context.bot_data['voting_task'].done():
        context.bot_data['voting_task'].cancel()
    await update.effective_message.reply_text("Радио выключено.")
    await send_status_panel(context.application, update.effective_chat.id, config.status_message_id)
    logger.info("Radio stopped")

@admin_only
async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not config.is_on:
        await update.effective_message.reply_text("Радио выключено.")
        return
    if config.active_poll:
        await update.effective_message.reply_text("Голосование уже идет.")
        return
    if await _create_and_send_poll(context.application):
        await update.effective_message.reply_text("Голосование запущено.")
        logger.info("Voting started")
    else:
        await update.effective_message.reply_text("Ошибка голосования.")
        logger.error("Failed to start voting")

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_status_panel(context.application, update.effective_chat.id)
    logger.info("Status panel requested")

@admin_only
async def skip_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'radio_task' in context.bot_data and not context.bot_data['radio_task'].done():
        context.bot_data['radio_task'].cancel()
        context.bot_data['radio_task'] = asyncio.create_task(radio_loop(context.application))
    await update.effective_message.reply_text("Пропускаем трек...")
    logger.info("Track skipped")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    command, data = query.data.split(":", 1)
    logger.info(f"Button callback: {command}, data: {data}")
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
            logger.error(f"Error in button_callback play_track: {e}")
            await query.edit_message_text(f"Ошибка: {e}")
        finally:
            if track_info and os.path.exists(track_info['filepath']):
                os.remove(track_info['filepath'])
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
        await send_status_panel(context.application, query.message.chat_id, query.message.message_id)

rate_limiter = AsyncLimiter(3, 1)

async def send_status_panel(application: Application, chat_id: int, message_id: int = None):
    async with rate_limiter:
        config = load_config()
        now_playing = config.now_playing
        is_on = config.is_on
        status_icon = "🟢" if is_on else "🔴"
        status_text = "В ЭФИРЕ" if is_on else "ВЫКЛЮЧЕНО"
        genre = config.genre
        text = f"Панель управления\n────────────────────────\nСтатус: {status_icon} {status_text}\nЖанр: {genre}"
        if is_on and now_playing:
            title = now_playing.get('title', 'Неизвестный трек')
            duration = format_duration(now_playing.get('duration', 0))
            text += f"\n────────────────────────\nСейчас играет:\n{title}\n{duration}\n────────────────────────"
        else:
            text += "\n────────────────────────\nСейчас играет: — тишина...\n────────────────────────"
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
        text = escape_markdown(text)
        try:
            if message_id:
                await application.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')
            else:
                sent_message = await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')
                config.status_message_id = sent_message.message_id
                save_config(config)
        except TelegramError as e:
            if "message is not modified" not in str(e).lower():
                text = text.replace('\\', '').replace('*', '').replace('`', '')
                if message_id:
                    await application.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode=None)
                else:
                    sent_message = await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=None)
                    config.status_message_id = sent_message.message_id
                    save_config(config)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await send_status_panel(application, chat_id, message_id)

async def download_track(url: str, max_retries: int = Constants.MAX_RETRIES) -> Optional[dict]:
    ensure_download_dir()
    out_template = os.path.join(DOWNLOAD_DIR, f'{uuid.uuid4()}.%(ext)s')
    logger.info(f"Checking availability of {url}")
    try:
        ydl_opts = {
            'format': 'bestaudio',
            'noplaylist': True,
            'quiet': True,
            'extract_flat': 'in_playlist',
            'force_generic_extractor': True,
            'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            logger.error(f"Track not available: {url}")
            return None
    except Exception as e:
        logger.error(f"Failed to check availability of {url}: {e}")
        return None
    for attempt in range(max_retries):
        logger.info(f"Downloading track {url}, attempt {attempt + 1}/{max_retries}")
        try:
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': out_template,
                'noplaylist': True,
                'quiet': False,
                'socket_timeout': 600,
                'fragment_retries': 10,
                'retries': 15,
                'no_check_certificate': True,
                'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                filename = ydl.prepare_filename(info)
                file_size = os.path.getsize(filename) if os.path.exists(filename) else 0
                if file_size > Constants.MAX_FILE_SIZE:
                    logger.error(f"Track too large: {file_size} bytes for {url}")
                    os.remove(filename)
                    return None
                logger.info(f"Downloaded track {info.get('title', 'Unknown')} from {url}")
                return {
                    'filepath': filename,
                    'title': info.get('title', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'url': url
                }
        except Exception as e:
            logger.error(f"Download attempt {attempt + 1} failed for {url}: {e}")
            if attempt == max_retries - 1:
                logger.error(f"Failed to download {url} after {max_retries} attempts")
                await notify_admins(application, f"Не удалось загрузить трек {url}: {e}")
                return None
            await asyncio.sleep(5)
    return None

async def send_track(track_info: dict, chat_id: int, bot):
    filepath = track_info['filepath']
    logger.info(f"Sending track {track_info['title']} from {filepath} to chat {chat_id}")
    try:
        file_size = os.path.getsize(filepath)
        if file_size > Constants.MAX_FILE_SIZE:
            logger.error(f"Track too large: {file_size} bytes for {track_info['title']}")
            return None
        with open(filepath, 'rb') as audio_file:
            sent_message = await bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=track_info['title'],
                duration=track_info['duration']
            )
            logger.info(f"Sent track {track_info['title']} to chat {chat_id}")
            return sent_message
    except Exception as e:
        logger.error(f"Failed to send track {filepath}: {e}")
        await notify_admins(bot.application, f"Не удалось отправить трек {track_info['title']}: {e}")
        return None

async def clear_old_tracks(app: Application):
    radio_msgs = app.bot_data.get('radio_message_ids', deque())
    for _ in range(10):
        if not radio_msgs:
            break
        msg_id = radio_msgs.popleft()
        try:
            await app.bot.delete_message(RADIO_CHAT_ID, msg_id)
        except Exception as e:
            logger.error(f"Failed to delete msg {msg_id}: {e}")

search_cache = TTLCache(maxsize=100, ttl=3600)

async def refill_playlist(application: Application):
    bot_data = application.bot_data
    config = load_config()
    raw_genre = config.genre
    search_queries = build_search_queries(raw_genre)
    played = set(bot_data.get('played_radio_urls', []))
    suitable_tracks = []
    for provider in ['scsearch50', 'ytsearch50']:
        for query in search_queries:
            logger.info(f"Searching: {query} with {provider}")
            cache_key = f"{query}:{raw_genre}:{provider}"
            if cache_key in search_cache:
                info = search_cache[cache_key]
            else:
                ydl_opts = {
                    'format': 'bestaudio',
                    'noplaylist': True,
                    'quiet': True,
                    'default_search': provider,
                    'extract_flat': 'in_playlist',
                    'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                }
                for attempt in range(Constants.MAX_RETRIES):
                    try:
                        async with rate_limiter:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(query, download=False)
                        search_cache[cache_key] = info
                        logger.info(f"Found {len(info.get('entries', []))} entries for {query} with {provider}")
                        break
                    except Exception as e:
                        logger.error(f"Search attempt {attempt + 1}/{Constants.MAX_RETRIES} failed for {query}: {e}")
                        if attempt == Constants.MAX_RETRIES - 1:
                            info = None
                        await asyncio.sleep(5)
            if not info or not info.get('entries'):
                continue
            for t in info['entries']:
                if not t or not t.get('url') or t.get('url') in played:
                    continue
                if not (15 < t.get('duration', 0) < 1800):
                    continue
                suitable_tracks.append(t)
    unique_tracks = {t['url']: t for t in suitable_tracks if t.get('url')}
    suitable_tracks = list(unique_tracks.values())
    final_urls = [t['url'] for t in suitable_tracks[:30]]
    random.shuffle(final_urls)
    logger.info(f"Refilled playlist with {len(final_urls)} tracks")
    bot_data['radio_playlist'] = deque(final_urls)
    config.radio_playlist = list(bot_data['radio_playlist'])
    save_config(config)
    if not final_urls:
        logger.warning(f"No tracks found for '{raw_genre}', switching to 'lo-fi hip hop'")
        config.genre = "lo-fi hip hop"
        config.radio_playlist = []
        bot_data['radio_playlist'] = deque()
        save_config(config)
        await notify_admins(application, f"Не удалось найти треки для '{raw_genre}'. Переключено на 'lo-fi hip hop'.")
        await refill_playlist(application)

async def radio_loop(application: Application):
    bot_data = application.bot_data
    retry_count = 0
    while True:
        config = load_config()
        if not config.is_on:
            logger.info("Radio is off, sleeping for 30s")
            await asyncio.sleep(30)
            continue
        logger.info("Checking playlist")
        await asyncio.sleep(5)
        if not bot_data.get('radio_playlist'):
            logger.info("Playlist empty, refilling")
            await refill_playlist(application)
            if not bot_data.get('radio_playlist'):
                retry_count += 1
                logger.warning(f"Retry {retry_count}/{Constants.MAX_RETRIES}: Failed to refill playlist")
                if retry_count >= Constants.MAX_RETRIES:
                    logger.error("Failed to refill playlist after max retries")
                    await notify_admins(application, "Не удалось заполнить плейлист. Радио остановлено.")
                    config.is_on = False
                    save_config(config)
                    break
                await asyncio.sleep(60)
                continue
            retry_count = 0
        if not bot_data['radio_playlist']:
            logger.error("Playlist is empty after refill")
            await asyncio.sleep(5)
            continue
        track_url = bot_data['radio_playlist'].popleft()
        logger.info(f"Processing track: {track_url}")
        try:
            track_info = await download_track(track_url)
            if track_info:
                sent_msg = await send_track(track_info, RADIO_CHAT_ID, application.bot)
                if sent_msg:
                    bot_data.setdefault('radio_message_ids', deque()).append(sent_msg.message_id)
                    bot_data.setdefault('played_radio_urls', []).append(track_url)
                    if len(bot_data['played_radio_urls']) > 100:
                        bot_data['played_radio_urls'].pop(0)
                    if len(bot_data['radio_message_ids']) >= config.message_cleanup_limit:
                        await clear_old_tracks(application)
                    config.radio_playlist = list(bot_data['radio_playlist'])
                    config.played_radio_urls = bot_data['played_radio_urls']
                    config.radio_message_ids = list(bot_data['radio_message_ids'])
                    config.now_playing = {
                        'title': track_info.get('title', 'Unknown'),
                        'duration': track_info.get('duration', 0)
                    }
                    save_config(config)
                    await send_status_panel(application, RADIO_CHAT_ID, config.status_message_id)
                    logger.info(f"Successfully played track: {track_info['title']}")
                else:
                    logger.warning(f"Failed to send track {track_url}")
            else:
                logger.warning(f"Failed to download track {track_url}")
        except Exception as e:
            logger.error(f"Radio loop error for {track_url}: {e}")
            await notify_admins(application, f"Ошибка в radio_loop для {track_url}: {e}")
        finally:
            if track_info and os.path.exists(track_info['filepath']):
                os.remove(track_info['filepath'])
        logger.info(f"Sleeping for {config.track_interval_seconds}s before next track")
        await asyncio.sleep(config.track_interval_seconds)

async def hourly_voting_loop(application: Application):
    while True:
        try:
            config = load_config()
            await asyncio.sleep(config.voting_interval_seconds)
            if config.is_on and not config.active_poll:
                await _create_and_send_poll(application)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Voting loop error: {e}")
            await asyncio.sleep(60)

async def _create_and_send_poll(application: Application) -> bool:
    config = load_config()
    try:
        votable_genres = config.votable_genres
        if len(votable_genres) < 2:
            logger.error("Not enough genres for voting")
            return False
        decades = ["70-х", "80-х", "90-х", "2000-х", "2010-х"]
        special = {f"{random.choice(votable_genres)} {random.choice(decades)}" for _ in range(5)}
        regular_pool = [g for g in votable_genres if g not in {s.split(' ')[0] for s in special} and g.lower() != 'pop']
        num_to_sample = min(4, len(regular_pool))
        regular = set(random.sample(regular_pool, k=num_to_sample)) if regular_pool else set()
        options = list(special | regular)
        while len(options) < 9:
            chosen = random.choice(votable_genres)
            if chosen not in options:
                options.append(chosen)
        options.append("Pop")
        random.shuffle(options)
        poll_duration = config.poll_duration_seconds
        message = await application.bot.send_poll(RADIO_CHAT_ID, "Выбери жанр на следующий час!", options[:10], is_anonymous=False, open_period=poll_duration)
        poll_data = message.poll.to_dict()
        poll_data['close_timestamp'] = datetime.now().timestamp() + poll_duration
        config.active_poll = poll_data
        save_config(config)
        asyncio.create_task(schedule_poll_processing(application, message.poll.id, poll_duration))
        logger.info("Poll created successfully")
        return True
    except Exception as e:
        logger.error(f"Create poll error: {e}")
        await notify_admins(application, f"Ошибка создания голосования: {e}")
        return False

async def schedule_poll_processing(application: Application, poll_id: str, delay: int):
    await asyncio.sleep(delay + 2)
    config = load_config()
    active_poll_dict = config.active_poll
    if not active_poll_dict or active_poll_dict['id'] != poll_id:
        return
    try:
        poll = Poll.from_dict(active_poll_dict, application.bot)
        await process_poll_results(poll, application)
    except Exception as e:
        logger.error(f"Failed to process poll {poll_id}: {e}")
        config.active_poll = None
        save_config(config)

async def receive_poll_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    active_poll_dict = config.active_poll
    if active_poll_dict and active_poll_dict['id'] == update.poll.id:
        if active_poll_dict.get('close_timestamp', 0) > datetime.now().timestamp():
            config.active_poll = update.poll.to_dict()
            save_config(config)
            logger.info(f"Poll updated: {update.poll.id}")

async def process_poll_results(poll, application: Application):
    config = load_config()
    config.active_poll = None
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
    final_winner = config.genre if not winning_options else random.choice(winning_options)
    config.genre = final_winner
    config.radio_playlist = []
    application.bot_data['radio_playlist'].clear()
    config.now_playing = None
    save_config(config)
    msg = f"Голосование завершено! Играет: {final_winner}"
    try:
        await application.bot.send_message(RADIO_CHAT_ID, escape_markdown(msg), parse_mode='MarkdownV2')
    except TelegramError:
        await application.bot.send_message(RADIO_CHAT_ID, msg, parse_mode=None)
    await refill_playlist(application)
    if 'radio_task' not in application.bot_data or application.bot_data['radio_task'].done():
        application.bot_data['radio_task'] = asyncio.create_task(radio_loop(application))
    logger.info(f"Poll processed, new genre: {final_winner}")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_user.id in ADMIN_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in ('administrator', 'creator')
    except Exception:
        return False

async def cleanup_download_dir():
    while True:
        try:
            total, used, free = shutil.disk_usage(DOWNLOAD_DIR)
            logger.info(f"Disk space - Total: {total / 1e9:.2f}GB, Used: {used / 1e9:.2f}GB, Free: {free / 1e9:.2f}GB")
            if free < Constants.MIN_DISK_SPACE:
                for file in Path(DOWNLOAD_DIR).glob('*'):
                    file.unlink()
            for file in Path(DOWNLOAD_DIR).glob('*'):
                if file.stat().st_mtime < datetime.now().timestamp() - 3600:
                    file.unlink()
        except Exception as e:
            logger.error(f"Error cleaning up download directory: {e}")
        await asyncio.sleep(3600)

async def cleanup_cache():
    while True:
        search_cache.clear()
        await asyncio.sleep(24 * 3600)

async def post_init(application: Application) -> None:
    config = load_config()
    bot_data = application.bot_data
    bot_data['radio_playlist'] = deque(config.radio_playlist)
    bot_data['played_radio_urls'] = config.played_radio_urls
    bot_data['radio_message_ids'] = deque(config.radio_message_ids)
    if config.is_on:
        bot_data['radio_task'] = asyncio.create_task(radio_loop(application))
        bot_data['voting_task'] = asyncio.create_task(hourly_voting_loop(application))
    active_poll = config.active_poll
    if active_poll:
        close_timestamp = active_poll.get('close_timestamp')
        if close_timestamp:
            remaining_time = close_timestamp - datetime.now().timestamp()
            if remaining_time > 0:
                asyncio.create_task(schedule_poll_processing(application, active_poll['id'], remaining_time))
    asyncio.create_task(cleanup_download_dir())
    asyncio.create_task(cleanup_cache())
    logger.info("Bot initialized")

async def shutdown(application: Application):
    config = load_config()
    tasks = []
    if 'radio_task' in application.bot_data:
        tasks.append(application.bot_data['radio_task'])
    if 'voting_task' in application.bot_data:
        tasks.append(application.bot_data['voting_task'])
    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    save_config(config)
    logger.info("Bot shutdown")

def main() -> None:
    if not BOT_TOKEN:
        logger.error("FATAL: BOT_TOKEN not found.")
        return
    try:
        application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(shutdown).build()
        handlers = [
            CommandHandler("start", start_command),
            CommandHandler(["help", "h"], help_command),
            CommandHandler("id", id_command),
            CommandHandler(["play", "p"], play_command),
            CommandHandler(["ron"], radio_on_command),
            CommandHandler(["rof"], radio_off_command),
            CommandHandler("votestart", start_vote_command),
            CommandHandler("status", status_command),
            CommandHandler("skip", skip_track),
            CallbackQueryHandler(button_callback),
            PollHandler(receive_poll_update)
        ]
        application.add_handlers(handlers)
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == "__main__":
    ensure_download_dir()
    main()
