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

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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
            return ydl.extract_info(query, download)
    return await loop.run_in_executor(None, _exec)

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
        await stop_event.wait()
    finally:
        logger.info("Shutting down web server...")
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
            'preferredquality': '192',
        }],
        'outtmpl': '%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'nocheckcertificate': True,
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
                    await asyncio.wait_for(stop_event.wait(), timeout=track.get('duration', 300) + 5)
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
    stop_event = asyncio.Event()
    application.bot_data['stop_event'] = stop_event
    application.bot_data['radio_task'] = asyncio.create_task(radio_loop(application, stop_event))
    application.bot_data['web_task'] = asyncio.create_task(web_server(stop_event))

def setup_signal_handlers(application: Application):
    loop = asyncio.get_event_loop()
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
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("p", play_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("ron", radio_on_command))
    app.add_handler(CommandHandler("roff", radio_off_command))
    logger.info("Запуск бота...")
    setup_signal_handlers(app)
    app.run_polling()

if __name__ == "__main__":
    main()
