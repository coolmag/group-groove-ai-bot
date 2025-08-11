import asyncio
import os
import logging
import subprocess
from pytgcalls.types import MediaStream
import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from aiohttp import web

# --- WEB SERVER FOR RENDER --- 
async def web_server():
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
        await asyncio.Event().wait() # Keep server running
    finally:
        await runner.cleanup()
# --- END WEB SERVER ---

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

def cleanup_files(*files):
    for file in files:
        if os.path.exists(file):
            try:
                os.remove(file)
                logger.info(f"Removed {file}")
            except Exception as e:
                logger.error(f"Error removing {file}: {e}")

async def run_command(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        logger.error(f'Command failed: {cmd!r}')
        if stderr:
            logger.error(f'[stderr]\n{stderr.decode()}')
        raise Exception(f"Command failed with return code {proc.returncode}")
    
    logger.info(f'[{cmd!r} executed successfully]')
    if stdout:
        logger.info(f'[stdout]\n{stdout.decode()}')

@app.on_message(filters.command('play'))
async def play_handler(client: Client, message: Message):
    chat_id = message.chat.id
    
    cleanup_files('audio.raw', 'downloaded_audio.*')

    try:
        if len(message.command) < 2:
            await message.reply_text('Please specify a song name after /play')
            return

        await message.reply_text('**Searching on SoundCloud...**')
        song_name = message.text.split(None, 1)[1]

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'downloaded_audio.%(ext)s',
            'noplaylist': True,
            'quiet': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"scsearch:{song_name}", download=True)['entries'][0]
                downloaded_file = ydl.prepare_filename(info)
                title = info.get('title', 'Unknown Title')
        except Exception as e:
            await message.reply_text("Error downloading the track from SoundCloud.")
            logger.error(f"Download error: {e}")
            return

        try:
            await message.reply_text('**Converting...**')
            await run_command(
                f'ffmpeg -i "{downloaded_file}" -f s16le -ac 2 -ar 48000 -acodec pcm_s16le audio.raw -y'
            )
        except Exception as e:
            await message.reply_text("Error converting the audio.")
            logger.error(f"Conversion error: {e}")
            cleanup_files(downloaded_file)
            return

        cleanup_files(downloaded_file)

        try:
            await message.reply_text(f'▶️ **Now playing:** {title}')
            await pytgcalls.join_group_call(
                chat_id,
                MediaStream('audio.raw'),
            )
        except Exception as e:
            await message.reply_text("Error joining the voice chat.")
            logger.error(f"Voice chat error: {e}")
            cleanup_files('audio.raw')

    except Exception as e:
        await message.reply_text(f'An error occurred: {str(e)}')
        logger.error(e, exc_info=True)
        cleanup_files('audio.raw', 'downloaded_audio.*')

@app.on_message(filters.command('stop'))
async def stop_handler(client: Client, message: Message):
    try:
        await pytgcalls.leave_group_call(message.chat.id)
        await message.reply_text("⏹ **Playback stopped**")
        cleanup_files('audio.raw', 'downloaded_audio.*')
    except Exception as e:
        await message.reply_text(f"Error stopping playback: {e}")
        logger.error(e, exc_info=True)

async def bot_main():
    logger.info("Starting bot client...")
    await app.start()
    await pytgcalls.start()
    logger.info("Clients started. Idling...")
    await idle()
    logger.info("Stopping clients...")
    await app.stop()
    cleanup_files('audio.raw', 'downloaded_audio.*')

if __name__ == '__main__':
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.gather(
            bot_main(),
            web_server(),
        ))
    except KeyboardInterrupt:
        logger.info('Bot stopped by user.')
    finally:
        cleanup_files('audio.raw', 'downloaded_audio.*')
