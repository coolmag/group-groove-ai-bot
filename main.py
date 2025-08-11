import logging
import os
import yt_dlp
from telegram import Update, Audio
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /play command."""
    if not context.args:
        await update.message.reply_text("Please provide a song name or YouTube/SoundCloud URL.")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f"Searching for "{query}"...")

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

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await message.edit_text("Downloading and converting...")
            info = ydl.extract_info(f"ytsearch:{query}", download=True)['entries'][0]
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)

        await message.edit_text("Uploading to Telegram...")
        with open(filename, 'rb') as audio_file:
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=audio_file,
                title=title,
                duration=duration
            )
        await message.delete()

    except Exception as e:
        logger.error("Error in play_command: %s", e, exc_info=True)
        await message.edit_text(f"Sorry, an error occurred: {e}")
    finally:
        if os.path.exists(filename):
            os.remove(filename)


def main() -> None:
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("play", play_command))

    logger.info("Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
