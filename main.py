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
skip_event = asyncio.Event()
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

# --- Helper Functions ---
def format_duration(seconds):
    if not seconds or seconds == 0: return "--:--"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

def has_ukrainian_chars(text):
    return any(char in text for char in 'іІїЇєЄґҐ')

def build_search_queries(genre_str):
    decade_match = re.search(r'(70|80|90|2000|2010)-х$', genre_str)
    if decade_match:
        decade = decade_match.group(1)
        core_genre = genre_str[:decade_match.start()].strip().lower()
    else:
        decade, core_genre = None, genre_str.lower()

    base_keywords = GENRE_KEYWORDS.get(core_genre.split()[0], [core_genre])
    queries = []
    for kw in base_keywords:
        if decade:
            queries.append(f"{decade}s {kw} hits")
            queries.append(f"{decade}s {kw} best")
            queries.append(f"{decade}s {kw} playlist")
        else:
            queries.append(f"{kw} hits")
            queries.append(f"{kw} best")
            queries.append(f"{kw} playlist")
    return queries

def is_genre_match(track, genre_str):
    core_genre = genre_str.split()[0].lower()
    keywords = GENRE_KEYWORDS.get(core_genre, [core_genre])
    title = track.get('title') or ''
    description = track.get('description') or ''
    text = (title + ' ' + description).lower()
    return any(kw in text for kw in keywords)

# --- Config & FS Management ---
async def load_config():
    global config_cache
    async with config_lock:
        if config_cache:
            return config_cache
        
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_cache = json.load(f)
        else:
            config_cache = {}
            
        config_cache.setdefault('is_on', False)
        config_cache.setdefault('genre', 'lo-fi hip hop')
        config_cache.setdefault('radio_playlist', [])
        config_cache.setdefault('played_radio_urls', [])
        config_cache.setdefault('radio_message_ids', [])
        config_cache.setdefault('voting_interval_seconds', 3600)
        config_cache.setdefault('track_interval_seconds', 120)
        config_cache.setdefault('message_cleanup_limit', 30)
        config_cache.setdefault('poll_duration_seconds', 60)
        config_cache.setdefault('active_poll', None)
        config_cache.setdefault('status_message_id', None)
        return config_cache

async def save_config(config):
    global config_cache, last_config_save, config_save_task
    async with config_lock:
        config_cache = config
        if time.time() - last_config_save > CONFIG_SAVE_INTERVAL:
            _save_to_disk()
        elif not config_save_task or config_save_task.done():
            config_save_task = asyncio.create_task(schedule_config_save())

def _save_to_disk():
    global last_config_save
    # In a production environment (like Railway), this should write to a persistent store like Redis or a database.
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_cache, f, indent=4, ensure_ascii=False)
    last_config_save = time.time()
    logger.info("Configuration saved to disk.")

async def schedule_config_save():
    await asyncio.sleep(CONFIG_SAVE_INTERVAL)
    async with config_lock:
        _save_to_disk()

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

# --- Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"**V3 - STABLE**\nInstance: `{INSTANCE_ID}`\nПанель управления: /status")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "`/play <название>` - Поиск трека.\n"
        "`/status` - Панель управления.\n"
        "`/ron <жанр>` - Включить радио.\n"
        "`/rof` - Выключить радио.\n"
        "`/votestart` - Запустить голосование.\n"
        "`/skip` - Пропустить трек."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def get_paginated_keyboard(search_id: str, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    page_size = 5
    results = context.bot_data.get('paginated_searches', {}).get(search_id, [])
    if not results:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Ошибка: поиск не найден.", callback_data="noop:0")]])

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
        await update.message.reply_text("Укажите название песни: `/play <название>`")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'Ищу "{query}"...')
    
    try:
        info = await asyncio.to_thread(search_soundcloud, query, 30)
        if not info.get('entries'):
            await message.edit_text("Треки не найдены.")
            return

        search_id = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('paginated_searches', {})[search_id] = info['entries']
        reply_markup = await get_paginated_keyboard(search_id, context)
        await message.edit_text(f'Найдено: {len(info["entries"])}. Выберите трек:', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in /play: {e}")
        await message.edit_text("Ошибка поиска.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Эта кнопка не для вас.", show_alert=True)
        return
    await query.answer()
    command, data = query.data.split(":", 1)

    if command == "play_track":
        await handle_play_track_callback(query, context, data)
    elif command == "page":
        search_id, page_num_str = data.split(":")
        reply_markup = await get_paginated_keyboard(search_id, context, int(page_num_str))
        await query.edit_message_text('Выберите трек:', reply_markup=reply_markup)
    elif command == "status_refresh":
        await send_status_panel(context.application, query.message.chat_id, query.message.message_id)
    elif command == "toggle_radio":
        config = await load_config()
        if config.get('is_on'):
            await radio_off_command(update, context)
        else:
            await radio_on_command(update, context, use_last_genre=True)
        await send_status_panel(context.application, query.message.chat_id, query.message.message_id)
    elif command == "start_vote":
        await start_vote_command(update, context)
    elif command == "skip_track":
        await skip_track(update, context)

async def handle_play_track_callback(query, context, data):
    track_url = context.bot_data.get('track_urls', {}).get(data)
    if not track_url:
        await query.edit_message_text("Ошибка: трек устарел.")
        return
    await query.edit_message_text("Скачиваю...")
    try:
        track_info = await download_track(track_url)
        if track_info:
            await send_track(track_info, query.message.chat_id, context.bot)
            await query.edit_message_text("Трек отправлен!")
            if os.path.exists(track_info['filepath']): os.remove(track_info['filepath'])
        else:
            await query.edit_message_text("Не удалось скачать трек.")
    except Exception as e:
        logger.error(f"Error handling play_track callback: {e}")
        await query.edit_message_text(f"Ошибка: {e}")

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE, use_last_genre: bool = False):
    global radio_task, voting_task
    if update.effective_user.id != ADMIN_ID: return

    config = await load_config()
    genre = " ".join(context.args) if context.args else config.get('genre', 'lo-fi hip hop')
    if use_last_genre:
        genre = config.get('genre', 'lo-fi hip hop')

    async with playlist_lock:
        config.update({'is_on': True, 'genre': genre, 'radio_playlist': [], 'played_radio_urls': []})
        context.bot_data.update({'radio_playlist': deque(), 'played_radio_urls': []})
        await save_config(config)
    
    if not radio_task or radio_task.done():
        radio_task = asyncio.create_task(radio_loop_wrapper(context.application))
    if not voting_task or voting_task.done():
        voting_task = asyncio.create_task(hourly_voting_loop_wrapper(context.application))
    
    if update.message: await update.message.reply_text(f"Радио включено. Жанр: {genre}.")
    asyncio.create_task(refill_playlist_and_play(context.application))
    await update_status_panel_safe(context.application)

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global radio_task, voting_task
    if update.effective_user.id != ADMIN_ID: return
    
    config = await load_config()
    config['is_on'] = False
    await save_config(config)
    
    if radio_task: radio_task.cancel()
    if voting_task: voting_task.cancel()
    
    if update.message: await update.message.reply_text("Радио выключено.")
    await update_status_panel_safe(context.application)

async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    config = await load_config()
    if not config.get('is_on'): 
        if update.message: await update.message.reply_text("Радио выключено.")
        return
    if config.get('active_poll'): 
        if update.message: await update.message.reply_text("Голосование уже идет.")
        return
    
    if await _create_and_send_poll(context.application):
        if update.message: await update.message.reply_text("Голосование запущено.")
    else:
        if update.message: await update.message.reply_text("Ошибка запуска голосования.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await send_status_panel(context.application, update.message.chat_id)

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await skip_track(update, context)

async def skip_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = await load_config()
    if not config.get('is_on'):
        if update.message: await update.message.reply_text("Радио не включено.")
        return
    skip_event.set()
    if update.message: await update.message.reply_text("Пропускаю трек...")

# --- UI Panel Logic ---
async def send_status_panel(application: Application, chat_id: int, message_id: int = None):
    config = await load_config()
    is_on = config.get('is_on', False)
    status = "🟢 ВКЛ" if is_on else "🔴 ВЫКЛ"
    genre = config.get('genre', '-')

    text = f"**Панель Управления**\n\n**Статус:** {status}\n**Жанр:** `{genre}`"

    toggle_button_text = "Выключить радио" if is_on else "Включить радио"
    keyboard = [
        [InlineKeyboardButton(toggle_button_text, callback_data="toggle_radio:0")],
        [InlineKeyboardButton("🗳️ Новое голосование", callback_data="start_vote:0"),
         InlineKeyboardButton("⏭️ Пропустить", callback_data="skip_track:0")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="status_refresh:0")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if message_id:
            await application.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            sent_message = await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
            config['status_message_id'] = sent_message.message_id
            await save_config(config)
    except Exception as e:
        logger.warning(f"Error sending status panel: {e}")

async def update_status_panel_safe(application: Application):
    config = await load_config()
    message_id = config.get('status_message_id')
    if message_id:
        await send_status_panel(application, RADIO_CHAT_ID, message_id)

# --- Music & Radio Logic ---
def search_soundcloud(query: str, count: int):
    ydl_opts = {'format': 'bestaudio', 'noplaylist': True, 'quiet': True, 'default_search': f'scsearch{count}', 'extract_flat': 'in_playlist'}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

async def download_track(url: str):
    ensure_download_dir()
    out_template = os.path.join(DOWNLOAD_DIR, f'{uuid.uuid4()}.%(ext)s')
    ydl_opts = {
        'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'outtmpl': out_template, 'noplaylist': True, 'quiet': True
    }
    
    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            return {'filepath': filename, 'title': info.get('title', 'Unknown'), 'duration': info.get('duration', 0)}
            
    return await asyncio.to_thread(_download)

async def send_track(track_info: dict, chat_id: int, bot):
    try:
        with open(track_info['filepath'], 'rb') as audio_file:
            return await bot.send_audio(chat_id=chat_id, audio=audio_file, title=track_info['title'], duration=track_info['duration'])
    except Exception as e:
        logger.error(f"Failed to send track {track_info.get('filepath')}: {e}")
        return None

async def clear_old_tracks(bot, message_ids_deque):
    # ... (implementation unchanged)
    pass

async def refill_playlist_and_play(application: Application):
    async with playlist_lock:
        bot_data = application.bot_data
        logger.info("Attempting to refill radio playlist...")
        config = await load_config()
        raw_genre = config.get('genre', 'lo-fi hip hop')
        search_queries = build_search_queries(raw_genre)
        played = set(bot_data.get('played_radio_urls', []))
        
        suitable_tracks = []
        for query in search_queries:
            try:
                info = await asyncio.to_thread(search_soundcloud, query, 50)
                if not info or not info.get('entries'): continue
                for t in info['entries']:
                    if t and 60 < t.get('duration', 0) < 900 and t.get('url') not in played and not has_ukrainian_chars(t.get('title', '')) and is_genre_match(t, raw_genre):
                        suitable_tracks.append(t)
            except Exception as e:
                logger.error(f"Search error for '{query}': {e}")

        unique_tracks = {t['url']: t for t in suitable_tracks}
        final_tracks = list(unique_tracks.values())
        final_tracks.sort(key=lambda tr: (tr.get('view_count', 0) or 0) + (tr.get('like_count', 0) or 0), reverse=True)
        
        final_urls = [t['url'] for t in final_tracks[:50]]
        random.shuffle(final_urls)

        bot_data['radio_playlist'] = deque(final_urls)
        config['radio_playlist'] = final_urls
        await save_config(config)

        logger.info(f"Playlist refilled with {len(final_urls)} tracks.")

        if final_urls:
            skip_event.set()
        elif config.get('is_on'):
            logger.warning("Playlist is empty after refill, starting a new vote.")
            asyncio.create_task(_create_and_send_poll(application))

async def radio_loop_wrapper(application: Application):
    while True:
        try:
            await radio_loop(application)
        except asyncio.CancelledError:
            logger.info("Radio loop wrapper cancelled.")
            break
        except Exception as e:
            logger.critical(f"FATAL error in radio_loop, restarting in 60s: {e}", exc_info=True)
            await asyncio.sleep(60)

async def radio_loop(application: Application):
    bot_data = application.bot_data
    while True:
        config = await load_config()
        if not config.get('is_on'):
            await asyncio.sleep(30)
            continue

        track_url = None
        async with playlist_lock:
            if not bot_data.get('radio_playlist'):
                logger.info("Playlist empty, attempting to refill.")
                asyncio.create_task(refill_playlist_and_play(application))
            else:
                track_url = bot_data['radio_playlist'].popleft()
        
        if track_url:
            track_info = None
            try:
                track_info = await download_track(track_url)
                sent_msg = await send_track(track_info, RADIO_CHAT_ID, application.bot)
                if sent_msg:
                    async with playlist_lock:
                        # ... (update played_radio_urls, etc)
                        await save_config(config)
            except Exception as e:
                logger.error(f"Failed to process track {track_url}: {e}")
                skip_event.set() # Try next track immediately
            finally:
                if track_info and os.path.exists(track_info['filepath']): os.remove(track_info['filepath'])

        try:
            await asyncio.wait_for(skip_event.wait(), timeout=config.get('track_interval_seconds', 120))
            skip_event.clear()
            logger.info("Track skipped or finished.")
        except asyncio.TimeoutError:
            pass

# --- Voting Logic ---
async def hourly_voting_loop_wrapper(application: Application):
    while True:
        try:
            await hourly_voting_loop(application)
        except asyncio.CancelledError:
            logger.info("Voting loop wrapper cancelled.")
            break
        except Exception as e:
            logger.critical(f"FATAL error in hourly_voting_loop, restarting in 60s: {e}", exc_info=True)
            await asyncio.sleep(60)

async def hourly_voting_loop(application: Application):
    while True:
        config = await load_config()
        await asyncio.sleep(config.get('voting_interval_seconds', 3600))
        if config.get('is_on') and not config.get('active_poll'):
            await _create_and_send_poll(application)

async def _create_and_send_poll(application: Application) -> bool:
    config = await load_config()
    if config.get('active_poll'): return False
    
    votable_genres = config.get("votable_genres", [])
    if len(votable_genres) < 2: return False # Telegram requires at least 2 options
    
    options = random.sample(votable_genres, k=min(10, len(votable_genres)))
    
    try:
        poll_duration = config.get('poll_duration_seconds', 60)
        message = await application.bot.send_poll(RADIO_CHAT_ID, "Выбираем жанр на следующий час!", options, is_anonymous=False, open_period=poll_duration)
        
        poll_data = message.poll.to_dict()
        poll_data['close_timestamp'] = datetime.now().timestamp() + poll_duration
        config['active_poll'] = poll_data
        await save_config(config)

        logger.info(f"Poll {message.poll.id} sent.")
        asyncio.create_task(schedule_poll_processing(application, message.poll.id, poll_duration))
        return True
    except Exception as e:
        logger.error(f"Create poll error: {e}")
        return False

async def schedule_poll_processing(application: Application, poll_id: str, delay: int):
    await asyncio.sleep(delay + 5) # Add a little buffer
    # ... (implementation unchanged)
    pass

async def receive_poll_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (implementation unchanged)
    pass

async def process_poll_results(poll, application: Application):
    config = await load_config()
    if not config.get('is_on'): return

    winning_options = []
    max_votes = 0
    for option in poll.options:
        if option.voter_count > max_votes:
            max_votes = option.voter_count
            winning_options = [option.text]
        elif option.voter_count == max_votes:
            winning_options.append(option.text)

    if not winning_options:
        # If no votes, pick a random genre from the poll options
        final_winner = random.choice([o.text for o in poll.options])
    else:
        final_winner = random.choice(winning_options)

    logger.info(f"Poll winner: '{final_winner}'.")
    
    async with playlist_lock:
        config['genre'] = final_winner
        config['radio_playlist'] = []
        application.bot_data['radio_playlist'].clear()
        await save_config(config)
    
    await application.bot.send_message(RADIO_CHAT_ID, f"Голосование завершено! Играет: **{final_winner}**", parse_mode='Markdown')
    status_msg = await application.bot.send_message(RADIO_CHAT_ID, "⚙️ Составляю новый плейлист...")
    application.bot_data['playlist_status_message_id'] = status_msg.message_id
    
    await refill_playlist_and_play(application)
    await update_status_panel_safe(application)

# --- Application Setup ---
async def post_init(application: Application) -> None:
    logger.info(f"Bot starting up with instance ID: {INSTANCE_ID}")
    global radio_task, voting_task
    await load_config() # Initial load
    bot_data = application.bot_data
    bot_data['radio_playlist'] = deque(config_cache.get('radio_playlist', []))
    bot_data['played_radio_urls'] = config_cache.get('played_radio_urls', [])
    bot_data['radio_message_ids'] = deque(config_cache.get('radio_message_ids', []))
    
    if config_cache.get('is_on'):
        logger.info("Radio was ON at startup. Starting background tasks.")
        radio_task = asyncio.create_task(radio_loop_wrapper(application))
        voting_task = asyncio.create_task(hourly_voting_loop_wrapper(application))
    
    active_poll = config_cache.get('active_poll')
    if active_poll:
        # ... (logic to reschedule poll processing)
        pass
    
    await update_status_panel_safe(application)

def main() -> None: 
    if not BOT_TOKEN: logger.critical("FATAL: BOT_TOKEN not found."); return
    ensure_download_dir()
    
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    handlers = [
        CommandHandler("start", start_command),
        CommandHandler(["help", "h"], help_command),
        CommandHandler(["play", "p"], play_command),
        CommandHandler(["ron"], radio_on_command),
        CommandHandler(["rof"], radio_off_command),
        CommandHandler("votestart", start_vote_command),
        CommandHandler("status", status_command),
        CommandHandler("skip", skip_command),
        CallbackQueryHandler(button_callback),
        PollHandler(receive_poll_update)
    ]
    application.add_handlers(handlers)
    
    logger.info("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()