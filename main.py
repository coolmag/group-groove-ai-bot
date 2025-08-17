# main.py
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
import aiohttp

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
    POLL_DURATION_SECONDS = 60
    MAX_FILE_SIZE = 50_000_000
    MAX_DURATION = 900
    MIN_DURATION = 30
    PLAYED_URLS_MEMORY = 200
    DOWNLOAD_TIMEOUT = 120
    DEFAULT_SOURCE = "soundcloud"  # soundcloud | youtube

# --- Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")

# --- Models ---
class NowPlaying(BaseModel):
    """Represents the currently playing track."""
    title: str
    duration: int
    url: str

class State(BaseModel):
    """Represents the bot's state."""
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

# --- Admin ---
async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not await is_admin(user_id):
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
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
    return [{"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", [])]

async def get_tracks_youtube(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"ytsearch10:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
    return [{"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", [])]

# --- Playlist refill ---
async def refill_playlist(context):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    try:
        if state.source == "soundcloud":
            tracks = await get_tracks_soundcloud(state.genre)
        else:
            tracks = await get_tracks_youtube(state.genre)

        urls = [t["url"] for t in tracks if t["url"] not in state.played_radio_urls]
        if urls:
            random.shuffle(urls)
            state.radio_playlist.extend(urls)
            await save_state_from_botdata(context.bot_data)
            logger.info(f"Added {len(urls)} new tracks.")
    except Exception as e:
        logger.error(f"Playlist refill failed: {e}")

# --- Download & send ---
async def download_and_send_to_chat(context, url: str, chat_id: int):
    state: State = context.bot_data['state']
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info))
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                chat_id, f,
                title=info.get("title", "Unknown"),
                duration=int(info.get("duration", 0))
            )
        filepath.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, "⚠️ Не удалось обработать трек.")

async def download_and_send_track(context, url: str):
    state: State = context.bot_data['state']
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        filepath = Path(ydl.prepare_filename(info))
        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown"),
            duration=int(info.get("duration", 0)),
            url=url
        )
        await update_status_panel(context)
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                RADIO_CHAT_ID, f,
                title=state.now_playing.title,
                duration=state.now_playing.duration
            )
        filepath.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed to download/send track {url}: {e}", exc_info=True)
        await context.bot.send_message(RADIO_CHAT_ID, "⚠️ Не удалось обработать трек.")

# --- Radio loop ---
async def radio_loop(context):
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
                    await asyncio.sleep(10)
                    continue
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            await asyncio.sleep(Constants.TRACK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"radio_loop error: {e}")
            await asyncio.sleep(5)

# --- UI ---
async def update_status_panel(context):
    state: State = context.bot_data['state']
    lines = [
        f"Статус: {'🟢' if state.is_on else '🔴'}",
        f"Жанр: {state.genre}",
        f"Источник: {state.source}"
    ]
    if state.now_playing:
        lines.append(f"Сейчас играет: {state.now_playing.title} ({format_duration(state.now_playing.duration)})")
    else:
        lines.append("Сейчас играет: ...загрузка...")
    text = "\n".join(lines)

    last_status_text = context.bot_data.get('last_status_text')
    if text == last_status_text:
        return

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="status:refresh")],
        [InlineKeyboardButton("⏭ Пропустить", callback_data="radio:skip")] if state.is_on else [],
        [InlineKeyboardButton("🗳️ Голосование", callback_data="vote:start")] if state.is_on else [],
        [InlineKeyboardButton("▶️ Запустить", callback_data="radio:on")] if not state.is_on else [InlineKeyboardButton("⏹️ Стоп", callback_data="radio:off")]
    ]
    try:
        if state.status_message_id:
            await context.bot.edit_message_text(
                chat_id=RADIO_CHAT_ID, message_id=state.status_message_id,
                text=text, reply_markup=InlineKeyboardMarkup([row for row in keyboard if row])
            )
        else:
            msg = await context.bot.send_message(RADIO_CHAT_ID, text, reply_markup=InlineKeyboardMarkup([row for row in keyboard if row]))
            state.status_message_id = msg.message_id
        context.bot_data['last_status_text'] = text
    except TelegramError as e:
        logger.warning(f"Failed to update status panel: {e}")

# --- Commands ---
async def toggle_radio(context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    state.is_on = turn_on
    message = ""
    if turn_on:
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        message = f"Радио включено. Источник: {state.source}"
    else:
        task = context.bot_data.get('radio_loop_task')
        if task:
            task.cancel()
        state.now_playing = None
        message = "Радио выключено."
    
    await context.bot.send_message(RADIO_CHAT_ID, message)

    await update_status_panel(context)
    await save_state_from_botdata(context.bot_data)

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    await toggle_radio(context, turn_on)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵\n\n" 
                                   "Используйте /play <название песни>, чтобы найти и послушать трек.\n" 
                                   "Администраторы могут использовать /ron и /rof для управления радио.")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in ["soundcloud", "youtube"]:
        await update.message.reply_text("Использование: /source soundcloud|youtube")
        return
    state: State = context.bot_data['state']
    state.source = context.args[0]
    await update.message.reply_text(f"Источник переключен на: {state.source}")
    await save_state_from_botdata(context.bot_data)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /play command from user {user_id}")
    if not context.args:
        await update.message.reply_text("Please provide a song name.")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'Searching for "{query}"...')
    logger.info(f"Searching for '{query}' for user {user_id}")

    ydl_opts = {
        'format': 'bestaudio',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'scsearch5',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info.get('entries'):
                await message.edit_text("No tracks found.")
                return

        keyboard = []
        for i, entry in enumerate(info['entries'][:5]):
            title = entry.get('title', 'Unknown Title')
            video_id = entry.get('id')
            keyboard.append([InlineKeyboardButton(f"▶️ {title}", callback_data=f"play_track:{video_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.edit_text('Please choose a track:', reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in /play search: {e}", exc_info=True)
        await message.edit_text("Sorry, an error occurred during search.")

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received button callback from user {user_id}")
    query = update.callback_query
    await query.answer()

    command, data = query.data.split(":", 1)

    if command == "play_track":
        video_id = data
        await query.edit_message_text(text=f"Processing track...")
        try:
            await download_and_send_to_chat(context, video_id, query.message.chat_id)
            await query.edit_message_text(text=f"Track sent!")
        except Exception as e:
            await query.edit_message_text(f"Failed to process track: {e}")

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    command, data = query.data.split(":", 1)
    user_id = query.from_user.id

    if command == "radio":
        if not await is_admin(user_id):
            await context.bot.send_message(user_id, "Эта команда только для администраторов.")
            return
        if data == "skip":
            await skip_track(context)
        elif data == "on":
            await toggle_radio(context, True)
        elif data == "off":
            await toggle_radio(context, False)
    elif command == "vote":
        if data == "start":
            await start_vote(context)

async def skip_track(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.is_on and context.bot_data.get('radio_loop_task'):
        context.bot_data['radio_loop_task'].cancel()
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))

async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.is_on:
        # This is a placeholder for the voting logic
        await context.bot.send_message(RADIO_CHAT_ID, "Voting is not implemented yet.")

# --- Bot Lifecycle ---
async def post_init(application: Application):
    application.bot_data['state'] = load_state()
    if application.bot_data['state'].is_on:
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))

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
    logger.info("Starting bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
