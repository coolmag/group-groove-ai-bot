import logging
import os
import asyncio
import json
import random
import re
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
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
from pydantic import BaseModel, Field
from cachetools import TTLCache
from asyncio import Lock
from functools import wraps

# --- Constants ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 10 # Reduced for faster testing
    POLL_DURATION_SECONDS = 60
    MAX_RETRIES = 3
    MIN_DISK_SPACE = 1_000_000_000  # 1GB
    MAX_FILE_SIZE = 50_000_000      # 50MB
    MAX_DURATION = 900              # 15 minutes
    MIN_DURATION = 30               # 30 seconds
    PLAYED_URLS_MEMORY = 200

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
            with CONFIG_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
                # Pydantic will automatically convert lists to deques where specified
                return State(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Error decoding config file, creating a new one. Error: {e}")
            backup_path = CONFIG_FILE.with_suffix(f'.bak.{int(datetime.now().timestamp())}')
            CONFIG_FILE.rename(backup_path)
    return State()

async def save_state(context: ContextTypes.DEFAULT_TYPE):
    async with state_lock:
        state = context.bot_data.get('state')
        if state:
            temp_path = CONFIG_FILE.with_suffix('.tmp')
            try:
                with temp_path.open('w', encoding='utf-8') as f:
                    # Pydantic's model_dump handles deque serialization
                    json.dump(state.model_dump(), f, indent=4)
                temp_path.replace(CONFIG_FILE)
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

# --- Helper Functions ---
def format_duration(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}" if seconds > 0 else "--:--"

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
        'format': 'bestaudio', 'noplaylist': True, 'quiet': True,
        'default_search': 'ytsearch50', 'extract_flat': 'in_playlist',
        'match_filter': lambda i: Constants.MIN_DURATION < i.get('duration', 0) <= Constants.MAX_DURATION,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, f"{state.genre} music", download=False)
        
        entries = info.get('entries', [])
        unplayed = [e['url'] for e in entries if e and e.get('url') not in state.played_radio_urls]
        
        if unplayed:
            random.shuffle(unplayed)
            state.radio_playlist.extend(unplayed)
            logger.info(f"Added {len(unplayed)} new tracks to the playlist.")
        else:
            logger.warning(f"No new tracks found for genre '{state.genre}'. Playlist may be empty.")
    except Exception as e:
        logger.error(f"Failed to refill playlist: {e}")

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str):
    state: State = context.bot_data['state']
    state.now_playing = None
    await update_status_panel(context)

    ydl_opts = {
        'format': 'bestaudio/best', 'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True, 'quiet': True, 'noprogress': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        
        filepath = DOWNLOAD_DIR / f"{info['id']}.mp3"
        if not filepath.exists() or filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            logger.error(f"File error for {url}")
            if filepath.exists(): filepath.unlink()
            return

        state.now_playing = NowPlaying(title=info.get('title', 'Unknown'), duration=info.get('duration', 0), url=url)
        
        with open(filepath, 'rb') as audio_file:
            msg = await context.bot.send_audio(
                RADIO_CHAT_ID, audio_file, title=state.now_playing.title, duration=state.now_playing.duration
            )
        filepath.unlink()
        await update_status_panel(context)

    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}")
        state.now_playing = None

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Radio loop started.")
    while True:
        try:
            state: State = context.bot_data['state']
            if not state.is_on:
                logger.info("Radio is off. Pausing loop.")
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
    text = f"Статус: {status_icon} ({'В ЭФИРЕ' if state.is_on else 'ВЫКЛЮЧЕНО'})\n"
    text += f"Жанр: {state.genre}\n"
    if state.is_on and state.now_playing:
        text += f"Сейчас играет: {state.now_playing.title} ({format_duration(state.now_playing.duration)})"
    elif state.is_on:
        text += "Сейчас играет: ...загрузка..."

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="status:refresh")],
        [InlineKeyboardButton("⏭ Пропустить", callback_data="radio:skip")] if state.is_on else [],
        [InlineKeyboardButton("🗳️ Голосование", callback_data="vote:start")] if state.is_on else [],
        [InlineKeyboardButton("▶️ Запустить", callback_data="radio:on")] if not state.is_on else [InlineKeyboardButton("⏹️ Стоп", callback_data="radio:off")]
    ]
    reply_markup = InlineKeyboardMarkup([row for row in keyboard if row])

    try:
        if state.status_message_id:
            await context.bot.edit_message_text(chat_id=RADIO_CHAT_ID, message_id=state.status_message_id, text=text, reply_markup=reply_markup)
        else:
            msg = await context.bot.send_message(RADIO_CHAT_ID, text, reply_markup=reply_markup)
            state.status_message_id = msg.message_id
    except Exception as e:
        logger.warning(f"Could not update status panel (message {state.status_message_id} may be deleted). Sending new one. Error: {e}")
        msg = await context.bot.send_message(RADIO_CHAT_ID, text, reply_markup=reply_markup)
        state.status_message_id = msg.message_id

@admin_only
async def radio_on_off(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    if state.is_on == turn_on: return

    state.is_on = turn_on
    if turn_on:
        if not context.bot_data.get('radio_loop_task') or context.bot_data['radio_loop_task'].done():
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        await update.effective_message.reply_text(f"Радио включено. Жанр: {state.genre}")
    else:
        if context.bot_data.get('radio_loop_task') and not context.bot_data['radio_loop_task'].done():
            context.bot_data['radio_loop_task'].cancel()
        state.now_playing = None
        await update.effective_message.reply_text("Радио выключено.")
    await update_status_panel(context)

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not state.is_on: return
    
    # Cancel the current loop, a new one will be started by the main logic if needed
    if context.bot_data.get('radio_loop_task') and not context.bot_data['radio_loop_task'].done():
        context.bot_data['radio_loop_task'].cancel()
    context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    await update.effective_message.reply_text("Пропускаем трек...")

@admin_only
async def create_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.active_poll_id:
        await update.effective_message.reply_text("Голосование уже идет.")
        return

    options = random.sample(state.votable_genres, k=min(10, len(state.votable_genres)))
    msg = await context.bot.send_poll(
        RADIO_CHAT_ID, "Выбери жанр на следующий час!", options,
        is_anonymous=False, open_period=Constants.POLL_DURATION_SECONDS
    )
    state.active_poll_id = msg.poll.id
    await update.effective_message.reply_text("Голосование запущено!")

async def process_poll_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not update.poll.is_closed or state.active_poll_id != update.poll.id:
        return

    state.active_poll_id = None
    winning_option = max(update.poll.options, key=lambda o: o.voter_count, default=None)
    
    if winning_option and winning_option.voter_count > 0:
        state.genre = winning_option.text
        state.radio_playlist.clear()
        state.now_playing = None
        await context.bot.send_message(RADIO_CHAT_ID, f"Голосование завершено! Новый жанр: {state.genre}")
        # The radio loop will automatically pick up the new genre on the next iteration
    else:
        await context.bot.send_message(RADIO_CHAT_ID, "В голосовании никто не участвовал.")
    await update_status_panel(context)

# --- Entry Point Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵")
    await status_command(update, context)

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    command, *data = update.callback_query.data.split(":")

    actions = {
        "radio": {"on": lambda: radio_on_off(update, context, True), "off": lambda: radio_on_off(update, context, False), "skip": lambda: skip_command(update, context)},
        "vote": {"start": lambda: create_poll_command(update, context)},
        "status": {"refresh": lambda: update_status_panel(context)}
    }
    if command in actions and data[0] in actions[command]:
        await actions[command][data[0]]()

# --- Bot Lifecycle ---
async def post_init(application: Application):
    application.bot_data['state'] = load_state()
    if application.bot_data['state'].is_on:
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
    logger.info("Bot started successfully.")

async def on_shutdown(application: Application):
    logger.info("Shutting down... saving state.")
    if application.bot_data.get('radio_loop_task'):
        application.bot_data['radio_loop_task'].cancel()
    await save_state(application)

def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if not BOT_TOKEN: return logger.critical("FATAL: BOT_TOKEN not set.")
    if not shutil.which("ffmpeg"): return logger.critical("FATAL: ffmpeg not found.")

    application = (
        Application.builder().token(BOT_TOKEN)
        .post_init(post_init).post_shutdown(on_shutdown).build()
    )
    
    application.add_handlers([
        CommandHandler("start", start_command),
        CommandHandler("status", status_command),
        CommandHandler("skip", skip_command),
        CommandHandler("votestart", create_poll_command),
        CommandHandler("ron", lambda u, c: radio_on_off(u, c, True)),
        CommandHandler("rof", lambda u, c: radio_on_off(u, c, False)),
        CallbackQueryHandler(button_callback),
        PollHandler(process_poll_results)
    ])
    
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
