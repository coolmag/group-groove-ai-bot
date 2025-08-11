     1 import asyncio
     2 import os
     3 import logging
     4 import subprocess
     5 from pytgcalls.types import AudioPiped
     6
     7 import yt_dlp
     8 from pyrogram import Client, filters, idle
     9 from pyrogram.types import Message
    10
    11 from pytgcalls import PyTgCalls
    12
    13 # Logging
    14 logging.basicConfig(
    15     level=logging.INFO,
    16     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    17 )
    18 logger = logging.getLogger(__name__)
    19
    20 # Config
    21 API_ID = int(os.getenv('API_ID', 0))
    22 API_HASH = os.getenv('API_HASH')
    23 BOT_TOKEN = os.getenv('BOT_TOKEN')
    24
    25 # Clients
    26 app = Client(
    27     'my_bot',
    28     api_id=API_ID,
    29     api_hash=API_HASH,
    30     bot_token=BOT_TOKEN,
    31 )
    32 pytgcalls = PyTgCalls(app)
    33
    34 # --- Helper function to run shell commands ---
    35 async def run_command(cmd):
    36     proc = await asyncio.create_subprocess_shell(
    37         cmd,
    38         stdout=asyncio.subprocess.PIPE,
    39         stderr=asyncio.subprocess.PIPE,
    40     )
    41     stdout, stderr = await proc.communicate()
    42     logger.info(f'[{cmd!r} exited with {proc.returncode}]')
    43     if stdout:
    44         logger.info(f'[stdout]\n{stdout.decode()}')
    45     if stderr:
    46         logger.error(f'[stderr]\n{stderr.decode()}')
    47
    48
    49 @app.on_message(filters.command('play'))
    50 async def play_handler(client: Client, message: Message):
    51     chat_id = message.chat.id
    52
    53     # Cleanup previous files
    54     if os.path.exists('audio.raw'):
    55         os.remove('audio.raw')
    56
    57     try:
    58         if len(message.command) < 2:
    59             await message.reply_text('Please specify a song name after /play')
    60             return
    61
    62         await message.reply_text('**Downloading...**')
    63         song_name = message.text.split(None, 1)[1]
    64
    65         # Download with yt-dlp
    66         ydl_opts = {
    67             'format': 'bestaudio/best',
    68             'outtmpl': 'downloaded_audio.%(ext)s',
    69             'noplaylist': True,
    70         }
    71         with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    72             info = ydl.extract_info(song_name, download=True)
    73             downloaded_file = ydl.prepare_filename(info)
    74             title = info.get('title', 'Unknown Title')
    75
    76         # Convert to raw format with FFmpeg
    77         await message.reply_text('**Converting...**')
    78         await run_command(
    79             f'ffmpeg -i "{downloaded_file}" -f s16le -ac 2 -ar 48000 -acodec pcm_s16le
       audio.raw'
    80         )
    81
    82         # Clean up downloaded file
    83         if os.path.exists(downloaded_file):
    84             os.remove(downloaded_file)
    85
    86         # Join call and stream
    87         await message.reply_text(f'▶️ **Now playing:** {title}')
    88         await pytgcalls.join_group_call(
    89             chat_id,
    90             AudioPiped('audio.raw'),
    91         )
    92
    93     except Exception as e:
    94         await message.reply_text(f'An error occurred: {e}')
    95         logger.error(e, exc_info=True)
    96
    97
    98 async def main():
    99     logger.info("Starting bot and call client...")
   100     await app.start()
   101     await pytgcalls.start()
   102     logger.info("Clients started. Idling...")
   103     await idle()
   104
   105 if __name__ == '__main__':
   106     try:
   107         asyncio.run(main())
   108     except KeyboardInterrupt:
   109         logger.info('Bot stopped by user.')
