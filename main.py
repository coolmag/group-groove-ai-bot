import asyncio
import os
import logging
import subprocess
from pytgcalls.types import AudioPiped
import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')

app = Client(
    'my_bot',
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)
pytgcalls = PyTgCalls(app)

async def run_command(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    logger.info(f'[{cmd!r} exited with {proc.returncode}]')
    if stdout:
        logger.info(f'[stdout]\n{stdout.decode()}')
    if stderr:
        logger.error(f'[stderr]\n{stderr.decode()}')

@app.on_message(filters.command('play'))
async def play_handler(client: Client, message: Message):
    chat_id = message.chat.id
    
    if os.path.exists('audio.raw'):
        os.remove('audio.raw')

    try:
        if len(message.command) < 2:
            await message.reply_text('Please specify a song name after /play')
            return

        await message.reply_text('**Downloading...**')
        song_name = message.text.split(None, 1)[1]

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'downloaded_audio.%(ext)s',
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(song_name, download=True)
            downloaded_file = ydl.prepare_filename(info)
            title = info.get('title', 'Unknown Title')

        await message.reply_text('**Converting...**')
        await run_command(
            f'ffmpeg -i "{downloaded_file}" -f s16le -ac 2 -ar 48000 -acodec pcm_s16le audio.raw'
        )
        
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)

        await message.reply_text(f'▶️ **Now playing:** {title}')
        await pytgcalls.join_group_call(
            chat_id,
            AudioPiped('audio.raw'),
        )

    except Exception as e:
        await message.reply_text(f'An error occurred: {e}')
        logger.error(e, exc_info=True)

async def main():
    logger.info("Starting bot and call client...")
    await app.start()
    await pytgcalls.start()
    logger.info("Clients started. Idling...")
    await idle()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Bot stopped by user.')
