import logging
import os
import asyncio
import json
import random
import yt_dlp
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Message
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from dotenv import load_dotenv
from collections import deque

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

# --- Helper Functions ---
def format_duration(seconds):
    if not seconds or seconds == 0:
        return "--:--"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

# --- Config & FS Management ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {}

    # Ensure all keys are present with defaults
    config.setdefault('is_on', False)
    config.setdefault('genre', 'lo-fi hip hop')
    config.setdefault('radio_playlist', [])
    config.setdefault('played_radio_urls', [])
    config.setdefault('radio_message_ids', [])
    # Timing and limits configuration
    config.setdefault('voting_interval_seconds', 3600)
    config.setdefault('track_interval_seconds', 120)
    config.setdefault('message_cleanup_limit', 30)
    
    return config

def save_config(config):
    # When saving, convert deque to list for JSON serialization
    if isinstance(config.get('radio_message_ids'), deque):
        config['radio_message_ids'] = list(config['radio_message_ids'])
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

# --- Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /start command from user {user_id}")
    welcome_text = (
        f"Привет! Я музыкальный бот. 🎵\n\n"
        f"Используй /play для поиска песен или включи радио с помощью /ron.\n"
        f"Для полного списка команд и описания, используй /help."
    )
    await update.message.reply_text(welcome_text)
    logger.info(f"Replied to /start command from user {user_id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /help command from user {user_id}")
    help_text = (
        "🎧 **Описание бота и команды**\n\n"
        "Я могу искать и присылать треки, а также работать в режиме радио, проигрывая музыку по заданному жанру в фоновом режиме.\n\n"
        "**Основные команды:**\n"
        "🔹 `/play` или `/p` `<название песни>` - Поиск и загрузка трека. Я пришлю до 5 вариантов, из которых можно выбрать.\n"
        "🔹 `/id` - Показывает ID текущего чата.\n"
        "🔹 `/help` или `/h` - Показывает это сообщение.\n\n"
        "**Команды администратора:**\n"
        "🔸 `/ron` `<жанр>` - Включает режим радио. Бот начнет присылать треки указанного жанра в заданный чат (`RADIO_CHAT_ID`).\n"
        "🔸 `/rof` - Выключает режим радио.\n\n"
        "*Примечание: Для работы радио-режима бот должен быть добавлен в чат как администратор, а `RADIO_CHAT_ID` должен быть правильно указан в настройках."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    logger.info(f"Received /id command from user {user_id} in chat {chat_id}")
    await update.message.reply_text(f"ID этого чата: `{chat_id}`", parse_mode='Markdown')
    logger.info(f"Replied to /id command from user {user_id}")

async def get_paginated_keyboard(search_id: str, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    page_size = 5
    
    if 'paginated_searches' not in context.bot_data or search_id not in context.bot_data['paginated_searches']:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Ошибка: поиск не найден. Попробуйте снова.", callback_data="noop")]])

    results = context.bot_data['paginated_searches'][search_id]
    
    start_index = page * page_size
    end_index = start_index + page_size
    
    keyboard = []
    for entry in results[start_index:end_index]:
        title = entry.get('title', 'Unknown Title')
        duration_str = format_duration(entry.get('duration'))
        
        cache_key = uuid.uuid4().hex[:10]
        if 'track_urls' not in context.bot_data:
            context.bot_data['track_urls'] = {}
        context.bot_data['track_urls'][cache_key] = entry.get('url')
        
        keyboard.append([InlineKeyboardButton(f"▶️ {title} ({duration_str})", callback_data=f"play_track:{cache_key}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{search_id}:{page-1}"))
    
    total_pages = (len(results) + page_size - 1) // page_size
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page:{search_id}:{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)
        
    return InlineKeyboardMarkup(keyboard)


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /play command from user {user_id}")
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите название песни. Например: `/play queen bohemian rhapsody`")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'Ищу "{query}"...')
    logger.info(f"Searching for '{query}' for user {user_id}")

    ydl_opts = {
        'format': 'bestaudio',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'scsearch30',
        'extract_flat': 'in_playlist'
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info.get('entries'):
                await message.edit_text("Треки не найдены.")
                return

        search_id = uuid.uuid4().hex[:10]
        if 'paginated_searches' not in context.bot_data:
            context.bot_data['paginated_searches'] = {}
        
        context.bot_data['paginated_searches'][search_id] = info['entries']

        reply_markup = await get_paginated_keyboard(search_id, context, page=0)
        await message.edit_text(f'Найдено треков: {len(info["entries"])}. Пожалуйста, выберите трек:', reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in /play search: {e}", exc_info=True)
        await message.edit_text("Произошла ошибка во время поиска.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received button callback from user {user_id}")
    query = update.callback_query
    await query.answer()

    command, data = query.data.split(":", 1)

    if command == "play_track":
        cache_key = data
        if 'track_urls' not in context.bot_data or cache_key not in context.bot_data['track_urls']:
            await query.edit_message_text("Ошибка: результат поиска устарел. Пожалуйста, попробуйте снова.")
            return

        track_url = context.bot_data['track_urls'][cache_key]
        await query.edit_message_text(text=f"Обрабатываю трек...")
        
        try:
            track_info = await download_track(url=track_url, bot_data=context.bot_data)
            if track_info:
                await send_track(track_info, query.message.chat_id, context.bot)
                await query.edit_message_text(text=f"Трек отправлен!")
                if os.path.exists(track_info['filepath']):
                    os.remove(track_info['filepath'])
            else:
                await query.edit_message_text("Не удалось обработать трек.")
        except Exception as e:
            await query.edit_message_text(f"Не удалось обработать трек: {e}")
    
    elif command == "page":
        search_id, page_num_str = data.split(":", 1)
        page_num = int(page_num_str)
        
        reply_markup = await get_paginated_keyboard(search_id, context, page=page_num)
        
        if search_id in context.bot_data['paginated_searches']:
            results = context.bot_data['paginated_searches'][search_id]
            await query.edit_message_text(f'Найдено треков: {len(results)}. Пожалуйста, выберите трек:', reply_markup=reply_markup)
        else:
            await query.edit_message_text('Ошибка: поиск устарел.', reply_markup=None)

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /ron command from user {user_id}")
    if user_id != ADMIN_ID:
        logger.warning(f"Unauthorized /ron attempt by user {user_id}")
        await update.message.reply_text("Вы не авторизованы.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /ron <жанр>")
        return

    genre = " ".join(context.args)
    config = load_config()
    config['is_on'] = True
    config['genre'] = genre
    # Clear old playlist data for the new genre
    config['radio_playlist'] = []
    config['played_radio_urls'] = []
    context.bot_data['radio_playlist'] = deque()
    context.bot_data['played_radio_urls'] = []
    
    save_config(config)
    await update.message.reply_text(f"Режим радио ВКЛ. Жанр: {genre}.\n\nГотовлю плейлист, это может занять до минуты...")
    logger.info(f"Radio mode turned ON by admin {user_id} with genre '{genre}'. Playlist cleared.")

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /rof command from user {user_id}")
    if user_id != ADMIN_ID:
        logger.warning(f"Unauthorized /rof attempt by user {user_id}")
        await update.message.reply_text("Вы не авторизованы.")
        return

    config = load_config()
    config['is_on'] = False
    save_config(config)
    await update.message.reply_text("Режим радио ВЫКЛ.")
    logger.info(f"Radio mode turned OFF by admin {user_id}")

async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually starts a genre vote poll."""
    user_id = update.effective_user.id
    logger.info(f"Received /votestart command from user {user_id}")
    if user_id != ADMIN_ID:
        logger.warning(f"Unauthorized /votestart attempt by user {user_id}")
        await update.message.reply_text("Вы не авторизованы.")
        return

    config = load_config()
    if not config.get('is_on'):
        await update.message.reply_text("Нельзя начать голосование, когда радио выключено.")
        return

    success = await _create_and_send_poll(context.application)
    if success:
        await update.message.reply_text("Голосование за жанр запущено.")
    else:
        await update.message.reply_text("Не удалось запустить голосование. Проверьте логи.")

# --- Music Handling ---
async def _refill_radio_playlist(config: dict, bot_data: dict) -> bool:
    """Searches for new tracks and refills the radio playlist."""
    logger.info("[Radio] Playlist is empty. Refilling...")
    genre_query = config.get('genre', 'lo-fi hip hop')
    played_urls = set(bot_data.get('played_radio_urls', []))
    new_playlist = []

    ydl_opts = {
        'format': 'bestaudio',
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist',
        'default_search': 'scsearch50', # Search for 50 tracks
    }

    # Use a clean, direct query for the best results.
    direct_query = f"{genre_query}"
    logger.info(f"[Radio] Searching for new tracks with query: '{direct_query}'")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_result = ydl.extract_info(direct_query, download=False)

        if not search_result or not search_result.get('entries'):
            logger.warning(f"[Radio] Search for '{direct_query}' yielded no results.")
            return False

        # Filter tracks by duration and whether they have been played recently
        for track in search_result['entries']:
            if not (track and track.get('url')):
                continue
            
            duration = track.get('duration')
            url = track.get('url')

            if url not in played_urls and duration and 60 < duration < 900:
                new_playlist.append(url)
        
        if not new_playlist:
            logger.warning("[Radio] No new, suitable tracks found from the search.")
            return False

        # Do not shuffle the playlist. Play tracks in order of popularity from SoundCloud.
        # random.shuffle(new_playlist)
        bot_data['radio_playlist'].extend(new_playlist)
        config['radio_playlist'] = list(bot_data['radio_playlist']) # Update config for saving
        logger.info(f"[Radio] Refilled playlist with {len(new_playlist)} new tracks.")
        return True

    except Exception as e:
        logger.error(f"[Radio] Failed to refill playlist: {e}", exc_info=True)
        return False

async def download_track(url: str, bot_data: dict = None):
    ensure_download_dir()
    unique_id = uuid.uuid4()
    out_template = os.path.join(DOWNLOAD_DIR, f'{unique_id}.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'outtmpl': out_template,
        'noplaylist': True,
        'quiet': True,
    }
    
    if not url:
        return None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            return {
                "filepath": filename,
                "title": info.get('title', 'Unknown Title'),
                "duration": info.get('duration', 0),
                "url": url # Return the original URL
            }
    except Exception as e:
        logger.error(f"Failed to download track {url}: {e}", exc_info=True)
        return None

async def send_track(track_info: dict, chat_id: int, bot) -> Message | None:
    try:
        with open(track_info['filepath'], 'rb') as audio_file:
            message = await bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=track_info['title'],
                duration=track_info['duration']
            )
            return message
    except Exception as e:
        logger.error(f"Failed to send track {track_info['filepath']}: {e}")
        return None

async def clear_old_tracks(context: ContextTypes.DEFAULT_TYPE):
    """Deletes the 10 oldest track messages from the chat."""
    logger.info(f"Track limit reached. Clearing 10 oldest tracks.")
    config = load_config()
    for _ in range(10):
        if context.bot_data['radio_message_ids']:
            chat_id, message_id = context.bot_data['radio_message_ids'].popleft()
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                logger.info(f"Deleted message {message_id} from chat {chat_id}")
                await asyncio.sleep(1) # Avoid hitting rate limits
            except Exception as e:
                logger.error(f"Failed to delete message {message_id}: {e}")
    # Persist the changes to the message queue
    config['radio_message_ids'] = list(context.bot_data['radio_message_ids'])
    save_config(config)

async def radio_loop(application: Application):
    logger.info("Radio loop started.")
    bot_data = application.bot_data

    while True:
        await asyncio.sleep(5) # Check every 5 seconds
        config = load_config()
        
        if not config.get('is_on'):
            await asyncio.sleep(15)
            continue

        track_interval = config.get('track_interval_seconds', 120)
        cleanup_limit = config.get('message_cleanup_limit', 30)

        try:
            # Refill playlist if it's empty
            if not bot_data.get('radio_playlist'):
                refill_successful = await _refill_radio_playlist(config, bot_data)
                if not refill_successful:
                    logger.warning("[Radio] Failed to refill playlist. Retrying in 60s.")
                    await asyncio.sleep(60)
                    continue
                save_config(config) # Save the newly filled playlist

            # Get next track from the playlist
            track_url = bot_data['radio_playlist'].popleft()
            logger.info(f"[Radio] Processing next track from playlist: {track_url}")

            track_info = await download_track(url=track_url, bot_data=bot_data)

            if not track_info:
                logger.warning(f"[Radio] Could not download track URL: {track_url}. Skipping.")
                # Persist the state change immediately
                config['radio_playlist'] = list(bot_data['radio_playlist'])
                save_config(config)
                continue
            
            logger.info(f"[Radio] Sending track: {track_info['title']}")
            sent_message = await send_track(track_info, RADIO_CHAT_ID, application.bot)
            
            if sent_message:
                # Manage played tracks history (session)
                bot_data['played_radio_urls'].append(track_info['url'])
                if len(bot_data['played_radio_urls']) > 50: # Keep history of last 50
                    bot_data['played_radio_urls'].pop(0)

                # Manage message queue for deletion
                bot_data['radio_message_ids'].append((sent_message.chat_id, sent_message.message_id))
                if len(bot_data['radio_message_ids']) >= cleanup_limit:
                    await clear_old_tracks(application)

                # Persist state after sending a track
                config['radio_playlist'] = list(bot_data['radio_playlist'])
                config['played_radio_urls'] = bot_data['played_radio_urls']
                config['radio_message_ids'] = list(bot_data['radio_message_ids'])
                save_config(config)

            # Wait for the configured interval before the next track
            logger.info(f"[Radio] Waiting for {track_interval} seconds...")
            await asyncio.sleep(track_interval)

        except Exception as e:
            logger.error(f"Error in radio loop: {e}", exc_info=True)
            await asyncio.sleep(30)
        finally:
            if 'track_info' in locals() and track_info and os.path.exists(track_info['filepath']):
                os.remove(track_info['filepath'])

async def hourly_voting_loop(application: Application):
    """Initiates a genre poll periodically if the radio is on."""
    logger.info("Hourly voting loop started.")
    while True:
        config = load_config()
        interval = config.get('voting_interval_seconds', 3600)
        
        # Wait for the configured interval. Check every minute to see if it has changed.
        # This loop allows the interval to be changed dynamically without restarting the bot.
        for _ in range(int(interval / 60)):
            await asyncio.sleep(60)
            current_config = load_config()
            if not current_config.get('is_on') or current_config.get('voting_interval_seconds', 3600) != interval:
                break # Stop waiting if radio is turned off or interval changes

        config = load_config() # Reload config in case it changed
        if not config.get('is_on'):
            logger.info("[Voting] Radio is off, skipping poll.")
            continue

        await _create_and_send_poll(application)

async def _create_and_send_poll(application: Application) -> bool:
    """Generates options and sends the genre poll."""
    logger.info("[Voting] Starting genre poll.")
    config = load_config()
    try:
        votable_genres = config.get("votable_genres", [])
        if len(votable_genres) < 10:
            logger.warning("[Voting] Not enough genres in config to start a poll.")
            return False

        decades = ["70-х", "80-х", "90-х", "2000-х", "2010-х"]
        
        special_options = set()
        while len(special_options) < 5:
            genre = random.choice(votable_genres)
            decade = random.choice(decades)
            special_options.add(f"{genre} {decade}")

        regular_options = set()
        base_genres_in_special = {opt.split(' ')[0] for opt in special_options}
        pool_for_regular = [g for g in votable_genres if g not in base_genres_in_special and g.lower() != 'pop']
        
        num_to_sample = min(4, len(pool_for_regular))
        regular_options.update(random.sample(pool_for_regular, k=num_to_sample))

        options = list(special_options) + list(regular_options)
        while len(options) < 9:
            chosen_genre = random.choice(votable_genres)
            if chosen_genre not in options:
                options.append(chosen_genre)

        options.append("Pop")
        random.shuffle(options)
        
        question = "Выбираем жанр на следующий час!"

        message = await application.bot.send_poll(
            chat_id=RADIO_CHAT_ID,
            question=question,
            options=options[:10],
            is_anonymous=False,
            open_period=60,
        )
        
        application.bot_data['genre_poll_id'] = message.poll.id
        logger.info(f"[Voting] Poll {message.poll.id} sent to chat {RADIO_CHAT_ID}.")
        return True

    except Exception as e:
        logger.error(f"Error in _create_and_send_poll: {e}", exc_info=True)
        return False


async def receive_poll_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the result of the hourly genre poll."""
    poll = update.poll
    logger.info(f"Received poll update for poll {poll.id}")

    # Check if this is the poll we are waiting for
    if context.bot_data.get('genre_poll_id') != poll.id:
        logger.info(f"Poll {poll.id} is not the one we are tracking. Ignoring.")
        return

    # Clean up poll id immediately
    context.bot_data['genre_poll_id'] = None

    # Check if radio is still on
    config = load_config()
    if not config.get('is_on'):
        logger.info("[Voting] Radio was turned off during the poll. Ignoring results.")
        await context.bot.send_message(RADIO_CHAT_ID, "Голосование отменено, так как радио было выключено.")
        return

    winning_option = None
    max_votes = -1
    total_votes = 0

    for option in poll.options:
        total_votes += option.voter_count
        if option.voter_count > max_votes:
            max_votes = option.voter_count
            winning_option = option.text

    # Default to Pop if no one voted
    if total_votes == 0:
        winning_option = "Pop"
        logger.info("[Voting] No votes received. Defaulting to Pop.")
    else:
        logger.info(f"[Voting] Winning genre is '{winning_option}' with {max_votes} vote(s).")

    # Update config
    config['genre'] = winning_option
    # Clear the playlist so the new genre takes effect immediately
    config['radio_playlist'] = []
    context.bot_data['radio_playlist'] = deque()
    save_config(config)
    
    await context.bot.send_message(RADIO_CHAT_ID, f"Голосование завершено! Следующий час играет: **{winning_option}**", parse_mode='Markdown')

# --- Application Setup ---
async def post_init(application: Application) -> None:
    """This function is called after initialization but before polling starts."""
    # Load persisted state from config
    config = load_config()
    application.bot_data['radio_playlist'] = deque(config.get('radio_playlist', []))
    application.bot_data['played_radio_urls'] = config.get('played_radio_urls', [])
    application.bot_data['radio_message_ids'] = deque(config.get('radio_message_ids', []))
    application.bot_data['genre_poll_id'] = None # This should not be persisted

    logger.info(f"Loaded {len(application.bot_data['radio_playlist'])} tracks into playlist.")
    logger.info(f"Loaded {len(application.bot_data['played_radio_urls'])} played URLs.")
    logger.info(f"Loaded {len(application.bot_data['radio_message_ids'])} message IDs.")

    await application.bot.set_my_commands([
        BotCommand("play", "Найти и скачать трек"),
        BotCommand("p", "Сокращение для /play"),
        BotCommand("ron", "Включить радио (только админ)"),
        BotCommand("rof", "Выключить радио (только админ)"),
        BotCommand("votestart", "Запустить голосование за жанр (админ)"),
        BotCommand("id", "Показать ID чата"),
        BotCommand("help", "Помощь по командам"),
        BotCommand("h", "Сокращение для /help"),
    ])
    asyncio.create_task(radio_loop(application))
    asyncio.create_task(hourly_voting_loop(application))

# --- Main Application Logic (Polling) ---
def main() -> None:
    """Runs the bot in polling mode."""
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN not found.")
        return

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler(["help", "h"], help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler(["play", "p"], play_command))
    application.add_handler(CommandHandler(["radio_on", "ron"], radio_on_command))
    application.add_handler(CommandHandler(["radio_off", "rof"], radio_off_command))
    application.add_handler(CommandHandler("votestart", start_vote_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(PollHandler(receive_poll_update))

    logger.info("Starting bot in polling mode...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    ensure_download_dir()
    main()