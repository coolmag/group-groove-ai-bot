import logging
import os
import asyncio
import json
import random
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CONFIG_FILE = "radio_config.json"

# --- Config Management ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"is_on": False, "genre": "lo-fi hip hop"}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

# --- Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
    Welcome to the bot! Commands:
    /play <song name> - Search for a song.
    
    Admin commands:
    /radio_on <genre> - Start radio mode with a genre.
    /radio_off - Stop radio mode.
    """
    await update.message.reply_text(text)

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
        'default_search': 'scsearch5', # Search for 5 results on SoundCloud
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
    query = update.callback_query
    await query.answer()
    
    command, data = query.data.split(":", 1)

    if command == "play_track":
        video_id = data
        await query.edit_message_text(text=f"Processing track...")
        await download_and_send(video_id, query.message.chat_id, context)
        await query.edit_message_text(text=f"Track sent!")

async def download_and_send(video_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': 'downloaded_song.%(ext)s',
        'noplaylist': True,
    }
    filename = ""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_id, download=True)
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)

        with open(filename, 'rb') as audio_file:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=title,
                duration=duration
            )
    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return
    
    if not context.args:
        await update.message.reply_text("Please provide a genre. Usage: /radio_on <genre>")
        return

    genre = " ".join(context.args)
    config = load_config()
    config['is_on'] = True
    config['genre'] = genre
    save_config(config)
    await update.message.reply_text(f"Radio mode ON. Genre: {genre}")

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized.")
        return

    config = load_config()
    config['is_on'] = False
    save_config(config)
    await update.message.reply_text("Radio mode OFF.")

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    while True:
        await asyncio.sleep(5) # Check every 5 seconds
        config = load_config()
        if config.get('is_on'):
            try:
                chat_id = ADMIN_ID # Send radio to admin's chat, or a specific channel ID
                genre_query = f"{config['genre']} playlist"
                await context.bot.send_message(chat_id, f"Radio: Searching for a new track in genre '{config['genre']}'...")
                
                ydl_opts = {'format': 'bestaudio', 'noplaylist': False, 'quiet': True, 'default_search': 'scsearch1'}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(genre_query, download=False)
                    if info and info.get('entries'):
                        # Pick a random track from the found playlist/results
                        track = random.choice(info['entries'])
                        video_id = track.get('id')
                        await download_and_send(video_id, chat_id, context)
                        # Sleep for the duration of the track + a small buffer
                        duration = track.get('duration', 300)
                        await asyncio.sleep(duration + 5)
                    else:
                        await context.bot.send_message(chat_id, "Radio: Couldn't find tracks for the genre.")
                        await asyncio.sleep(60) # Wait a minute before retrying
            except Exception as e:
                logger.error(f"Error in radio loop: {e}", exc_info=True)
                await asyncio.sleep(60) # Wait on error

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("play", play_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(CommandHandler("radio_on", radio_on_command))
    application.add_handler(CommandHandler("radio_off", radio_off_command))

    # Start radio loop as a background task
    asyncio.create_task(radio_loop(application))

    logger.info("Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
