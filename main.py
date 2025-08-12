import logging
import os
import asyncio
import json
import random
import yt_dlp
import uuid
import signal
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
# The full public URL of your service (e.g., https://your-app.onrailway.app)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

logger.info("Bot token loaded.")
if BOT_TOKEN:
    masked_token = f"{BOT_TOKEN[:4]}...{BOT_TOKEN[-4:]}"
    logger.info(f"BOT_TOKEN loaded successfully. Masked value: {masked_token}")
else:
    logger.error("FATAL: BOT_TOKEN environment variable not set or is empty.")

if not WEBHOOK_URL:
    logger.error("FATAL: WEBHOOK_URL environment variable not set. Bot cannot start in webhook mode.")

# --- Config & FS Management ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"is_on": False, "genre": "lo-fi hip hop"}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

# --- Bot Commands (No changes needed here, they remain the same) ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /start command from user {user_id}")
    text = """
    Welcome! Commands:
    /play <song name> - Search for a song.
    /ron <genre> - Start radio mode.
    /rof - Stop radio mode.
    /id - Get the ID of this chat.
    """
    await update.message.reply_text(text)
    logger.info(f"Replied to /start command from user {user_id}")

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    logger.info(f"Received /id command from user {user_id} in chat {chat_id}")
    await update.message.reply_text(f"This chat's ID is: {chat_id}")
    logger.info(f"Replied to /id command from user {user_id}")

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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received button callback from user {user_id}")
    query = update.callback_query
    await query.answer()

    command, data = query.data.split(":", 1)

    if command == "play_track":
        video_id = data
        await query.edit_message_text(text=f"Processing track...")
        try:
            track_info = await download_track(video_id)
            if track_info:
                await send_track(track_info, query.message.chat_id, context.bot)
                await query.edit_message_text(text=f"Track sent!")
                if os.path.exists(track_info['filepath']):
                    os.remove(track_info['filepath'])
            else:
                await query.edit_message_text("Failed to process track.")
        except Exception as e:
            await query.edit_message_text(f"Failed to process track: {e}")

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /ron command from user {user_id}")
    if user_id != ADMIN_ID:
        logger.warning(f"Unauthorized /ron attempt by user {user_id}")
        await update.message.reply_text("You are not authorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /ron <genre>")
        return

    genre = " ".join(context.args)
    config = load_config()
    config['is_on'] = True
    config['genre'] = genre
    save_config(config)
    await update.message.reply_text(f"Radio mode ON. Genre: {genre}")
    logger.info(f"Radio mode turned ON by admin {user_id} with genre '{genre}'")

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Received /rof command from user {user_id}")
    if user_id != ADMIN_ID:
        logger.warning(f"Unauthorized /rof attempt by user {user_id}")
        await update.message.reply_text("You are not authorized.")
        return

    config = load_config()
    config['is_on'] = False
    save_config(config)
    await update.message.reply_text("Radio mode OFF.")
    logger.info(f"Radio mode turned OFF by admin {user_id}")

# --- Music Handling (No changes needed here, they remain the same) ---
async def download_track(video_id: str, query: str = None):
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
    
    search_query = video_id if video_id else query
    if not search_query:
        return None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=True)
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            return {
                "filepath": filename,
                "title": info.get('title', 'Unknown Title'),
                "duration": info.get('duration', 0)
            }
    except Exception as e:
        logger.error(f"Failed to download track {search_query}: {e}")
        return None

async def send_track(track_info: dict, chat_id: int, bot):
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

async def radio_loop(application: Application):
    next_track_info = None
    while True:
        await asyncio.sleep(2)
        config = load_config()
        
        if not config.get('is_on'):
            if next_track_info and os.path.exists(next_track_info['filepath']):
                os.remove(next_track_info['filepath'])
            next_track_info = None
            continue

        try:
            if next_track_info is None:
                logger.info("[Radio] Fetching first track...")
                genre_query = f"{config['genre']} music"
                next_track_info = await download_track(None, query=f"scsearch1:{genre_query}")

            if not next_track_info:
                logger.warning("[Radio] Could not fetch a track. Retrying in 30s.")
                await asyncio.sleep(30)
                continue
            
            current_track_info = next_track_info

            logger.info("[Radio] Pre-fetching next track...")
            genre_query = f"{config['genre']} music"
            fetch_task = asyncio.create_task(download_track(None, query=f"scsearch1:{genre_query}"))

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
async def main() -> None:
    """Runs the bot in webhook mode."""
    if not BOT_TOKEN or not WEBHOOK_URL:
        logger.critical("BOT_TOKEN and WEBHOOK_URL must be set.")
        return

    # The port must be taken from the environment variable for platforms like Railway/Render
    port = int(os.environ.get("PORT", 8080))
    
    # We need to pass the bot instance to the radio_loop, so we create it first
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("play", play_command))
    application.add_handler(CommandHandler(["radio_on", "ron"], radio_on_command))
    application.add_handler(CommandHandler(["radio_off", "rof"], radio_off_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    # Start background tasks
    radio_task = asyncio.create_task(radio_loop(application))
    
    # Set up the webhook
    logger.info(f"Setting webhook to {WEBHOOK_URL}")
    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

    # Run the web server to listen for webhooks
    # The library's built-in web server is simple and effective
    async with application:
        logger.info(f"Starting webhook listener on port {port}")
        await application.start_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=f"/{BOT_TOKEN}" # The path part of the webhook URL
        )
        
        # Keep the application running
        # We can use a stop signal like before for graceful shutdown
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        # On shutdown, cancel background tasks and clean up
        logger.info("Shutdown signal received. Cleaning up...")
        if not radio_task.done():
            radio_task.cancel()
        await application.bot.delete_webhook()
        await application.stop()

if __name__ == "__main__":
    ensure_download_dir()
    logger.info("Starting application in webhook mode...")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application stopped by user.")
