import logging
import os
import asyncio
import json
import random
import shutil
import uuid
from pathlib import Path
from typing import List, Optional
from collections import deque
from datetime import datetime
import yt_dlp
import ffmpeg

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from telegram.error import TelegramError
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from functools import wraps
from asyncio import Lock

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 10
    POLL_DURATION_SECONDS = 60
    MAX_RETRIES = 3
    MIN_DISK_SPACE = 1_000_000_000  # 1GB
    MAX_FILE_SIZE = 50_000_000      # 50MB
    MAX_DURATION = 900              # 15 minutes
    MIN_DURATION = 30               # 30 seconds
    PLAYED_URLS_MEMORY = 200
    DOWNLOAD_TIMEOUT = 120          # 2 minutes

# --- Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")

# --- Pydantic Models for State Management ---
class NowPlaying(BaseModel):
    title: str
    duration: int
    url: str

class State(BaseModel):
    is_on: bool = False
    genre: str = "lo-fi hip hop"
    radio_playlist: deque[str] = Field(default_factory=deque)
    played_radio_urls: deque[str] = Field(default_factory=deque)
    active_poll_id: Optional[str] = None
    status_message_id: Optional[int] = None
    now_playing: Optional[NowPlaying] = None
    votable_genres: List[str] = Field(default_factory=lambda: ["pop", "rock", "hip hop", "electronic", "classical", "jazz", "blues", "country", "metal", "reggae", "folk", "indie", "rap", "r&b", "soul", "funk", "disco", "punk rock", "alternative rock", "post-punk", "ambient", "drum and bass", "techno", "trance", "house", "dubstep", "grime", "trip-hop", "acid jazz", "swing", "bluegrass", "blues rock", "folk rock", "post-rock", "shoegaze", "garage rock", "britpop", "k-pop", "j-pop", "latin", "bossa nova", "samba", "reggaeton", "rockabilly"])

    class Config:
        arbitrary_types_allowed = True

# --- Globals ---
state_lock = Lock()

# --- State & Config Management ---
def load_state() -> State:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            return State(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Config file error, creating new one. Error: {e}")
            CONFIG_FILE.with_suffix(f'.bak.{int(datetime.now().timestamp())}').write_text(CONFIG_FILE.read_text())
    return State()

async def save_state(context: ContextTypes.DEFAULT_TYPE):
    async with state_lock:
        if state := context.bot_data.get('state'):
            try:
                temp_path = CONFIG_FILE.with_suffix('.tmp')
                temp_path.write_text(state.model_dump_json(indent=4), encoding='utf-8')
                temp_path.replace(CONFIG_FILE)
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

# --- Helper Functions ---
def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    s_int = int(seconds)
    return f"{s_int // 60:02d}:{s_int % 60:02d}"

# --- Admin Check ---
async def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update):
            await update.effective_message.reply_text("Эта команда только для администраторов.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Core Radio Logic ---
async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist for genre: {state.genre}")
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'noplaylist': True, 'quiet': True,
        'default_search': 'ytsearch50', 'extract_flat': 'in_playlist',
        'match_filter': lambda i: Constants.MIN_DURATION < i.get('duration', 0) <= Constants.MAX_DURATION,
        'force-ipv4': True, 'no-cache-dir': True,
        'sleep_interval': 1,
        'max_sleep_interval': 3
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, f"{state.genre} music", download=False)
        entries = info.get('entries', [])
        unplayed = [e['url'] for e in entries if e and e.get('url') not in state.played_radio_urls]
        if unplayed:
            random.shuffle(unplayed)
            state.radio_playlist.extend(unplayed)
            logger.info(f"Added {len(unplayed)} new tracks.")
    except Exception as e:
        logger.error(f"Playlist refill failed: {e}")

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str):
    state: State = context.bot_data['state']
    state.now_playing = None
    await update_status_panel(context)

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True, 'quiet': True, 'noprogress': True,
        'force-ipv4': True, 'no-cache-dir': True,
        'sleep_interval': 1,
        'max_sleep_interval': 3
    }
    try:
        logger.info(f"Downloading {url}")
        download_task = asyncio.to_thread(yt_dlp.YoutubeDL(ydl_opts).extract_info, url, download=True)
        info = await asyncio.wait_for(download_task, timeout=Constants.DOWNLOAD_TIMEOUT)
        
        if not info.get('requested_downloads'):
            raise ValueError("yt-dlp did not report a downloaded file.")
        filepath = Path(info['requested_downloads'][0]['filepath'])

        if not filepath.exists() or filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            raise ValueError(f"File error for {url}: {filepath} not found or too large")

        try:
            state.now_playing = NowPlaying(title=info.get('title', 'Unknown'), duration=info.get('duration', 0), url=url)
            with open(filepath, 'rb') as audio_file:
                await context.bot.send_audio(RADIO_CHAT_ID, audio_file, title=state.now_playing.title, duration=state.now_playing.duration)
        finally:
            if filepath.exists():
                filepath.unlink()

    except asyncio.TimeoutError:
        logger.error(f"Download timed out for {url}")
        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Загрузка трека заняла слишком много времени и была прервана.")
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"⚠️ Не удалось скачать или отправить трек.")
    finally:
        state.now_playing = None
        await update_status_panel(context)

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Radio loop started.")
    while True:
        try:
            state: State = context.bot_data['state']
            if not state.is_on:
                await asyncio.sleep(15)
                continue

            if not state.radio_playlist:
                await refill_playlist(context)
                if not state.radio_playlist:
                    await asyncio.sleep(30)
                    continue
            
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()

            await download_and_send_track(context, url)
            await asyncio.sleep(Constants.TRACK_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Radio loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Critical error in radio_loop: {e}", exc_info=True)
            await asyncio.sleep(15)

# --- UI and Commands ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    status_icon = "🟢" if state.is_on else "🔴"
    
    lines = []
    lines.append(f"Статус: {status_icon} {('В ЭФИРЕ' if state.is_on else 'ВЫКЛЮЧЕНО')}")
    lines.append(f"Жанр: {state.genre}")

    if state.is_on and state.now_playing:
        now_playing_text = f"Сейчас играет: {state.now_playing.title} ({format_duration(state.now_playing.duration)})"
        lines.append(now_playing_text))
        lines.append(now_playing_text)
    elif state.is_on:
        lines.append("Сейчас играет: ...загрузка...")
    
    text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="status:refresh")],
        [InlineKeyboardButton("⏭ Пропустить", callback_data="radio:skip")] if state.is_on else [],
        [InlineKeyboardButton("🗳️ Голосование", callback_data="vote:start")] if state.is_on else [],
        [InlineKeyboardButton("▶️ Запустить", callback_data="radio:on")] if not state.is_on else [InlineKeyboardButton("⏹️ Стоп", callback_data="radio:off")]
    ]
    reply_markup = InlineKeyboardMarkup([row for row in keyboard if row])

    try:
        if state.status_message_id:
            logger.debug(f"Editing status message {state.status_message_id}")
            await context.bot.edit_message_text(chat_id=RADIO_CHAT_ID, message_id=state.status_message_id, text=text, reply_markup=reply_markup)
        else:
            logger.debug("Sending new status message")
            msg = await context.bot.send_message(RADIO_CHAT_ID, text, reply_markup=reply_markup)
            state.status_message_id = msg.message_id
    except TelegramError as e:
        if "not modified" in str(e).lower():
            pass # Ignore this specific error, it's not a real problem
        else:
            logger.warning(f"Could not update status panel (message {state.status_message_id} may be deleted). Sending new one. Error: {e}")
            try:
                msg = await context.bot.send_message(RADIO_CHAT_ID, text, reply_markup=reply_markup)
                state.status_message_id = msg.message_id
            except Exception as e2:
                logger.error(f"Failed to send new status panel after edit failed: {e2}")

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    if state.is_on == turn_on: return

    state.is_on = turn_on
    task = context.bot_data.get('radio_loop_task')

    if turn_on:
        if not task or task.done():
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        await update.effective_message.reply_text(f"Радио включено. Жанр: {state.genre}")
    else:
        if task and not task.done():
            task.cancel()
        state.now_playing = None
        await update.effective_message.reply_text("Радио выключено.")
    await update_status_panel(context)

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not state.is_on: return
    
    task = context.bot_data.get('radio_loop_task')
    if task and not task.done():
        task.cancel()
    context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    await update.effective_message.reply_text("Пропускаем трек...")

@admin_only
async def create_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.active_poll_id:
        return await update.effective_message.reply_text("Голосование уже идет.")

    options = random.sample(state.votable_genres, k=min(10, len(state.votable_genres)))
    msg = await context.bot.send_poll(
        RADIO_CHAT_ID, "Выбери жанр на следующий час!", options,
        is_anonymous=False, open_period=Constants.POLL_DURATION_SECONDS
    )
    state.active_poll_id = msg.poll.id
    context.job_queue.run_once(force_process_poll, Constants.POLL_DURATION_SECONDS + 5, data={'poll_id': msg.poll.id}, name=f"poll_{msg.poll.id}")
    await update.effective_message.reply_text("Голосование запущено!")

async def force_process_poll(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    poll_id = job_data['poll_id']
    state: State = context.bot_data['state']
    if state.active_poll_id == poll_id:
        logger.warning(f"Poll {poll_id} was not closed by handler, forcing processing.")
        state.active_poll_id = None
        await context.bot.send_message(RADIO_CHAT_ID, "Голосование принудительно завершено из-за таймаута.")
        await update_status_panel(context)

async def poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not state.active_poll_id or state.active_poll_id != update.poll.id:
        return

    if update.poll.is_closed:
        logger.info(f"Processing closed poll: {update.poll.id}")
        state.active_poll_id = None
        winning_option = max(update.poll.options, key=lambda o: o.voter_count, default=None)
        
        if winning_option and winning_option.voter_count > 0:
            state.genre = winning_option.text
            state.radio_playlist.clear()
            state.now_playing = None
            await context.bot.send_message(RADIO_CHAT_ID, f"Голосование завершено! Новый жанр: {state.genre}")
        else:
            await context.bot.send_message(RADIO_CHAT_ID, "В голосовании никто не участвовал.")
        await update_status_panel(context)

# --- Search Command Handlers ---
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
        'match_filter': lambda info: Constants.MIN_DURATION < info.get('duration', 0) <= Constants.MAX_DURATION,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
        if not info.get('entries'):
            return await message.edit_text("Треки не найдены.")
        
        search_id = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('paginated_searches', {})[search_id] = [
            {'url': t['url'], 'title': t['title'], 'duration': t['duration']} for t in info['entries']
        ]
        reply_markup = await get_paginated_keyboard(search_id, context)
        await message.edit_text(f'Найдено: {len(info["entries"])}. Выбери трек:', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Search error for query '{query}': {e}")
        await message.edit_text(f"Ошибка поиска: {e}")

# --- Entry Point Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵")
    await status_command(update, context)

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    query = update.callback_query
    command, *data = query.data.split(":")
    action_key = data[0] if data else ''

    # Search result pagination
    if command == "page":
        search_id, page_num_str = data
        page = int(page_num_str)
        reply_markup = await get_paginated_keyboard(search_id, context, page)
        return await query.edit_message_text('Выбери трек:', reply_markup=reply_markup)
    
    # Individual track playback
    if command == "play_track":
        track_url = context.bot_data.get('track_urls', {}).get(action_key)
        if not track_url:
            return await query.edit_message_text("Трек устарел.")
        await query.edit_message_text("Обрабатываю...")
        await download_and_send_track(context, track_url)
        return await query.edit_message_text("Трек отправлен!")

    # Admin panel actions
    actions = {
        "radio": {"on": lambda: radio_on_off_command(update, context, True), "off": lambda: radio_on_off_command(update, context, False), "skip": lambda: skip_command(update, context)},
        "vote": {"start": lambda: create_poll_command(update, context)},
        "status": {"refresh": lambda: update_status_panel(context)}
    }
    if command in actions and action_key in actions[command]:
        await actions[command][action_key]()

# --- Bot Lifecycle ---
async def post_init(application: Application):
    application.bot_data['state'] = load_state()
    if application.bot_data['state'].is_on:
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
    logger.info("Bot started successfully.")

async def on_shutdown(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Shutting down... saving state.")
    if task := context.application.bot_data.get('radio_loop_task'):
        task.cancel()
    await save_state(context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not BOT_TOKEN: return logger.critical("FATAL: BOT_TOKEN not set.")
    if not shutil.which("ffmpeg"): return logger.critical("FATAL: ffmpeg not found.")

    application = (
        Application.builder().token(BOT_TOKEN)
        .post_init(post_init).post_shutdown(on_shutdown).build()
    )
    
    application.add_error_handler(error_handler)

    handlers = [
        CommandHandler("start", start_command),
        CommandHandler(["status", "st"], status_command),
        CommandHandler(["skip", "s"], skip_command),
        CommandHandler(["votestart", "v"], create_poll_command),
        CommandHandler(["play", "p"], play_command),
        CommandHandler(["ron", "r_on"], lambda u, c: radio_on_off_command(u, c, True)),
        CommandHandler(["rof", "r_off"], lambda u, c: radio_on_off_command(u, c, False)),
        CallbackQueryHandler(button_callback),
        PollHandler(poll_handler)
    ]
    application.add_handlers(handlers)
    
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    main()