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

# --- Global Task References ---
radio_task = None
voting_task = None

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = "radio_config.json"
DOWNLOAD_DIR = "downloads"

# --- Genre Definitions ---
GENRE_KEYWORDS = {
    "electronic": ["electronic", "synth", "synthwave", "synth pop", "new wave", "edm", "house", "techno", "trance", "dance"],
    "rock": ["rock", "punk", "hard rock", "metal", "grunge", "garage", "alternative"],
    "pop": ["pop", "dance pop", "synthpop", "electropop"],
    "hip": ["hip hop", "rap", "trap", "r&b", "hiphop"],
    "jazz": ["jazz", "swing", "smooth jazz", "fusion"],
    "blues": ["blues", "rhythm and blues", "r&b"],
    "classical": ["classical", "orchestra", "symphony", "piano", "violin"],
    "reggae": ["reggae", "ska", "dub"],
    "country": ["country", "bluegrass", "folk"],
    "metal": ["metal", "heavy metal", "death metal", "black metal", "thrash"],
    "lo-fi": ["lo-fi", "lofi", "chillhop", "chill"],
    "disco": ["disco", "funk", "boogie"],
}

def format_duration(seconds):
    if not seconds or seconds == 0: return "--:--"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

def has_ukrainian_chars(text: str) -> bool:
    return bool(re.search(r"[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]", text))

def is_genre_match(track: dict, genre: str) -> bool:
    title = track.get('title', '').lower()
    for key, keywords in GENRE_KEYWORDS.items():
        if key in genre.lower():
            return any(kw in title for kw in keywords)
    return True

def build_search_queries(genre: str):
    return [f"{genre} music", f"{genre} best tracks", f"{genre} playlist"]

def escape_markdown(text: str) -> str:
    return re.sub(r'([_*[\\\\]()~`>#+\-=|}{}.!])', r'\\\\\1', text)

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
    config.setdefault('poll_duration_seconds', 60)
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
    help_text = """
*Команды бота*

/play <название> - Поиск трека
/id - ID чата

*Админ-команды:*
/ron <жанр> - Включить радио
/rof - Выключить радио
/votestart - Запустить голосование
"""
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
        print(f"Error in /play: {e}")
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
    elif command == "toggle_radio":
        config = load_config()
        if config.get('is_on'):
            await radio_off_command(update, context)
        else:
            await radio_on_command(update, context)
    elif command == "skip_track":
        await skip_track(update, context)
    elif command == "start_vote":
        await start_vote_command(update, context)
    elif command == "status_refresh":
        await send_status_panel(context.application, query.message.chat_id, query.message.message_id)

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global radio_task, voting_task
    if update.effective_user.id != ADMIN_ID: return
    
    genre = " ".join(context.args) if context.args else load_config().get('genre', 'lo-fi hip hop')
    config = load_config()
    config.update({'is_on': True, 'genre': genre, 'radio_playlist': [], 'played_radio_urls': []})
    context.bot_data.update({'radio_playlist': deque(), 'played_radio_urls': []})
    save_config(config)
    
    if not radio_task or radio_task.done():
        radio_task = asyncio.create_task(radio_loop(context.application))
    if not voting_task or voting_task.done():
        voting_task = asyncio.create_task(hourly_voting_loop(context.application))
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(f"Радио включено. Жанр: {escape_markdown(genre)}.", parse_mode='MarkdownV2')
    await send_status_panel(context.application, update.effective_chat.id, config.get('status_message_id'))

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global radio_task, voting_task
    if update.effective_user.id != ADMIN_ID: return
    config = load_config()
    config['is_on'] = False
    config['now_playing'] = None
    save_config(config)
    
    if radio_task and not radio_task.done():
        radio_task.cancel()
    if voting_task and not voting_task.done():
        voting_task.cancel()
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("Радио выключено.")
    await send_status_panel(context.application, update.effective_chat.id, config.get('status_message_id'))

async def start_vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    config = load_config()
    if not config.get('is_on'): await update.message.reply_text("Радио выключено."); return
    if config.get('active_poll'): await update.message.reply_text("Голосование уже идет."); return
    
    if await _create_and_send_poll(context.application):
        await update.message.reply_text("Голосование запущено.")
    else:
        await update.message.reply_text("Ошибка запуска голосования.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await send_status_panel(context.application, update.effective_chat.id)

async def skip_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: 
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer("Эта кнопка не для вас.", show_alert=True)
        return

    if radio_task and not radio_task.done():
        radio_task.cancel()
        radio_task = asyncio.create_task(radio_loop(context.application))

    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.answer("Пропускаем трек...")

async def send_status_panel(application: Application, chat_id: int, message_id: int = None):
    config = load_config()
    now_playing = config.get('now_playing')
    is_on = config.get('is_on', False)

    status_icon = "🟢" if is_on else "🔴"
    status_text = "В ЭФИРЕ" if is_on else "ВЫКЛЮЧЕНО"
    genre = escape_markdown(config.get('genre', '-'))
    
    text = f"""
*Интерактивная Панель Управления*
────────────────────────
*Статус:* {status_icon} *{escape_markdown(status_text)}*
*Жанр:* `{genre}`
"""

    if is_on and now_playing:
        title = escape_markdown(now_playing.get('title', 'Неизвестный трек'))
        duration = escape_markdown(format_duration(now_playing.get('duration', 0)))
        text += f"""
────────────────────────
*Сейчас играет:*
`{title}`
`{duration}`
────────────────────────
"""
    else:
        text += f"""
*Сейчас играет:* — тишина...
────────────────────────
"""

    keyboard = []
    if is_on:
        keyboard.append([
            InlineKeyboardButton("⏭ Пропустить", callback_data="skip_track:0"),
            InlineKeyboardButton("⏹️ Стоп", callback_data="toggle_radio:0")
        ])
        keyboard.append([InlineKeyboardButton("🗳️ Голосование", callback_data="start_vote:0"),
        ])
    else:
        keyboard.append([InlineKeyboardButton("▶️ Запустить Радио", callback_data="toggle_radio:0")])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="status_refresh:0")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if message_id:
            await application.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')
        else:
            sent_message = await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='MarkdownV2')
            config['status_message_id'] = sent_message.message_id
            save_config(config)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Error sending status panel: {e}")

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
        print(f"Failed to send track {track_info.get('filepath')}: {e}")
        return None

async def clear_old_tracks(app: Application):
    radio_msgs = app.bot_data.get('radio_message_ids')
    if not isinstance(radio_msgs, deque) or not radio_msgs:
        return
    for _ in range(10):
        if not radio_msgs:
            break
        chat_id, msg_id = radio_msgs.popleft()
        try:
            await app.bot.delete_message(chat_id, msg_id)
        except Exception as e:
            print(f"Failed to delete msg {msg_id}: {e}")

async def refill_playlist(application: Application):
    bot_data = application.bot_data
    print("Refilling radio playlist...")
    config = load_config()
    raw_genre = config.get('genre', 'lo-fi hip hop')
    search_queries = build_search_queries(raw_genre)
    played = set(bot_data.get('played_radio_urls', []))
    suitable_tracks = []

    for query in search_queries:
        print(f"Searching: {query}")
        ydl_opts = {
            'format': 'bestaudio',
            'noplaylist': True,
            'quiet': True,
            'default_search': 'scsearch50',
            'extract_flat': 'in_playlist'
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
            if not info or not info.get('entries'):
                continue

            for t in info['entries']:
                if not t:
                    continue
                if not (60 < t.get('duration', 0) < 900):
                    continue
                if t.get('url') in played:
                    continue
                if has_ukrainian_chars(t.get('title', '')):
                    continue
                if not is_genre_match(t, raw_genre):
                    continue
                suitable_tracks.append(t)
        except Exception as e:
            print(f"Search error for '{query}': {e}")

    unique_tracks = {}
    for t in suitable_tracks:
        if t['url'] not in unique_tracks:
            unique_tracks[t['url']]=t
    suitable_tracks = list(unique_tracks.values())

    suitable_tracks.sort(
        key=lambda tr: (tr.get('play_count', 0) or 0) + (tr.get('like_count', 0) or 0),
        reverse=True
    )

    final_urls = [t['url'] for t in suitable_tracks[:50]]
    random.shuffle(final_urls)

    bot_data['radio_playlist'] = deque(final_urls)
    config['radio_playlist'] = final_urls
    save_config(config)

    print(f"Playlist refilled with {len(final_urls)} tracks.")

async def radio_loop(application: Application):
    bot_data = application.bot_data
    while True:
        try:
            await asyncio.sleep(5)
            config = load_config()
            if not config.get('is_on'): 
                await asyncio.sleep(30)
                continue

            if not bot_data.get('radio_playlist'):
                await refill_playlist(application)
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
                    if len(bot_data['played_radio_urls']) > 100:
                        bot_data['played_radio_urls'].pop(0)
                    if len(bot_data['radio_message_ids']) >= config.get('message_cleanup_limit', 30):
                        await clear_old_tracks(application)

                    config['radio_playlist'] = list(bot_data['radio_playlist'])
                    config['played_radio_urls'] = bot_data['played_radio_urls']
                    config['radio_message_ids'] = list(bot_data['radio_message_ids'])
                    config['now_playing'] = {
                        'title': track_info.get('title', 'Unknown'),
                        'duration': track_info.get('duration', 0)
                    }
                    save_config(config)

            except Exception as e:
                print(f"Radio loop track error: {e}")
            finally:
                if 'track_info' in locals() and os.path.exists(track_info['filepath']): os.remove(track_info['filepath'])
            
            await asyncio.sleep(config.get('track_interval_seconds', 120))
        except asyncio.CancelledError:
            print("Radio loop cancelled.")
            break
        except Exception as e:
            print(f"FATAL error in radio_loop: {e}")
            await asyncio.sleep(60)

# --- Voting Logic ---
async def hourly_voting_loop(application: Application):
    while True:
        try:
            config = load_config()
            await asyncio.sleep(config.get('voting_interval_seconds', 3600))
            if config.get('is_on') and not config.get('active_poll'):
                await _create_and_send_poll(application)
        except asyncio.CancelledError:
            print("Voting loop cancelled.")
            break
        except Exception as e:
            print(f"Error in hourly_voting_loop: {e}")
            await asyncio.sleep(60)

async def _create_and_send_poll(application: Application) -> bool:
    config = load_config()
    try:
        votable_genres = config.get("votable_genres", [])
        if len(votable_genres) < 10: return False
        decades = ["70-х", "80-х", "90-х", "2000-х", "2010-х"]
        special = {f"{random.choice(votable_genres)} {random.choice(decades)}" for _ in range(5)}
        regular_pool = [g for g in votable_genres if g not in {s.split(' ')[0] for s in special} and g.lower() != 'pop']
        num_to_sample = min(4, len(regular_pool))
        regular = set(random.sample(regular_pool, k=num_to_sample))
        options = list(special | regular)
        while len(options) < 9: 
            chosen = random.choice(votable_genres)
            if chosen not in options: options.append(chosen)
        options.append("Pop")
        random.shuffle(options)
        
        poll_duration = config.get('poll_duration_seconds', 60)
        message = await application.bot.send_poll(RADIO_CHAT_ID, "Выбираем жанр на следующий час!", options[:10], is_anonymous=False, open_period=poll_duration)
        
        poll_data = message.poll.to_dict()
        poll_data['close_timestamp'] = datetime.now().timestamp() + poll_duration
        config['active_poll'] = poll_data
        save_config(config)

        print(f"Poll {message.poll.id} sent, processing in {poll_duration}s.")
        asyncio.create_task(schedule_poll_processing(application, message.poll.id, poll_duration))
        return True
    except Exception as e:
        print(f"Create poll error: {e}")
        return False

async def schedule_poll_processing(application: Application, poll_id: str, delay: int):
    await asyncio.sleep(delay + 2)
    print(f"Processing poll {poll_id}...")
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
        print(f"Updated state for poll {update.poll.id}.")

async def process_poll_results(poll, application: Application):
    global radio_task
    config = load_config()
    config['active_poll'] = None
    if not config.get('is_on'): 
        save_config(config)
        return

    winning_options = []
    max_votes = 0
    for option in poll.options:
        if option.voter_count > max_votes:
            max_votes = option.voter_count
            winning_options = [option.text]
        elif option.voter_count == max_votes and max_votes > 0:
            winning_options.append(option.text)

    final_winner = "Pop" if not winning_options else random.choice(winning_options)
    print(f"Winner: '{final_winner}'.")
    config['genre'] = final_winner
    config['radio_playlist'] = []
    if isinstance(application.bot_data.get('radio_playlist'), deque):
        application.bot_data['radio_playlist'].clear()
    else:
        application.bot_data['radio_playlist'] = deque()

    config['now_playing'] = None
    save_config(config)

    await application.bot.send_message(
        RADIO_CHAT_ID,
        f"Голосование завершено! Играет: **{escape_markdown(final_winner)}**",
        parse_mode='MarkdownV2'
    )

    asyncio.create_task(refill_playlist(application))

    if not radio_task or radio_task.done():
        radio_task = asyncio.create_task(radio_loop(application))

# --- Application Setup ---
async def post_init(application: Application) -> None:
    global radio_task, voting_task
    config = load_config()
    bot_data = application.bot_data
    bot_data['radio_playlist'] = deque(config.get('radio_playlist', []))
    bot_data['played_radio_urls'] = config.get('played_radio_urls', [])
    bot_data['radio_message_ids'] = deque(config.get('radio_message_ids', []))
    
    if config.get('is_on'):
        print("Radio was ON at startup. Starting background tasks.")
        radio_task = asyncio.create_task(radio_loop(application))
        voting_task = asyncio.create_task(hourly_voting_loop(application))
    
    active_poll = config.get('active_poll')
    if active_poll:
        close_timestamp = active_poll.get('close_timestamp')
        if close_timestamp:
            remaining_time = close_timestamp - datetime.now().timestamp()
            if remaining_time > 0:
                print(f"[Init] Found an active poll. Rescheduling processing in {remaining_time:.0f}s.")
                asyncio.create_task(schedule_poll_processing(application, active_poll['id'], remaining_time))

def main() -> None: 
    if not BOT_TOKEN: print("FATAL: BOT_TOKEN not found."); return
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    handlers = [
        CommandHandler("start", start_command),
        CommandHandler(["help", "h"], help_command),
        CommandHandler("id", id_command),
        CommandHandler(["play", "p"], play_command),
        CommandHandler(["ron"], radio_on_command),
        CommandHandler(["rof"], radio_off_command),
        CommandHandler("votestart", start_vote_command),
        CommandHandler("status", status_command),
        CommandHandler("skip", skip_track),
        CallbackQueryHandler(button_callback),
        PollHandler(receive_poll_update)
    ]
    application.add_handlers(handlers)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    ensure_download_dir()
    main()