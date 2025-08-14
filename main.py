import logging
import os
import asyncio
import json
import random
import re
import yt_dlp
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from dotenv import load_dotenv
from collections import deque
import time

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
INSTANCE_ID = str(uuid.uuid4())

# --- Global Task & Event References ---
radio_task = None
voting_task = None
config_save_task = None
panel_update_task = None
skip_event = asyncio.Event()
pause_event = asyncio.Event()
pause_event.set() # Default to not paused
playlist_lock = asyncio.Lock()
config_lock = asyncio.Lock()

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

# --- In-memory State ---
config_cache = {}
last_config_save = 0
CONFIG_SAVE_INTERVAL = 5  # seconds

# --- Helper Functions ---
def format_duration(seconds):
    if not seconds or seconds <= 0: return "00:00"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

def create_progress_bar(current, total, length=12):
    if not total or total <= 0: return "[░░░░░░░░░░░░]"
    progress = int(length * current / total)
    return f"[{"█" * progress}{"░" * (length - progress)}" ]"

# --- Config & FS Management ---
async def load_config():
    global config_cache
    async with config_lock:
        if config_cache:
            return config_cache
        
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config_cache = json.load(f)
            else:
                config_cache = {}
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error loading config file: {e}")
            config_cache = {}
            
        config_cache.setdefault('is_on', False)
        config_cache.setdefault('is_paused', False)
        config_cache.setdefault('genre', 'lo-fi hip hop')
        config_cache.setdefault('radio_playlist', [])
        config_cache.setdefault('played_radio_urls', [])
        config_cache.setdefault('now_playing', None)
        config_cache.setdefault('status_message_id', None)
        return config_cache

async def save_config(config):
    global config_cache, last_config_save, config_save_task
    async with config_lock:
        config_cache = config
        if time.time() - last_config_save > CONFIG_SAVE_INTERVAL:
            await _save_to_disk_safe()
        elif not config_save_task or config_save_task.done():
            config_save_task = asyncio.create_task(schedule_config_save())

async def _save_to_disk_safe():
    async with config_lock:
        temp_config = config_cache.copy()
        if isinstance(temp_config.get('radio_playlist'), deque):
            temp_config['radio_playlist'] = list(temp_config['radio_playlist'])
        if isinstance(temp_config.get('played_radio_urls'), deque):
            temp_config['played_radio_urls'] = list(temp_config['played_radio_urls'])
        
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(temp_config, f, indent=4, ensure_ascii=False)
        last_config_save = time.time()
        logger.info("Configuration saved to disk.")

async def schedule_config_save():
    await asyncio.sleep(CONFIG_SAVE_INTERVAL)
    await _save_to_disk_safe()

# --- Bot Commands & Handlers ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Эта кнопка не для вас.", show_alert=True)
        return
    await query.answer()
    command, data = query.data.split(":", 1)

    if command == "toggle_radio":
        config = await load_config()
        if config.get('is_on'):
            await radio_off_command(update, context)
        else:
            await radio_on_command(update, context, use_last_genre=True)
    elif command == "pause_radio":
        await toggle_pause_radio(context.application)
    elif command == "skip_track":
        await skip_track(update, context)
    elif command == "prev_track":
        await prev_track(context)
    elif command == "start_vote":
        await start_vote_command(update, context)
    elif command == "status_refresh":
        await update_status_panel_safe(context.application)
    elif command == "noop":
        pass # Do nothing

async def toggle_pause_radio(application: Application):
    config = await load_config()
    if not config.get('is_on'): return

    now_playing = config.get('now_playing')
    if not config.get('is_paused', False):
        config['is_paused'] = True
        if now_playing:
            start_time_str = now_playing.get('start_time')
            if start_time_str:
                now_playing['pause_start_time'] = datetime.now(timezone.utc).isoformat()
                now_playing['elapsed_at_pause'] = (
                    (datetime.now(timezone.utc) - datetime.fromisoformat(start_time_str)).total_seconds()
                    - now_playing.get('pause_duration', 0)
                )
        pause_event.clear()
        logger.info("Radio paused.")
    else:
        config['is_paused'] = False
        if now_playing and now_playing.get('pause_start_time'):
            pause_start = datetime.fromisoformat(now_playing['pause_start_time'])
            now_playing['pause_duration'] = now_playing.get('pause_duration', 0) + (datetime.now(timezone.utc) - pause_start).total_seconds()
            now_playing.pop('pause_start_time', None)
        pause_event.set()
        logger.info("Radio resumed.")

    await save_config(config)
    await update_status_panel_safe(application)

# --- UI Panel Logic ---
async def send_status_panel(application: Application, chat_id: int, message_id: int = None):
    config = await load_config()
    now_playing = config.get('now_playing')
    is_on = config.get('is_on', False)
    is_paused = config.get('is_paused', False)

    status_icon = "⏸️" if is_paused else ("🟢" if is_on else "🔴")
    status_text = "ПАУЗА" if is_paused else ("В ЭФИРЕ" if is_on else "ВЫКЛЮЧЕНО")
    genre = config.get('genre', '-')
    
    text = (
        f"🎧 **Music Radio Player**\n"
        f"────────────────────────\n"
        f"**Статус:** {status_icon} *{status_text}*\n"
        f"**Жанр:** `{genre}`\n"
    )

    if is_on and now_playing:
        title = now_playing.get('title', 'Неизвестный трек')
        duration = now_playing.get('duration', 0)
        start_time_str = now_playing.get('start_time')
        
        elapsed = 0
        if start_time_str:
            start_time = datetime.fromisoformat(start_time_str)
            pause_duration = now_playing.get('pause_duration', 0)
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds() - pause_duration
            if is_paused:
                elapsed = now_playing.get('elapsed_at_pause', 0)

        progress_bar = create_progress_bar(elapsed, duration)
        text += (
            f"────────────────────────\n"
            f"**Сейчас играет:**\n"
            f"`{title}`\n\n"
            f"{progress_bar} {format_duration(elapsed)} / {format_duration(duration)}\n"
            f"────────────────────────\n"
        )
    else:
        text += "\n*Сейчас играет:* — тишина...\n────────────────────────\n"

    keyboard = []
    if is_on:
        play_pause_icon = "▶️" if is_paused else "⏸️"
        play_pause_text = "Продолжить" if is_paused else "Пауза"
        keyboard.append([
            InlineKeyboardButton("⏮", callback_data="prev_track:0"),
            InlineKeyboardButton(f"{play_pause_icon} {play_pause_text}", callback_data="pause_radio:0"),
            InlineKeyboardButton("⏭", callback_data="skip_track:0")
        ])
        keyboard.append([
            InlineKeyboardButton("🗳️ Голосование", callback_data="start_vote:0"),
            InlineKeyboardButton("⏹️ Стоп", callback_data="toggle_radio:0")
        ])
    else:
        keyboard.append([InlineKeyboardButton("▶️ Запустить Радио", callback_data="toggle_radio:0")])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="status_refresh:0")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if message_id:
            await application.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            sent_message = await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            config['status_message_id'] = sent_message.message_id
            await save_config(config)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Error sending status panel: {e}")

# --- Music & Radio Logic ---
async def radio_loop(application: Application):
    global panel_update_task
    while True:
        await pause_event.wait()
        
        config = await load_config()
        if not config.get('is_on'):
            await asyncio.sleep(30)
            continue

        track_url = None
        async with playlist_lock:
            playlist = deque(application.bot_data.get('radio_playlist', []))
            if not playlist:
                logger.info("Playlist empty, refilling...")
                await refill_playlist_and_play(application)
                continue
            track_url = playlist.popleft()
            application.bot_data['radio_playlist'] = playlist
        
        if track_url:
            track_info = None
            try:
                track_info = await download_track(track_url)
                
                if panel_update_task and not panel_update_task.done():
                    panel_update_task.cancel()
                config['now_playing'] = {
                    'title': track_info['title'],
                    'duration': track_info['duration'],
                    'start_time': datetime.now(timezone.utc).isoformat(),
                    'pause_duration': 0,
                }
                await save_config(config)
                panel_update_task = asyncio.create_task(panel_updater_loop(application))

                await send_track(track_info, RADIO_CHAT_ID, application.bot)

            except Exception as e:
                logger.error(f"Failed to process track {track_url}: {e}")
                skip_event.set()
                continue
            finally:
                if track_info and track_info.get('filepath') and os.path.exists(track_info['filepath']):
                    os.remove(track_info['filepath'])

        try:
            await asyncio.wait_for(skip_event.wait(), timeout=config.get('track_interval_seconds', 120))
        except asyncio.TimeoutError:
            pass
        finally:
            skip_event.clear()
            if panel_update_task and not panel_update_task.done():
                panel_update_task.cancel()
            config = await load_config()
            config['now_playing'] = None
            await save_config(config)
            logger.info("Track finished or skipped.")

# ... (The rest of the file)