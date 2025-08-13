import logging
import os
import asyncio
import json
import random
import re
import yt_dlp
import uuid
from types import SimpleNamespace
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Message, Poll
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, PollHandler
from dotenv import load_dotenv
from collections import deque

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

# --- Helper Functions ---
def format_duration(seconds):
    if not seconds or seconds == 0:
        return "--:--"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

def has_ukrainian_chars(text):
    return any(char in text for char in 'іІїЇєЄґҐ')

def parse_genre_query(genre_string: str) -> str:
    match = re.search(r'(70|80|90|2000|2010)-х$', genre_string)
    if match:
        decade_part = match.group(1)
        core_genre = genre_string[:match.start()].strip()
        modifier = f"{decade_part}s" if decade_part in ['70', '80', '90'] else decade_part
        return f"{core_genre} {modifier}"
    return genre_string

# --- Config & FS Management ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {}
    config.setdefault('is_on', False)
    config.setdefault('genre', 'lo-fi hip hop')
    config.setdefault('radio_playlist', [])
    config.setdefault('played_radio_urls', [])
    config.setdefault('radio_message_ids', [])
    config.setdefault('voting_interval_seconds', 3600)
    config.setdefault('track_interval_seconds', 120)
    config.setdefault('message_cleanup_limit', 30)
    config.setdefault('active_poll', None)
    return config

def save_config(config):
    if isinstance(config.get('radio_message_ids'), deque):
        config['radio_message_ids'] = list(config['radio_message_ids'])
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def ensure_download_dir():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

# --- Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я музыкальный бот. 🎵\nИспользуй /play для поиска или /ron для радио.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🎧 **Команды бота**\n\n"
        "`/play <название>` - Поиск трека.\n"
        "`/id` - ID чата.\n"
        "**Админ-команды:**\n"
        "`/ron <жанр>` - Включить радио.\n"
        "`/rof` - Выключить радио.\n"
        "`/votestart` - Запустить голосование."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID этого чата: `{update.message.chat_id}`", parse_mode='Markdown')

async def get_paginated_keyboard(search_id: str, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    page_size = 5
    results = context.bot_data.get('paginated_searches', {}).get(search_id, [])
    if not results:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Ошибка: поиск не найден.", callback_data="noop")]])

    start_index = page * page_size
    keyboard = []
    for entry in results[start_index : start_index + page_size]:
        title = entry.get('title', 'Unknown')
        duration = format_duration(entry.get('duration'))
        cache_key = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('track_urls', {})[cache_key] = entry.get('url')
        keyboard.append([InlineKeyboardButton(f"▶️ {title} ({duration})", callback_data=f"play_track:{cache_key}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{search_id}:{page-1}"))
    if (page + 1) * page_size < len(results):
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page:{search_id}:{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    return InlineKeyboardMarkup(keyboard)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите название песни: `/play <название>`")
        return

    query = " ".join(context.args)
    message = await update.message.reply_text(f'Ищу "{query}"...')
    
    ydl_opts = {'format': 'bestaudio', 'noplaylist': True, 'quiet': True, 'default_search': 'scsearch30', 'extract_flat': 'in_playlist'}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if not info.get('entries'):
            await message.edit_text("Треки не найдены.")
            return

        search_id = uuid.uuid4().hex[:10]
        context.bot_data.setdefault('paginated_searches', {})[search_id] = info['entries']
        reply_markup = await get_paginated_keyboard(search_id, context)
        await message.edit_text(f'Найдено: {len(info["entries"])}. Выберите трек:', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in /play: {e}")
        await message.edit_text("Ошибка поиска.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    command, data = query.data.split(":", 1)

    if command == "play_track":
        track_url = context.bot_data.get('track_urls', {}).get(data)
        if not track_url:
            await query.edit_message_text("Ошибка: трек устарел.")
            return
        await query.edit_message_text("Обрабатываю...")
        try:
            track_info = await download_track(track_url)
            if track_info:
                await send_track(track_info, query.message.chat_id, context.bot)
                await query.edit_message_text("Трек отправлен!")
                if os.path.exists(track_info['filepath']): os.remove(track_info['filepath'])
            else:
                await query.edit_message_text("Не удалось скачать трек.")
        except Exception as e:
            await query.edit_message_text(f"Ошибка: {e}")
    elif command == "page":
        search_id, page_num_str = data.split(":")
        page = int(page_num_str)
        reply_markup = await get_paginated_keyboard(search_id, context, page)
        await query.edit_message_text('Выберите трек:', reply_markup=reply_markup)

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: await update.message.reply_text("Укажите жанр: `/ron <жанр>`"); return
    
    genre = " ".join(context.args)
    config = load_config()
    config.update({'is_on': True, 'genre': genre, 'radio_playlist': [], 'played_radio_urls': []})
    context.bot_data.update({'radio_playlist': deque(), 'played_radio_urls': []})
    save_config(config)
    await update.message.reply_text(f"Радио включено. Жанр: {genre}.")

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    config = load_config()
    config['is_on'] = False
    save_config(config)
    await update.message.reply_text("Радио выключено.")

async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    config = load_config()
    if not config.get('is_on'): await update.message.reply_text("Радио выключено."); return
    if config.get('active_poll'): await update.message.reply_text("Голосование уже идет."); return
    
    if await _create_and_send_poll(context.application):
        await update.message.reply_text("Голосование запущено.")
    else:
        await update.message.reply_text("Ошибка запуска голосования.")

# --- Music & Radio Logic ---
async def download_track(url: str):
    ensure_download_dir()
    out_template = os.path.join(DOWNLOAD_DIR, f'{uuid.uuid4()}.%(ext)s')
    ydl_opts = {
        'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'outtmpl': out_template, 'noplaylist': True, 'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
        return {'filepath': filename, 'title': info.get('title', 'Unknown'), 'duration': info.get('duration', 0)}

async def send_track(track_info: dict, chat_id: int, bot):
    try:
        with open(track_info['filepath'], 'rb') as audio_file:
            return await bot.send_audio(chat_id=chat_id, audio=audio_file, title=track_info['title'], duration=track_info['duration'])
    except Exception as e:
        logger.error(f"Failed to send track {track_info.get('filepath')}: {e}")
        return None

async def clear_old_tracks(context: ContextTypes.DEFAULT_TYPE):
    for _ in range(10):
        if context.bot_data['radio_message_ids']:
            chat_id, msg_id = context.bot_data['radio_message_ids'].popleft()
            try: await context.bot.delete_message(chat_id, msg_id)
            except Exception as e: logger.warning(f"Failed to delete msg {msg_id}: {e}")

async def radio_loop(application: Application):
    bot_data = application.bot_data
    while True:
        await asyncio.sleep(5)
        config = load_config()
        if not config.get('is_on'): continue

        if not bot_data.get('radio_playlist'):
            logger.info("Refilling radio playlist...")
            raw_genre = config.get('genre', 'lo-fi hip hop')
            search_query = parse_genre_query(raw_genre)
            logger.info(f"Original: '{raw_genre}', Parsed: '{search_query}'")
            ydl_opts = {'format': 'bestaudio', 'noplaylist': True, 'quiet': True, 'default_search': 'scsearch50', 'extract_flat': 'in_playlist'}
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_query, download=False)
                if info and info.get('entries'):
                    played = set(bot_data.get('played_radio_urls', []))
                    suitable = [
                        t['url'] for t in info['entries'] 
                        if t and 60 < t.get('duration', 0) < 900 
                        and t.get('url') not in played
                        and not has_ukrainian_chars(t.get('title', ''))
                    ]
                    random.shuffle(suitable)
                    bot_data['radio_playlist'] = deque(suitable)
            except Exception as e:
                logger.error(f"Playlist refill error: {e}")
                await asyncio.sleep(60)
                continue
        
        if not bot_data.get('radio_playlist'):
            await asyncio.sleep(60)
            continue

        track_url = bot_data['radio_playlist'].popleft()
        try:
            track_info = await download_track(track_url)
            sent_msg = await send_track(track_info, RADIO_CHAT_ID, application.bot)
            if sent_msg:
                bot_data.setdefault('radio_message_ids', deque()).append((sent_msg.chat_id, sent_msg.message_id))
                bot_data.setdefault('played_radio_urls', []).append(track_url)
                if len(bot_data['played_radio_urls']) > 100: bot_data['played_radio_urls'].pop(0)
                if len(bot_data['radio_message_ids']) >= config.get('message_cleanup_limit', 30): await clear_old_tracks(application)
                
                config['radio_playlist'] = list(bot_data['radio_playlist'])
                config['played_radio_urls'] = bot_data['played_radio_urls']
                config['radio_message_ids'] = list(bot_data['radio_message_ids'])
                save_config(config)

        except Exception as e:
            logger.error(f"Radio loop track error: {e}")
        finally:
            if 'track_info' in locals() and os.path.exists(track_info['filepath']): os.remove(track_info['filepath'])
        
        await asyncio.sleep(config.get('track_interval_seconds', 120))

# --- Voting Logic ---
# The voting system uses a hybrid approach:
# 1. A self-managed timer (`schedule_poll_processing`) is used to trigger the result processing, as relying on an update from Telegram was unreliable.
# 2. A passive `PollHandler` (`receive_poll_update`) listens for user votes to keep a persisted, up-to-date version of the poll object in the config file.
# 3. When the timer is up, the scheduler reads the final poll state from the config and processes it.

async def hourly_voting_loop(application: Application):
    while True:
        config = load_config()
        await asyncio.sleep(config.get('voting_interval_seconds', 3600))
        if config.get('is_on') and not config.get('active_poll'):
            await _create_and_send_poll(application)

async def _create_and_send_poll(application: Application) -> bool:
    config = load_config()
    try:
        votable_genres = config.get("votable_genres", [])
        if len(votable_genres) < 10: return False

        # --- Poll Option Generation ---
        # To create variety, we generate a mix of options:
        # 1. 5 options of "Genre + Decade" (e.g., "Rock 80-х")
        # 2. 4 unique random genres.
        # 3. "Pop" is always included as a default.
        decades = ["70-х", "80-х", "90-х", "2000-х", "2010-х"]
        special = {f"{random.choice(votable_genres)} {random.choice(decades)}" for _ in range(5)}
        regular_pool = [g for g in votable_genres if g not in {s.split(' ')[0] for s in special} and g.lower() != 'pop']
        num_to_sample = min(4, len(regular_pool))
        regular = set(random.sample(regular_pool, k=num_to_sample))
        
        options = list(special | regular)
        while len(options) < 9: # Ensure we have 9 diverse options before adding Pop
            chosen = random.choice(votable_genres)
            if chosen not in options: options.append(chosen)
        options.append("Pop")
        random.shuffle(options)
        
        poll_duration = 60
        message = await application.bot.send_poll(RADIO_CHAT_ID, "Выбираем жанр на следующий час!", options[:10], is_anonymous=False, open_period=poll_duration)
        
        config['active_poll'] = message.poll.to_dict()
        save_config(config)
        logger.info(f"Poll {message.poll.id} sent, processing in {poll_duration}s.")
        asyncio.create_task(schedule_poll_processing(application, message.poll.id, poll_duration))
        return True
    except Exception as e:
        logger.error(f"Create poll error: {e}")
        return False

async def schedule_poll_processing(application: Application, poll_id: str, delay: int):
    await asyncio.sleep(delay + 2)
    logger.info(f"Processing poll {poll_id}...")
    config = load_config()
    active_poll_dict = config.get('active_poll')

    if not active_poll_dict or active_poll_dict['id'] != poll_id: return

    mock_options = [SimpleNamespace(text=o.get('text'), voter_count=o.get('voter_count')) for o in active_poll_dict.get('options', [])]
    mock_poll = SimpleNamespace(id=active_poll_dict.get('id'), options=mock_options)
    await process_poll_results(mock_poll, application)

async def receive_poll_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    active_poll_dict = config.get('active_poll')
    if active_poll_dict and active_poll_dict['id'] == update.poll.id:
        config['active_poll'] = update.poll.to_dict()
        save_config(config)
        logger.info(f"Updated state for poll {update.poll.id}.")

async def process_poll_results(poll, application: Application):
    config = load_config()
    config['active_poll'] = None
    if not config.get('is_on'): save_config(config); return

    winning_options = []
    max_votes = 0
    for option in poll.options:
        if option.voter_count > max_votes:
            max_votes = option.voter_count
            winning_options = [option.text]
        elif option.voter_count == max_votes and max_votes > 0:
            winning_options.append(option.text)

    final_winner = "Pop" if not winning_options else random.choice(winning_options)
    logger.info(f"Winner: '{final_winner}'.")
    config['genre'] = final_winner
    config['radio_playlist'] = []
    application.bot_data['radio_playlist'] = deque()
    save_config(config)
    await application.bot.send_message(RADIO_CHAT_ID, f"Голосование завершено! Играет: **{final_winner}**", parse_mode='Markdown')

# --- Application Setup ---
async def post_init(application: Application) -> None:
    config = load_config()
    bot_data = application.bot_data
    bot_data['radio_playlist'] = deque(config.get('radio_playlist', []))
    bot_data['played_radio_urls'] = config.get('played_radio_urls', [])
    bot_data['radio_message_ids'] = deque(config.get('radio_message_ids', []))
    
    if config.get('active_poll'):
        logger.warning("Found active poll from previous session. Cannot restore timer.")

    await application.bot.set_my_commands([
        BotCommand("play", "/p <название>"),
        BotCommand("ron", "/ron <жанр>"),
        BotCommand("rof", "Выключить радио"),
        BotCommand("votestart", "Запустить голосование"),
        BotCommand("id", "Показать ID чата"),
        BotCommand("help", "Помощь"),
    ])
    asyncio.create_task(radio_loop(application))
    asyncio.create_task(hourly_voting_loop(application))

def main() -> None:
    if not BOT_TOKEN: logger.critical("FATAL: BOT_TOKEN not found."); return
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    handlers = [
        CommandHandler("start", start_command),
        CommandHandler(["help", "h"], help_command),
        CommandHandler("id", id_command),
        CommandHandler(["play", "p"], play_command),
        CommandHandler(["ron"], radio_on_command),
        CommandHandler(["rof"], radio_off_command),
        CommandHandler("votestart", start_vote_command),
        CallbackQueryHandler(button_callback),
        PollHandler(receive_poll_update)
    ]
    application.add_handlers(handlers)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    ensure_download_dir()
    main()