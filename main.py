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
from aiohttp import web

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

logger.info("Bot token loaded.")
if not BOT_TOKEN:
    logger.error("FATAL: BOT_TOKEN environment variable not set.")

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

# --- Web Server (modified for graceful shutdown) ---
async def web_server(stop_event: asyncio.Event):
    routes = web.RouteTableDef()
    @routes.get('/')
    async def hello(request):
        return web.Response(text="I am alive.")

    app = web.Application()
    app.add_routes(routes)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info(f"Web server started on port {port}")
        await stop_event.wait()
    finally:
        logger.info("Web server is shutting down.")
        await runner.cleanup()

# --- Bot Commands ---
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

# --- Music Handling ---
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
radio_task = None

async def post_init(application: Application) -> None:
    global radio_task
    logger.info("Running post_init setup...")
    await application.bot.set_my_commands([
        BotCommand("play", "Search for a song"),
        BotCommand("ron", "Start radio (Admin)"),
        BotCommand("rof", "Stop radio (Admin)"),
        BotCommand("id", "Get chat ID")
    ])
    radio_task = asyncio.create_task(radio_loop(application))
    logger.info("Post_init setup complete.")

async def main() -> None:
    """Runs the bot with graceful shutdown."""
    logger.info("Waiting for 15 seconds to allow old instances to terminate...")
    await asyncio.sleep(15)

    logger.info("Setting up graceful shutdown...")
    stop_event = asyncio.Event()
    
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    logger.info("Signal handlers set.")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Clear any pending updates from Telegram servers
    logger.info("Clearing any pending updates...")
    await application.initialize()
    updates = await application.bot.get_updates(offset=-1, timeout=5)
    if updates:
        logger.info(f"Cleared {len(updates)} old updates.")
        # The library will get the next update with the correct offset automatically

    async with application:
        logger.info("Starting application...")
        # We already initialized, so we don't call it again
        await application.start()
        await application.updater.start_polling()
        logger.info("Application polling started.")

        web_task = asyncio.create_task(web_server(stop_event))

        await stop_event.wait()

        logger.info("Shutdown signal received. Cleaning up...")
        await application.updater.stop()
        await application.stop()
        
        if radio_task and not radio_task.done():
            radio_task.cancel()
        if web_task and not web_task.done():
            web_task.cancel()
        
        await asyncio.gather(radio_task, web_task, return_exceptions=True)
        logger.info("Cleanup complete.")

if __name__ == "__main__":
    ensure_download_dir()
    logger.info("Starting application run...")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application stopped by user.")
    except asyncio.CancelledError:
        logger.info("Main task cancelled.")