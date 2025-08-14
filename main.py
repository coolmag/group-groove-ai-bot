import logging
import os
import asyncio
import json
import random
import re
import yt_dlp
import uuid
from types import SimpleNamespace
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Message, Poll
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from dotenv import load_dotenv
from collections import deque
from pydantic import BaseModel, root_validator
from typing import List, Deque, Optional
from pathlib import Path
import time
import shutil
from cachetools import TTLCache
from aiolimiter import AsyncLimiter
from telegram.error import TelegramError, RetryAfter
from yt_dlp.utils import DownloadError
from functools import wraps
import signal

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 120
    POLL_DURATION_SECONDS = 60
    MESSAGE_CLEANUP_LIMIT = 30
    MAX_RETRIES = 3
    MIN_DISK_SPACE = 1_000_000_000  # 1GB

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]
except ValueError as e:
    logger.error(f"Invalid ADMIN_IDS format: {e}")
    ADMIN_IDS = []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

# --- Genre Definitions ---
GENRE_KEYWORDS = {
    "electronic": ["electronic", "synth", "synthwave", "synth pop", "new wave", "edm", "house", "techno", "trance", "dance"],
    "rock": ["rock", "punk", "hard rock", "metal", "grunge", "garage", "alternative"],
    "pop": ["pop", "dance pop", "synthpop", "electropop"],
    "hip": ["hip hop", "rap", "trap", "r&b", "hiphop"],
    "jazz": ["jazz", "swing", "smooth jazz", "fusion"],
    "blues": ["blues", "rhythm and blues", "r&b"],
    "classical": ["classical", "orchestra", "symphony", "piano", "violin"],
    "reggae": ["reggae", "ska", "dub"],
    "country": ["country", "bluegrass", "folk"],
    "metal": ["metal", "heavy metal", "death metal", "black metal", "thrash"],
    "lo-fi": ["lo-fi", "lofi", "chillhop", "chill"],
    "disco": ["disco", "funk", "boogie"],
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
    votable_genres: List[str] = list(GENRE_KEYWORDS.keys())
    status_message_id: Optional[int] = None
    now_playing: Optional[dict] = None

    @root_validator
    def validate_poll_and_status(cls, values):
        if not values.get('is_on'):
            values['active_poll'] = None
            values['status_message_id'] = None
            values['now_playing'] = None
        return values

# --- Helper Functions ---
def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "--:--"
    minutes, seconds = divmod(int(float(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"

def has_ukrainian_chars(text: str) -> bool:
    return bool(re.search(r"[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]", text))

def is_genre_match(track: dict, genre: str) -> bool:
    title = track.get('title', '').lower()
    for key, keywords in GENRE_KEYWORDS.items():
        if key in genre.lower():
            return any(kw in title for kw in keywords)
    logger.warning(f"Genre '{genre}' not found in GENRE_KEYWORDS, allowing track")
    return True

def is_safe_track(track: dict) -> bool:
    """Проверка безопасности трека (например, отсутствие откровенного контента)."""
    title = track.get('title', '').lower()
    description = track.get('description', '').lower()
    unsafe_keywords = ['explicit', '18+', 'nsfw', 'offensive']
    return not any(kw in title or kw in description for kw in unsafe_keywords)

def build_search_queries(genre: str):
    return [f"{genre} music", f"{genre} best tracks", f"{genre} playlist"]

def escape_markdown(text: str) -> str:
    return re.sub(r'([_*[\\()~`>#+\-=|}{}.!])', r'\\\1', text)

# --- Config & FS Management ---
async def notify_admins(application: Application, message: str):
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(admin_id, message)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

def load_config() -> RadioConfig:
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        with config_path.open('r', encoding='utf-8') as f:
            try:
                return RadioConfig(**json.load(f))
            except Exception as e:
                logger.error(f"Error loading config: {e}")
                backup_path = config_path.with_suffix(f'.bak.{int(time.time())}')
                config_path.rename(backup_path)
                logger.info(f"Created backup of corrupted config at {backup_path}")
                asyncio.create_task(notify_admins(
                    application,
                    f"Ошибка загрузки конфигурации, создана резервная копия: {backup_path}"
                ))
    return RadioConfig()

def save_config(config: RadioConfig):
    config_path = Path(CONFIG_FILE)
    temp_path = config_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(config.dict(), f, indent=4, ensure_ascii=False)
        temp_path.replace(config_path)
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        if temp_path.exists():
            temp_path.unlink()
        raise

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

# --- Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵\nИспользуй /play для поиска или /ron для радио.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """*Команды бота*

/play <название> - Поиск трека
/id - ID чата

*Админ-команды:*
/ron <жанр> - Включить радио
/rof - Выключить радио
/votestart - Запустить голосование
/status - Показать панель управления
/skip - Пропустить трек
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID этого чата: `{update.message.chat_id}`", parse_mode='Markdown')

async def get_paginated_keyboard(search_id: str, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    page_size = 5
    results = context.bot_data.get('paginated_searches', {}).get(search_id, [])
    if not results:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Ошибка: поиск не найден.", callback_data="noop")]])

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
        await update.message.reply_text("Укажите название песни: `/play <название>`", parse_mode='Markdown')
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'Ищу "{query}"...')
    
    ydl_opts = {'format': 'bestaudio', 'noplaylist': True, 'quiet': True, 'default_search': 'scsearch30', 'extract_flat': 'in_playlist'}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info.get('entries'):
            await message.edit_text("Треки не найдены.")
            return

        search_id = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('paginated_searches', {})[search_id] = info['entries']
        reply_markup = await get_paginated_keyboard(search_id, context)
        await message.edit_text(f'Найдено: {len(info["entries"])}. Выберите трек:', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in /play: {e}")
        await message.edit_text("Ошибка поиска. Попробуйте позже.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    command, data = query.data.split(":", 1)

    if command == "play_track":
        track_url = context.bot_data.get('track_urls', {}).get(data)
        if not track_url:
            await query.edit_message_text("Ошибка: трек устарел.")
            return
        await query.edit_message_text("Обрабатываю...")
        try:
            track_info = await download_track(track_url)
            if track_info:
                await send_track(track_info, query.message.chat_id, context.bot)
                await query.edit_message_text("Трек отправлен!")
        except Exception as e:
            await query.edit_message_text(f"Ошибка: {e}")
        finally:
            if track_info and os.path.exists(track_info['filepath']):
                os.remove(track_info['filepath'])
    elif command == "page":
        search_id, page_num_str = data.split(":")
        page = int(page_num_str)
        reply_markup = await get_paginated_keyboard(search_id, context, page)
        await query.edit_message_text('Выберите трек:', reply_markup=reply_markup)
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

# --- Admin Commands ---
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update, context):
            if isinstance(update, Update) and update.callback_query:
                await update.callback_query.answer("Эта кнопка только для администраторов.", show_alert=True)
            else:
                await update.message.reply_text("Только администраторы могут использовать эту команду.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

@admin_only
async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    genre = " ".join(context.args) if context.args else load_config().genre
    config = load_config()
    config.is_on = True
    config.genre = genre
    save_config(config)
    
    if 'radio_task' not in context.bot_data or context.bot_data['radio_task'].done():
        context.bot_data['radio_task'] = asyncio.create_task(radio_loop(context.application))
    if 'voting_task' not in context.bot_data or context.bot_data['voting_task'].done():
        context.bot_data['voting_task'] = asyncio.create_task(hourly_voting_loop(context.application))
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(f"Радио включено. Жанр: {escape_markdown(genre)}.", parse_mode='MarkdownV2')
    await send_status_panel(context.application, update.effective_chat.id, config.status_message_id)

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
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("Радио выключено.")
    await send_status_panel(context.application, update.effective_chat.id, config.status_message_id)

@admin_only
async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not config.is_on:
        await update.message.reply_text("Радио выключено.")
        return
    if config.active_poll:
        await update.message.reply_text("Голосование уже идет.")
        return
    
    if await _create_and_send_poll(context.application):
        await update.message.reply_text("Голосование запущено.")
    else:
        await update.message.reply_text("Ошибка запуска голосования.")

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_status_panel(context.application, update.effective_chat.id)

@admin_only
async def skip_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'radio_task' in context.bot_data and not context.bot_data['radio_task'].done():
        context.bot_data['radio_task'].cancel()
        context.bot_data['radio_task'] = asyncio.create_task(radio_loop(context.application))
    
    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.answer("Пропускаем трек...")
    elif isinstance(update, Update) and update.message:
        await update.message.reply_text("Пропускаем трек...")

rate_limiter = AsyncLimiter(20, 1)

async def send_status_panel(application: Application, chat_id: int, message_id: int = None):
    async with rate_limiter:
        try:
            config = load_config()
            now_playing = config.now_playing
            is_on = config.is_on

            status_icon = "🟢" if is_on else "🔴"
            status_text = "В ЭФИРЕ" if is_on else "ВЫКЛЮЧЕНО"
            genre = escape_markdown(config.genre)
            
            text = f"""*Интерактивная Панель Управления*
────────────────────────
*Статус:* {status_icon} *{escape_markdown(status_text)}*
*Жанр:* `{genre}`
"""

            if is_on and now_playing:
                title = escape_markdown(now_playing.get('title', 'Неизвестный трек'))
                duration = escape_markdown(format_duration(now_playing.get('duration', 0)))
                text += f"""\
────────────────────────
*Сейчас играет:*
`{title}`
`{duration}`
────────────────────────
"""
            else:
                text += """\
────────────────────────
*Сейчас играет:* — тишина...
────────────────────────
"""

            keyboard = []
            if is_on:
                keyboard.append([
                    InlineKeyboardButton("⏭ Пропустить", callback_data="skip_track:0"),
                    InlineKeyboardButton("⏹️ Стоп", callback_data="toggle_radio:0")
                ])
                keyboard.append([InlineKeyboardButton("🗳️ Голосование", callback_data="start_vote:0")])
            else:
                keyboard.append([InlineKeyboardButton("▶️ Запустить Радио", callback_data="toggle_radio:0")])

            keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="status_refresh:0")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            if message_id:
                await application.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')
            else:
                sent_message = await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')
                config.status_message_id = sent_message.message_id
                save_config(config)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await send_status_panel(application, chat_id, message_id)
        except TelegramError as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"Error sending status panel: {e}")

# --- Music & Radio Logic ---
async def download_track(url: str, max_retries: int = Constants.MAX_RETRIES) -> Optional[dict]:
    ensure_download_dir()
    out_template = os.path.join(DOWNLOAD_DIR, f'{uuid.uuid4()}.%(ext)s')
    try:
        for attempt in range(max_retries):
            try:
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
                    'outtmpl': out_template,
                    'noplaylist': True,
                    'quiet': True,
                    'socket_timeout': 30,
                    'retries': 3
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                    filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
                    if not os.path.exists(filename):
                        raise FileNotFoundError(f"Downloaded file not found: {filename}")
                    return {
                        'filepath': filename,
                        'title': info.get('title', 'Unknown'),
                        'duration': info.get('duration', 0),
                        'url': url
                    }
            except (DownloadError, FileNotFoundError) as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to download {url} after {max_retries} attempts: {e}")
                    raise
                await asyncio.sleep(2 ** attempt)
    except Exception:
        return None

async def send_track(track_info: dict, chat_id: int, bot):
    try:
        with open(track_info['filepath'], 'rb') as audio_file:
            return await bot.send_audio(chat_id=chat_id, audio=audio_file, title=track_info['title'], duration=track_info['duration'])
    except Exception as e:
        logger.error(f"Failed to send track {track_info.get('filepath')}: {e}")
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
    logger.info("Refilling radio playlist...")
    config = load_config()
    raw_genre = config.genre
    search_queries = build_search_queries(raw_genre)
    played = set(bot_data.get('played_radio_urls', []))
    suitable_tracks = []

    for query in search_queries:
        logger.info(f"Searching: {query}")
        cache_key = f"{query}:{raw_genre}"
        if cache_key in search_cache:
            info = search_cache[cache_key]
        else:
            ydl_opts = {
                'format': 'bestaudio',
                'noplaylist': True,
                'quiet': True,
                'default_search': 'scsearch50',
                'extract_flat': 'in_playlist'
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                search_cache[cache_key] = info
            except Exception as e:
                logger.error(f"Search error for '{query}': {e}")
                info = None
        
        if not info or not info.get('entries'):
            continue

        for t in info['entries']:
            if not t:
                continue
            if not (60 < t.get('duration', 0) < 900):
                continue
            if t.get('url') in played:
                continue
            if has_ukrainian_chars(t.get('title', '')):
                continue
            if not is_genre_match(t, raw_genre):
                continue
            if not is_safe_track(t):
                continue
            suitable_tracks.append(t)

    unique_tracks = {t['url']: t for t in suitable_tracks}
    suitable_tracks = list(unique_tracks.values())

    suitable_tracks.sort(
        key=lambda tr: (tr.get('play_count', 0) or 0) + (tr.get('like_count', 0) or 0),
        reverse=True
    )

    final_urls = [t['url'] for t in suitable_tracks[:50]]
    random.shuffle(final_urls)

    bot_data['radio_playlist'] = deque(final_urls)
    config.radio_playlist = list(bot_data['radio_playlist'])
    save_config(config)

    logger.info(f"Playlist refilled with {len(final_urls)} tracks.")
    if not final_urls:
        await application.bot.send_message(
            RADIO_CHAT_ID,
            "Не удалось найти треки для плейлиста. Пробуем снова через минуту...",
            parse_mode='MarkdownV2'
        )

async def radio_loop(application: Application):
    bot_data = application.bot_data
    while True:
        config = load_config()
        if not config.is_on:
            await asyncio.sleep(30)
            continue
        await asyncio.sleep(5)
        
        if not bot_data.get('radio_playlist'):
            await refill_playlist(application)
            if not bot_data.get('radio_playlist'):
                await asyncio.sleep(60)
                continue
        
        track_url = bot_data['radio_playlist'].popleft()
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
                else:
                    await application.bot.send_message(
                        RADIO_CHAT_ID,
                        "Ошибка отправки трека, пробуем следующий...",
                        parse_mode='MarkdownV2'
                    )
        except Exception as e:
            logger.error(f"Radio loop track error: {e}")
            await application.bot.send_message(
                RADIO_CHAT_ID,
                "Ошибка воспроизведения трека, пробуем следующий...",
                parse_mode='MarkdownV2'
            )
        finally:
            if track_info and os.path.exists(track_info['filepath']):
                os.remove(track_info['filepath'])
        
        await asyncio.sleep(config.track_interval_seconds)

# --- Voting Logic ---
async def hourly_voting_loop(application: Application):
    while True:
        try:
            config = load_config()
            await asyncio.sleep(config.voting_interval_seconds)
            if config.is_on and not config.active_poll:
                await _create_and_send_poll(application)
        except asyncio.CancelledError:
            logger.info("Voting loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in hourly_voting_loop: {e}")
            await asyncio.sleep(60)

async def _create_and_send_poll(application: Application) -> bool:
    config = load_config()
    try:
        votable_genres = config.votable_genres
        if len(votable_genres) < 10:
            logger.error("Not enough votable genres.")
            return False
        decades = ["70-х", "80-х", "90-х", "2000-х", "2010-х"]
        special = {f"{random.choice(votable_genres)} {random.choice(decades)}" for _ in range(5)}
        regular_pool = [g for g in votable_genres if g not in {s.split(' ')[0] for s in special} and g.lower() != 'pop']
        num_to_sample = min(4, len(regular_pool))
        regular = set(random.sample(regular_pool, k=num_to_sample))
        options = list(special | regular)
        while len(options) < 9:
            chosen = random.choice(votable_genres)
            if chosen not in options:
                options.append(chosen)
        options.append("Pop")
        random.shuffle(options)
        
        poll_duration = config.poll_duration_seconds
        message = await application.bot.send_poll(
            RADIO_CHAT_ID,
            "Выбираем жанр на следующий час!",
            options[:10],
            is_anonymous=False,
            open_period=poll_duration
        )
        
        poll_data = message.poll.to_dict()
        poll_data['close_timestamp'] = datetime.now().timestamp() + poll_duration
        config.active_poll = poll_data
        save_config(config)

        logger.info(f"Poll {message.poll.id} sent, processing in {poll_duration}s.")
        asyncio.create_task(schedule_poll_processing(application, message.poll.id, poll_duration))
        return True
    except Exception as e:
        logger.error(f"Create poll error: {e}")
        return False

async def schedule_poll_processing(application: Application, poll_id: str, delay: int):
    await asyncio.sleep(delay + 2)
    logger.info(f"Processing poll {poll_id}...")
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
            logger.info(f"Updated state for poll {update.poll.id}.")

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
    logger.info(f"Poll winner: '{final_winner}'.")
    config.genre = final_winner
    config.radio_playlist = []
    if isinstance(application.bot_data.get('radio_playlist'), deque):
        application.bot_data['radio_playlist'].clear()
    else:
        application.bot_data['radio_playlist'] = deque()

    config.now_playing = None
    save_config(config)

    await application.bot.send_message(
        RADIO_CHAT_ID,
        f"Голосование завершено! Играет: **{escape_markdown(final_winner)}**",
        parse_mode='MarkdownV2'
    )

    asyncio.create_task(refill_playlist(application))

    if 'radio_task' not in application.bot_data or application.bot_data['radio_task'].done():
        application.bot_data['radio_task'] = asyncio.create_task(radio_loop(application))

# --- Application Setup ---
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
            if free < Constants.MIN_DISK_SPACE:
                logger.warning("Low disk space, cleaning up immediately...")
                for file in Path(DOWNLOAD_DIR).glob('*.mp3'):
                    file.unlink()
            for file in Path(DOWNLOAD_DIR).glob('*.mp3'):
                if file.stat().st_mtime < time.time() - 3600:
                    file.unlink()
        except Exception as e:
            logger.error(f"Error cleaning up download directory: {e}")
        await asyncio.sleep(3600)

async def cleanup_cache():
    while True:
        search_cache.clear()
        await asyncio.sleep(24 * 3600)

async def post_init(application: Application) -> None:
    global application
    config = load_config()
    bot_data = application.bot_data
    bot_data['radio_playlist'] = deque(config.radio_playlist)
    bot_data['played_radio_urls'] = config.played_radio_urls
    bot_data['radio_message_ids'] = deque(config.radio_message_ids)
    
    if config.is_on:
        logger.info("Radio was ON at startup. Starting background tasks.")
        bot_data['radio_task'] = asyncio.create_task(radio_loop(application))
        bot_data['voting_task'] = asyncio.create_task(hourly_voting_loop(application))
    
    active_poll = config.active_poll
    if active_poll:
        close_timestamp = active_poll.get('close_timestamp')
        if close_timestamp:
            remaining_time = close_timestamp - datetime.now().timestamp()
            if remaining_time > 0:
                logger.info(f"[Init] Found an active poll. Rescheduling processing in {remaining_time:.0f}s.")
                asyncio.create_task(schedule_poll_processing(application, active_poll['id'], remaining_time))
    
    asyncio.create_task(cleanup_download_dir())
    asyncio.create_task(cleanup_cache())

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

def main() -> None:
    if not BOT_TOKEN:
        logger.error("FATAL: BOT_TOKEN not found.")
        return
    try:
        global application
        application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(shutdown(application)))
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