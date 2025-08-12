import logging
import os
import asyncio
import json
import random
import yt_dlp
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

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
def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /start command from user {user_id}")
    # A shorter welcome, directing user to /help for details
    welcome_text = (
        f"Привет! Я музыкальный бот. 🎵\n\n"
        f"Используй /play для поиска песен или включи радио с помощью /ron.\n"
        f"Для полного списка команд и описания, используй /help."
    )
    await update.message.reply_text(welcome_text)
    logger.info(f"Replied to /start command from user {user_id}")

def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    logger.info(f"Received /id command from user {user_id} in chat {chat_id}")
    await update.message.reply_text(f"ID этого чата: `{chat_id}`", parse_mode='Markdown')
    logger.info(f"Replied to /id command from user {user_id}")

def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        'default_search': 'scsearch5',
        'extract_flat': 'in_playlist'
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info.get('entries'):
                await message.edit_text("Треки не найдены.")
                return

        if 'search_results' not in context.bot_data:
            context.bot_data['search_results'] = {}

        keyboard = []
        for entry in info['entries'][:5]:
            title = entry.get('title', 'Unknown Title')
            duration_str = format_duration(entry.get('duration'))
            cache_key = uuid.uuid4().hex[:10]
            context.bot_data['search_results'][cache_key] = entry.get('url')
            
            keyboard.append([InlineKeyboardButton(f"▶️ {title} ({duration_str})", callback_data=f"play_track:{cache_key}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.edit_text('Пожалуйста, выберите трек:', reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error in /play search: {e}", exc_info=True)
        await message.edit_text("Произошла ошибка во время поиска.")

def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received button callback from user {user_id}")
    query = update.callback_query
    await query.answer()

    command, cache_key = query.data.split(":", 1)

    if command == "play_track":
        if 'search_results' not in context.bot_data or cache_key not in context.bot_data['search_results']:
            await query.edit_message_text("Ошибка: результат поиска устарел. Пожалуйста, попробуйте снова.")
            return

        track_url = context.bot_data['search_results'][cache_key]
        await query.edit_message_text(text=f"Обрабатываю трек...")
        
        try:
            track_info = await download_track(url=track_url)
            if track_info:
                await send_track(track_info, query.message.chat_id, context.bot)
                await query.edit_message_text(text=f"Трек отправлен!")
                if os.path.exists(track_info['filepath']):
                    os.remove(track_info['filepath'])
            else:
                await query.edit_message_text("Не удалось обработать трек.")
        except Exception as e:
            await query.edit_message_text(f"Не удалось обработать трек: {e}")

def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(f"Режим радио ВКЛ. Жанр: {genre}")
    logger.info(f"Radio mode turned ON by admin {user_id} with genre '{genre}'")

def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
def download_track(url: str = None, query: str = None):
    ensure_download_dir()
    unique_id = uuid.uuid4()
    out_template = os.path.join(DOWNLOAD_DIR, f'{unique_id}.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'outtmpl': out_template,
        'noplaylist': True,
        'quiet': True,
    }
    
    search_query = url if url else query
    if not search_query:
        return None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if query:
                search_result = ydl.extract_info(query, download=False)
                if not search_result or not search_result.get('entries'):
                    return None
                info = ydl.extract_info(search_result['entries'][0]['url'], download=True)
            else:
                info = ydl.extract_info(url, download=True)

            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            return {
                "filepath": filename,
                "title": info.get('title', 'Unknown Title'),
                "duration": info.get('duration', 0)
            }
    except Exception as e:
        logger.error(f"Failed to download track {search_query}: {e}")
        return None

def send_track(track_info: dict, chat_id: int, bot):
    try:
        with open(track_info['filepath'], 'rb') as audio_file:
            await bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=track_info['title'],
                duration=track_info['duration']
            )
    except Exception as e:
        logger.error(f"Failed to send track {track_info['filepath']}: {e}")

def radio_loop(application: Application):
    logger.info("Radio loop started.")
    next_track_info = None
    while True:
        await asyncio.sleep(3)
        config = load_config()
        
        if not config.get('is_on'):
            if next_track_info and os.path.exists(next_track_info['filepath']):
                os.remove(next_track_info['filepath'])
            next_track_info = None
            continue

        try:
            if next_track_info is None:
                logger.info("[Radio] No pre-fetched track. Fetching one now...")
                genre_query = f"scsearch1:{config['genre']} music"
                next_track_info = await download_track(query=genre_query)

            if not next_track_info:
                logger.warning("[Radio] Could not fetch a track. Retrying in 30s.")
                await asyncio.sleep(30)
                continue
            
            current_track_info = next_track_info

            logger.info("[Radio] Pre-fetching next track in background...")
            genre_query = f"scsearch1:{config['genre']} music"
            fetch_task = asyncio.create_task(download_track(query=genre_query))

            logger.info(f"[Radio] Sending track: {current_track_info['title']}")
            await send_track(current_track_info, RADIO_CHAT_ID, application.bot)
            
            next_track_info = await fetch_task
            
            sleep_duration = current_track_info.get('duration', 180)
            logger.info(f"[Radio] Waiting for {sleep_duration} seconds.")
            await asyncio.sleep(sleep_duration)

        except Exception as e:
            logger.error(f"Error in radio loop: {e}", exc_info=True)
            next_track_info = None
            await asyncio.sleep(30)
        finally:
            if 'current_track_info' in locals() and current_track_info and os.path.exists(current_track_info['filepath']):
                os.remove(current_track_info['filepath'])

# --- Application Setup ---
def post_init(application: Application) -> None:
    """This function is called after initialization but before polling starts."""
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
