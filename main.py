import logging
import os
import asyncio
import json
import random
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv
from aiohttp import web
import signal

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Env validation ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables.")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
    RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID"))
except (TypeError, ValueError):
    raise ValueError("ADMIN_ID or RADIO_CHAT_ID are not set or invalid.")

CONFIG_FILE = "radio_config.json"
RADIO_CACHE = {"genre": None, "tracks": [], "last_update": 0}

# --- Config Management ---
def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error reading config: {e}")
    return {"is_on": False, "genre": "lo-fi hip hop"}

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    except OSError as e:
        logger.error(f"Error saving config: {e}")

# --- Async yt_dlp ---
async def run_yt_dlp(opts, query, download=False):
    loop = asyncio.get_running_loop()
    def _exec():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download)
    return await loop.run_in_executor(None, _exec)

# --- Web Server ---
async def web_server(stop_event: asyncio.Event):
    routes = web.RouteTableDef()
    @routes.get('/')
    async def hello(_):
        return web.Response(text="I am alive.")

    app = web.Application()
    app.add_routes(routes)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")

    try:
        await stop_event.wait()  # Ожидаем сигнала остановки
    finally:
        logger.info("Shutting down web server...")
        await runner.cleanup()

# --- Bot Commands ---
async def start_command(update: Update, _):
    text = """
Welcome! Commands:
/play <song name> - Search for a song.
/id - Get the ID of this chat.

Admin commands:
/radio_on <genre> - Start radio mode.
/radio_off - Stop radio mode.
"""
    await update.message.reply_text(text)

async def id_command(update: Update, _):
    await update.message.reply_text(f"This chat's ID is: {update.message.chat_id}")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a song name.")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'Searching for "{query}"...')

    ydl_opts = {
        'format': 'bestaudio',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch5',  # Ищем в YouTube
    }

    try:
        info = await run_yt_dlp(ydl_opts, query, download=False)
        if not info.get('entries'):
            await message.edit_text("No tracks found.")
            return

        context.user_data['search_results'] = {str(i): e for i, e in enumerate(info['entries'][:5])}
        keyboard = [
            [InlineKeyboardButton(f"▶️ {entry.get('title', 'Unknown')}", callback_data=f"play_track:{i}")]
            for i, entry in context.user_data['search_results'].items()
        ]
        await message.edit_text('Please choose a track:', reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Error in /play search: {e}", exc_info=True)
        await message.edit_text("Sorry, an error occurred during search.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cmd, idx = query.data.split(":", 1)
    if cmd == "play_track":
        track_info = context.user_data.get('search_results', {}).get(idx)
        if not track_info:
            await query.edit_message_text("Track info expired. Please search again.")
            return
        await query.edit_message_text("Processing track...")
        try:
            await download_and_send(track_info.get('id'), query.message.chat_id, context.bot)
            await query.edit_message_text("Track sent!")
        except Exception as e:
            logger.error(f"Error sending track: {e}", exc_info=True)
            await query.edit_message_text(f"Failed: {e}")

async def download_and_send(video_id, chat_id, bot):
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': '%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    info = await run_yt_dlp(ydl_opts, url, download=True)
    title = info.get('title', 'Unknown')
    duration = info.get('duration', 0)
    filename = f"{info.get('id')}.mp3"

    try:
        with open(filename, 'rb') as audio_file:
            await bot.send_audio(chat_id=chat_id, audio=audio_file, title=title, duration=duration)
    finally:
        if os.path.exists(filename):
            os.remove(filename)

# --- Radio ---
async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /radio_on <genre>")
        return
    genre = " ".join(context.args)
    config = load_config()
    config['is_on'] = True
    config['genre'] = genre
    save_config(config)
    await update.message.reply_text(f"Radio mode ON. Genre: {genre}")

async def radio_off_command(update: Update, _):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return
    config = load_config()
    config['is_on'] = False
    save_config(config)
    await update.message.reply_text("Radio mode OFF.")

async def get_cached_tracks(genre):
    now = asyncio.get_event_loop().time()
    if RADIO_CACHE['genre'] == genre and now - RADIO_CACHE['last_update'] < 3600:
        return RADIO_CACHE['tracks']
    opts = {'format': 'bestaudio', 'noplaylist': False, 'quiet': True, 'default_search': 'ytsearch10'}
    info = await run_yt_dlp(opts, f"{genre} playlist", download=False)
    tracks = info.get('entries', [])
    RADIO_CACHE.update({'genre': genre, 'tracks': tracks, 'last_update': now})
    return tracks

async def radio_loop(application: Application, stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            config = load_config()
            if config.get('is_on'):
                try:
                    tracks = await get_cached_tracks(config['genre'])
                    if not tracks:
                        logger.warning("No tracks found for radio.")
                        await asyncio.sleep(60)
                        continue
                    track = random.choice(tracks)
                    logger.info(f"[Radio] Playing: {track.get('title')}")
                    await download_and_send(track.get('id'), RADIO_CHAT_ID, application.bot)
                    # Подождать длительность трека + небольшой буфер
                    await asyncio.wait_for(stop_event.wait(), timeout=track.get('duration', 300) + 5)
                except Exception as e:
                    logger.error(f"Error in radio loop: {e}", exc_info=True)
                    await asyncio.wait_for(stop_event.wait(), timeout=60)
            else:
                # Радио выключено, ждём
                await asyncio.wait_for(stop_event.wait(), timeout=10)
    except asyncio.CancelledError:
        logger.info("Radio loop cancelled.")
    except Exception as e:
        logger.error(f"Radio loop unexpected error: {e}", exc_info=True)

# --- Post Init ---
async def post_init(application: Application):
    stop_event = asyncio.Event()
    application.stop_event = stop_event

    # Запуск фоновых задач
    application.radio_task = asyncio.create_task(radio_loop(application, stop_event))
    application.web_task = asyncio.create_task(web_server(stop_event))

# --- Graceful shutdown handler ---
def setup_signal_handlers(application: Application):
    loop = asyncio.get_event_loop()

    def _stop():
        logger.info("Received stop signal, shutting down...")
        application.stop_event.set()

        # Отменяем задачи
        if application.radio_task:
            application.radio_task.cancel()
        if application.web_task:
            application.web_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("radio_on", radio_on_command))
    app.add_handler(CommandHandler("radio_off", radio_off_command))

    logger.info("Starting bot...")

    setup_signal_handlers(app)
    app.run_polling()

if __name__ == "__main__":
    main()
