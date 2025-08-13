import logging
import os
import asyncio
import json
import random
import yt_dlp
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Message
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
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
            return json.load(f)
    return {"is_on": False, "genre": "lo-fi hip hop"}

def save_config(config):
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
    save_config(config)
    await update.message.reply_text(f"Режим радио ВКЛ. Жанр: {genre}.\n\nГотовлю первый трек, это может занять до минуты...")
    logger.info(f"Radio mode turned ON by admin {user_id} with genre '{genre}'")

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

# --- Music Handling ---
async def download_track(url: str = None, query: str = None, bot_data: dict = None):
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
    
    search_query = url if url else query
    if not search_query:
        return None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # If it's a search query (from radio), we apply advanced filtering and playlist logic
            if query:
                # --- Radio Playlist Logic ---
                if bot_data and 'played_radio_urls' in bot_data:
                    if len(bot_data['played_radio_urls']) >= 30:
                        logger.info("[Radio] Played 30 tracks, clearing session playlist.")
                        bot_data['played_radio_urls'].clear()
                elif bot_data and 'played_radio_urls' not in bot_data:
                    bot_data['played_radio_urls'] = []


                # --- Search Diversification & Uniqueness Check ---
                for attempt in range(10): # Max 10 attempts to find a new track
                    search_suffixes = [
                        "beats", "mix", "instrumental", "chill", "study", "hip hop", 
                        "music", "session", "vibes", "radio", "live", "remix"
                    ]
                    diversified_query = f"{query} {random.choice(search_suffixes)}"
                    logger.info(f"[Radio Attempt {attempt+1}/10] Diversified search: '{diversified_query}'")

                    search_result_opts = ydl_opts.copy()
                    search_result_opts.pop('postprocessors', None)
                    search_result_opts['extract_flat'] = 'in_playlist'
                    search_result_opts['default_search'] = 'scsearch100'

                    with yt_dlp.YoutubeDL(search_result_opts) as ydl_search:
                        search_result = ydl_search.extract_info(diversified_query.replace("scsearch1:", ""), download=False)
                    
                    if not search_result or not search_result.get('entries'):
                        logger.warning(f"Radio search for '{diversified_query}' yielded no results.")
                        continue # Try again with a different suffix

                    suitable_tracks = [t for t in search_result['entries'] if t.get('duration') and 60 < t['duration'] < 900]

                    if not suitable_tracks:
                        logger.warning(f"No suitable tracks found for '{diversified_query}' in the 1-15 minute range.")
                        continue # Try again

                    # Find an unplayed track from the suitable tracks
                    # random.shuffle(suitable_tracks) # Shuffle to not always pick the top ones
                    
                    for track_to_download in suitable_tracks:
                        track_url = track_to_download['url']
                        
                        # Check if track has been played (only if bot_data is available for radio mode)
                        if bot_data and track_url in bot_data.get('played_radio_urls', []):
                            logger.info(f"Track '{track_to_download['title']}' already played. Trying another from the same search results.")
                            continue # Try next track in suitable_tracks
                        
                        # Found a new track
                        if bot_data:
                            bot_data['played_radio_urls'].append(track_url)
                            logger.info(f"Found new track: {track_to_download['title']}. Playlist size: {len(bot_data['played_radio_urls'])}")
                        
                        info = ydl.extract_info(track_url, download=True)
                        filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
                        return {"filepath": filename, "title": info.get('title', 'Unknown Title'), "duration": info.get('duration', 0)}

                logger.warning("[Radio] Could not find a new, unplayed track after 10 attempts with multiple searches.")
                return None

            else: # If it's a direct URL from /play command
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
                return {
                    "filepath": filename,
                    "title": info.get('title', 'Unknown Title'),
                    "duration": info.get('duration', 0)
                }
    except Exception as e:
        logger.error(f"Failed to download track {search_query}: {e}", exc_info=True)
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
    for _ in range(10):
        if context.bot_data['radio_message_ids']:
            chat_id, message_id = context.bot_data['radio_message_ids'].popleft()
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                logger.info(f"Deleted message {message_id} from chat {chat_id}")
                await asyncio.sleep(1) # Avoid hitting rate limits
            except Exception as e:
                logger.error(f"Failed to delete message {message_id}: {e}")

async def radio_loop(application: Application):
    logger.info("Radio loop started.")
    while True:
        await asyncio.sleep(5) # Check every 5 seconds
        config = load_config()
        
        if not config.get('is_on'):
            await asyncio.sleep(15) # Sleep longer when inactive
            continue

        try:
            logger.info("[Radio] Searching for a track...")
            genre_query = f"{config['genre']}"
            track_info = await download_track(query=genre_query, bot_data=application.bot_data)

            if not track_info:
                logger.warning("[Radio] Could not fetch a track. Retrying in 60s.")
                await asyncio.sleep(60)
                continue
            
            logger.info(f"[Radio] Sending track: {track_info['title']}")
            sent_message = await send_track(track_info, RADIO_CHAT_ID, application.bot)
            
            if sent_message:
                application.bot_data['radio_message_ids'].append((sent_message.chat_id, sent_message.message_id))
                logger.info(f"Radio messages in queue: {len(application.bot_data['radio_message_ids'])}")
                
                if len(application.bot_data['radio_message_ids']) >= 30:
                    await clear_old_tracks(application)

            # Wait for 2 minutes before the next track
            logger.info("[Radio] Waiting for 120 seconds...")
            await asyncio.sleep(120)

        except Exception as e:
            logger.error(f"Error in radio loop: {e}", exc_info=True)
            await asyncio.sleep(30) # Wait before retrying after an error
        finally:
            if 'track_info' in locals() and track_info and os.path.exists(track_info['filepath']):
                os.remove(track_info['filepath'])


# --- Application Setup ---
async def post_init(application: Application) -> None:
    """This function is called after initialization but before polling starts."""
    # Initialize data structures for radio state
    application.bot_data['played_radio_urls'] = []
    application.bot_data['radio_message_ids'] = deque()
    
    await application.bot.set_my_commands([
        BotCommand("play", "Найти и скачать трек"),
        BotCommand("p", "Сокращение для /play"),
        BotCommand("ron", "Включить радио (только админ)"),
        BotCommand("rof", "Выключить радио (только админ)"),
        BotCommand("id", "Показать ID чата"),
        BotCommand("help", "Помощь по командам"),
        BotCommand("h", "Сокращение для /help"),
    ])
    asyncio.create_task(radio_loop(application))

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
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Starting bot in polling mode...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    ensure_download_dir()
    main()