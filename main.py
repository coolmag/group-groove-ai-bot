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
import tempfile
import time

load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s - %(pathname)s:%(lineno)d",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables.")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
    RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID"))
except (TypeError, ValueError):
    raise ValueError("ADMIN_ID or RADIO_CHAT_ID are not set or invalid.")

# Log env vars for debugging
logger.info(f"BOT_TOKEN: {BOT_TOKEN[:5]}..., ADMIN_ID: {ADMIN_ID}, RADIO_CHAT_ID: {RADIO_CHAT_ID}, PORT: {os.environ.get('PORT')}")

# Use temporary directory for files to handle ephemeral storage on Render
TEMP_DIR = tempfile.gettempdir()
CONFIG_FILE = os.path.join(TEMP_DIR, "radio_config.json")
logger.info(f"Using config file: {CONFIG_FILE}")

RADIO_CACHE = {"genre": None, "tracks": [], "last_update": 0}

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

async def run_yt_dlp(opts, query, download=False):
    loop = asyncio.get_running_loop()
    def _exec():
        with yt_dlp.YoutubeDL(opts) as ydl:
            for attempt in range(3):
                try:
                    return ydl.extract_info(query, download=download)
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning(f"yt_dlp attempt {attempt+1} failed: {e}")
                    time.sleep(2 ** attempt)
    return await loop.run_in_executor(None, _exec)

async def web_server(stop_event: asyncio.Event, application: Application):
    routes = web.RouteTableDef()
    @routes.get('/')
    async def hello(_):
        return web.Response(text="I am alive.")
    
    @routes.post(f'/{BOT_TOKEN}')
    async def webhook(request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return web.Response(status=200)
        except Exception as e:
            logger.error(f"Webhook error: {e}", exc_info=True)
            return web.Response(status=500)
    
    app = web.Application()
    app.add_routes(routes)
    try:
        port = int(os.environ["PORT"])
    except KeyError:
        logger.warning("PORT not set, skipping web server.")
        return
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")
    
    # Set webhook if on Render
    try:
        hostname = os.environ['RENDER_EXTERNAL_HOSTNAME']
        webhook_url = f"https://{hostname}/{BOT_TOKEN}"
        await application.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
    except KeyError:
        logger.warning("RENDER_EXTERNAL_HOSTNAME not set, skipping set_webhook.")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
    
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down web server...")
        try:
            await application.bot.delete_webhook()
            logger.info("Webhook deleted.")
        except Exception as e:
            logger.error(f"Error deleting webhook: {e}")
        await runner.cleanup()

async def start_command(update: Update, _):
    text = """
Привет! Я радио-бот с поиском треков.

Команды:
/p <название> - Поиск и выбор трека для отправки.
/id - Получить ID этого чата.

Админ команды:
/ron <жанр> - Включить радио с жанром.
/roff - Выключить радио.
"""
    await update.message.reply_text(text)

async def id_command(update: Update, _):
    await update.message.reply_text(f"ID этого чата: {update.message.chat_id}")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите название песни после команды.")
        return
    query = " ".join(context.args)
    message = await update.message.reply_text(f'Ищу треки по запросу: "{query}"...')
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch5',
    }
    try:
        info = await run_yt_dlp(ydl_opts, query, download=False)
        entries = info.get('entries', [])
        if not entries:
            await message.edit_text("Ничего не найдено.")
            return
        context.user_data['search_results'] = {str(i): e for i, e in enumerate(entries[:5])}
        keyboard = [
            [InlineKeyboardButton(f"▶️ {entry.get('title', 'Unknown')}", callback_data=f"play_track:{i}")]
            for i, entry in context.user_data['search_results'].items()
        ]
        await message.edit_text("Выберите трек:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Ошибка поиска треков: {e}", exc_info=True)
        await message.edit_text("Произошла ошибка при поиске.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd, idx = query.data.split(":", 1)
    if cmd == "play_track":
        track_info = context.user_data.get('search_results', {}).get(idx)
        if not track_info:
            await query.edit_message_text("Информация об этом треке устарела. Пожалуйста, сделайте поиск заново.")
            return
        await query.edit_message_text("Готовлю аудио...")
        try:
            await download_and_send(track_info.get('id'), query.message.chat_id, context.bot)
            await query.edit_message_text("Трек отправлен!")
        except Exception as e:
            logger.error(f"Ошибка при отправке трека: {e}", exc_info=True)
            await query.edit_message_text(f"Не удалось отправить трек: {e}")

async def download_and_send(video_id, chat_id, bot):
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',  # Reduced for lower resource usage
        }],
        'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'nocheckcertificate': True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    filename = None
    for attempt in range(3):
        try:
            info = await run_yt_dlp(ydl_opts, url, download=True)
            title = info.get('title', 'Unknown')
            duration = info.get('duration', 0)
            filename = os.path.join(TEMP_DIR, f"{info.get('id')}.mp3")
            with open(filename, 'rb') as audio_file:
                await bot.send_audio(chat_id=chat_id, audio=audio_file, title=title, duration=duration)
            return
        except Exception as e:
            logger.error(f"Attempt {attempt+1} failed: {e}")
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
        finally:
            if filename and os.path.exists(filename):
                os.remove(filename)

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /ron <жанр>")
        return
    genre = " ".join(context.args)
    config = load_config()
    config['is_on'] = True
    config['genre'] = genre
    save_config(config)
    await update.message.reply_text(f"Радио включено. Жанр: {genre}")

async def radio_off_command(update: Update, _):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для этой команды.")
        return
    config = load_config()
    config['is_on'] = False
    save_config(config)
    await update.message.reply_text("Радио выключено.")

async def get_cached_tracks(genre):
    now = asyncio.get_event_loop().time()
    if RADIO_CACHE['genre'] == genre and now - RADIO_CACHE['last_update'] < 3600:
        return RADIO_CACHE['tracks']
    opts = {'format': 'bestaudio/best', 'noplaylist': False, 'quiet': True, 'default_search': 'ytsearch10'}
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
                        logger.warning("Нет треков для радио.")
                        await asyncio.sleep(60)
                        continue
                    track = random.choice(tracks)
                    logger.info(f"[Radio] Воспроизвожу: {track.get('title')}")
                    await download_and_send(track.get('id'), RADIO_CHAT_ID, application.bot)
                    await asyncio.wait_for(stop_event.wait(), timeout=track.get('duration', 300) + 30)  # Added buffer
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Ошибка в радио цикле: {e}", exc_info=True)
                    await asyncio.sleep(60)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    continue
    except asyncio.CancelledError:
        logger.info("Цикл радио отменён.")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка радио: {e}", exc_info=True)

async def post_init(application: Application):
    await application.initialize()
    stop_event = asyncio.Event()
    application.bot_data['stop_event'] = stop_event
    application.bot_data['radio_task'] = asyncio.create_task(radio_loop(application, stop_event))
    application.bot_data['web_task'] = asyncio.create_task(web_server(stop_event, application))

def setup_signal_handlers(application: Application):
    loop = asyncio.get_running_loop()
    def _stop():
        logger.info("Получен сигнал завершения, останавливаем...")
        stop_event = application.bot_data.get('stop_event')
        if stop_event:
            stop_event.set()
        radio_task = application.bot_data.get('radio_task')
        if radio_task:
            radio_task.cancel()
        web_task = application.bot_data.get('web_task')
        if web_task:
            web_task.cancel()
        tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

async def async_main():
    app = Application.builder().token(BOT_TOKEN).build()
    await post_init(app)
    setup_signal_handlers(app)
    radio_task = app.bot_data['radio_task']
    web_task = app.bot_data['web_task']
    await asyncio.gather(radio_task, web_task, return_exceptions=True)
    await app.shutdown()

def main():
    logger.info("Запуск бота...")
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
