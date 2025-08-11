import logging
import os
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Setup
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
BOT_TOKEN = os.getenv("BOT_TOKEN")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Please provide a song name.")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'⏳ Processing "{query}"...')

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': 'downloaded_song.%(ext)s',
        'noplaylist': True,
        'default_search': 'scsearch1',
        'quiet': True,
    }

    filename = ""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)['entries'][0]
            filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)

        with open(filename, 'rb') as audio_file:
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=audio_file,
                title=title,
                duration=duration
            )
        await message.delete()

    except Exception as e:
        logging.error("Error in play_command: %s", e, exc_info=True)
        await message.edit_text(f"Sorry, an error occurred.")
    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("play", play_command))
    logging.info("Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
