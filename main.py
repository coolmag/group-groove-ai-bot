import asyncio
import os
import logging

import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import Message

from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Config
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Clients
app = Client(
    'my_bot',
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)
pytgcalls = PyTgCalls(app)

@app.on_message(filters.command('play'))
async def play_handler(client: Client, message: Message):
    try:
        if len(message.command) < 2:
            await message.reply_text('Please specify a song name after /play')
            return

        await message.reply_text('Searching...')
        song_name = message.text.split(None, 1)[1]
        chat_id = message.chat.id

        # Get audio stream URL from YouTube
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'default_search': 'ytsearch',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(song_name, download=False)['entries'][0]
            audio_url = info['url']
            title = info['title']

        # Join call and stream
        await pytgcalls.join_group_call(
            chat_id,
            AudioPiped(audio_url),
        )
        await message.reply_text(f'▶️ Now playing: {title}')

    except Exception as e:
        await message.reply_text(f'An error occurred: {e}')
        logger.error(e)

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