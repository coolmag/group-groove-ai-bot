# -*- coding: utf-8 -*-import logging
import os
import asyncio
import json
import random
import shutil
from pathlib import Path
from typing import List, Optional
from collections import deque
from datetime import datetime
import re
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, PollAnswer
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollHandler,
    PollAnswerHandler,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError, BadRequest, RetryAfter
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_serializer, field_validator
from functools import wraps
from asyncio import Lock

# --- Константы ---
class Constants:
    VOTING_INTERVAL_SECONDS = 3600
    TRACK_INTERVAL_SECONDS = 60
    POLL_DURATION_SECONDS = 60
    POLL_CHECK_TIMEOUT = 10
    MAX_FILE_SIZE = 50_000_000
    MAX_DURATION = 300
    MIN_DURATION = 30
    PLAYED_URLS_MEMORY = 100
    DOWNLOAD_TIMEOUT = 30
    DEFAULT_SOURCE = "soundcloud"
    DEFAULT_GENRE = "pop"
    PAUSE_BETWEEN_TRACKS = 0
    STATUS_UPDATE_INTERVAL = 10
    STATUS_UPDATE_MIN_INTERVAL = 2
    RETRY_INTERVAL = 30
    SEARCH_LIMIT = 50
    MAX_RETRIES = 3

# --- Настройка ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] or []
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
CONFIG_FILE = Path("radio_config.json")
DOWNLOAD_DIR = Path("downloads")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")

# --- Модели данных ---
class NowPlaying(BaseModel):
    title: str
    duration: int
    url: str
    start_time: float = Field(default_factory=lambda: asyncio.get_event_loop().time())

class State(BaseModel):
    is_on: bool = False
    genre: str = "lo-fi hip hop"
    source: str = Constants.DEFAULT_SOURCE
    radio_playlist: deque[str] = Field(default_factory=deque)
    played_radio_urls: deque[str] = Field(default_factory=deque)
    active_poll_id: Optional[str] = None
    poll_message_id: Optional[int] = None
    poll_options: List[str] = Field(default_factory=list)
    poll_votes: List[int] = Field(default_factory=list)
    status_message_id: Optional[int] = None
    last_status_update: float = 0.0
    now_playing: Optional[NowPlaying] = None
    last_error: Optional[str] = None
    votable_genres: List[str] = Field(
        default_factory=lambda: sorted(list(set([
            "pop", "pop 80s", "pop 90s", "pop 2000s",
            "rock", "rock 60s", "rock 70s", "rock 80s", "rock 90s",
            "hip hop", "hip hop 90s", "hip hop 2000s",
            "electronic", "electronic 90s", "electronic 2000s",
            "classical", "classical 18th century", "classical 19th century",
            "jazz", "jazz 50s", "jazz 60s",
            "blues", "blues 50s", "blues 60s",
            "country", "country 80s", "country 90s",
            "metal", "metal 80s", "metal 90s",
            "reggae", "reggae 70s", "reggae 80s",
            "folk", "folk 60s", "folk 70s",
            "indie", "indie 90s", "indie 2000s",
            "rap", "rap 80s", "rap 90s", "rap 2000s",
            "r&b", "r&b 90s", "r&b 2000s",
            "soul", "soul 60s", "soul 70s",
            "funk", "funk 70s", "funk 80s",
            "disco", "disco 70s", "disco 80s"
        ])))
    )
    retry_count: int = 0

    @field_serializer('radio_playlist', 'played_radio_urls')
    def _serialize_deques(self, v: deque[str], _info):
        return list(v)

    @field_validator('radio_playlist', 'played_radio_urls', mode='before')
    @classmethod
    def _lists_to_deques(cls, v):
        return deque(v) if isinstance(v, list) else deque()

state_lock = Lock()
status_lock = Lock()

# --- Состояние ---
def load_state() -> State:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
                return State(**data)
        except Exception as e:
            logger.error(f"Не удалось загрузить конфиг: {e}")
            return State()
    logger.info("Конфигурационный файл не найден, используется состояние по умолчанию")
    return State()

async def save_state(state: State):
    try:
        CONFIG_FILE.write_text(state.model_dump_json(indent=4), encoding='utf-8')
        logger.debug("Состояние сохранено в файл конфигурации")
    except Exception as e:
        logger.error(f"Не удалось сохранить состояние: {e}")

# --- Утилиты ---
def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    s_int = int(seconds)
    return f"{s_int // 60:02d}:{s_int % 60:02d}"

def get_progress_bar(progress: float, width: int = 10) -> str:
    filled = int(width * progress)
    return "█" * filled + " " * (width - filled)

def escape_markdown_v2(text: str) -> str:
    if not isinstance(text, str) or not text:
        return ""
    special_chars = r'([_*[\\\]()~`>#+\-=|"{}.!])'
    return re.sub(special_chars, r'\\\1', text)

async def set_error(state: State, error: str):
    async with state_lock:
        state.last_error = error
        logger.error(f"Ошибка установлена: {error}")

# --- Администрирование ---
async def is_admin(user_id: int) -> bool:
    logger.debug(f"Проверка статуса администратора для user_id: {user_id}. Список администраторов: {ADMIN_IDS}")
    return user_id in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not await is_admin(user_id):
            await set_error(context.bot_data['state'], "Попытка несанкционированного доступа")
            await update.effective_message.reply_text("Эта команда только для администраторов.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Источники музыки ---
async def get_tracks(source: str, genre: str) -> List[dict]:
    logger.info(f"Поиск по жанру '{genre}' на {source}")
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': False,
        'extract_flat': 'in_playlist',
        'default_search': f"{'scsearch' if source == 'soundcloud' else 'ytsearch'}{Constants.SEARCH_LIMIT}:{genre}"
    }
    if source == 'youtube' and YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, genre, download=False)
        tracks = info.get("entries", [])
        logger.info(f"Найдено {len(tracks)} треков для жанра '{genre}' на {source}")
        return tracks
    except yt_dlp.YoutubeDLError as e:
        logger.error(f"Поиск на {source.title()} не удался для жанра '{genre}': {e}")
        return []

# --- Пополнение плейлиста ---
async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Пополняю плейлист из {state.source} для жанра: {state.genre}")

    async with state_lock:
        if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY * 0.5:
            state.played_radio_urls.clear()
            logger.debug("Очищен список воспроизведенных URL для управления памятью")

    original_genre, original_source = state.genre, state.source
    current_source = original_source

    for attempt in range(Constants.MAX_RETRIES * 2): # *2 для попытки на обоих источниках
        try:
            tracks = await get_tracks(current_source, state.genre)
            if not tracks:
                logger.warning(f"Треки не найдены на {current_source} для жанра '{state.genre}', попытка {attempt + 1}")
                current_source = "youtube" if current_source == "soundcloud" else "soundcloud"
                await asyncio.sleep(1) # Небольшая задержка перед следующей попыткой
                continue

            filtered_urls = [t["url"] for t in tracks if Constants.MIN_DURATION <= t.get("duration", 0) <= Constants.MAX_DURATION and t.get("url") not in state.played_radio_urls]

            if filtered_urls:
                random.shuffle(filtered_urls)
                async with state_lock:
                    state.radio_playlist.extend(filtered_urls)
                    state.retry_count = 0
                    state.genre = original_genre
                    state.source = original_source
                    await save_state(state)
                logger.info(f"Плейлист пополнен {len(filtered_urls)} треками")
                return
            else:
                logger.warning(f"Нет подходящих треков после фильтрации на {current_source}. Пробую следующий источник.")
                current_source = "youtube" if current_source == "soundcloud" else "soundcloud"

        except Exception as e:
            logger.error(f"Пополнение плейлиста не удалось, попытка {attempt + 1}: {e}")
            await set_error(state, f"Ошибка пополнения плейлиста: {e}")
            await asyncio.sleep(Constants.RETRY_INTERVAL)

    logger.error(f"Не удалось пополнить плейлист после всех попыток.")
    async with state_lock:
        state.source = Constants.DEFAULT_SOURCE
        state.genre = Constants.DEFAULT_GENRE
        await set_error(state, f"Не удалось найти треки. Переключено на {state.source}/{state.genre}.")
        await save_state(state)

# --- Скачивание и отправка ---
async def download_and_send(context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int):
    state: State = context.bot_data['state']
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        await set_error(state, "FFmpeg или ffprobe не установлен")
        await context.bot.send_message(chat_id, "⚠️ Ошибка: FFmpeg или ffprobe не установлен.")
        return

    DOWNLOAD_DIR.mkdir(exist_ok=True)

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': False,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'ffmpeg_location': shutil.which("ffmpeg"),
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES

    filepath = None
    try:
        logger.info(f"Скачиваю: {url}")
        async with asyncio.timeout(Constants.DOWNLOAD_TIMEOUT):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        
        filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
        if not filepath.exists() or filepath.stat().st_size == 0:
            raise ValueError("Файл не создан или пуст после конвертации в mp3")

        if filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            await set_error(state, "Трек превышает максимальный размер файла")
            await context.bot.send_message(chat_id, "⚠️ Трек слишком большой для отправки.")
            return

        title = info.get("title", "Неизвестно")
        performer = info.get("uploader", "Неизвестно")
        caption_text = f"🎵 **{escape_markdown_v2(title)}**\n👤 {escape_markdown_v2(performer)}"

        logger.info(f"Отправляю в чат {chat_id}: {title}")
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                chat_id, f,
                caption=caption_text,
                parse_mode="MarkdownV2",
                title=title,
                duration=int(info.get("duration", 0)),
                performer=performer
            )
        return info # Возвращаем информацию для NowPlaying

    except Exception as e:
        logger.error(f"Не удалось скачать/отправить трек {url}: {e}", exc_info=True)
        await set_error(state, f"Не удалось обработать трек: {e}")
        await context.bot.send_message(chat_id, f"⚠️ Не удалось обработать трек: {e}")
        return None
    finally:
        if filepath and filepath.exists():
            filepath.unlink(missing_ok=True)

# --- Радио-цикл ---
async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info("Запускаю радио-цикл")
    await update_status_panel(context, force=True)
    while True:
        try:
            if not state.is_on:
                logger.info("Радио выключено, сплю")
                await asyncio.sleep(10)
                continue
            
            url = None
            async with state_lock:
                if state.radio_playlist:
                    url = state.radio_playlist.popleft()
                    state.played_radio_urls.append(url)
                    if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                        state.played_radio_urls.popleft()
            
            if not url:
                logger.info("Плейлист пуст, пополняю")
                await refill_playlist(context)
                continue

            logger.info(f"Воспроизвожу трек: {url}")
            track_info = await download_and_send(context, url, RADIO_CHAT_ID)

            async with state_lock:
                if track_info:
                    state.now_playing = NowPlaying(
                        title=track_info.get("title", "Неизвестно"),
                        duration=int(track_info.get("duration", 0)),
                        url=url
                    )
                else:
                    state.now_playing = None # Очищаем, если скачивание не удалось
                await save_state(state)
            
            await update_status_panel(context, force=True)

            if state.now_playing:
                context.bot_data['skip_event'].clear()
                sleep_duration = state.now_playing.duration
                logger.debug(f"Жду {sleep_duration} секунд или события пропуска")
                try:
                    await asyncio.wait_for(context.bot_data['skip_event'].wait(), timeout=sleep_duration)
                except asyncio.TimeoutError:
                    pass # Трек завершился естественным образом

            await asyncio.sleep(Constants.PAUSE_BETWEEN_TRACKS)

        except asyncio.CancelledError:
            logger.info("Радио-цикл отменен")
            break
        except Exception as e:
            logger.error(f"Ошибка в радио-цикле: {e}", exc_info=True)
            await set_error(state, f"Ошибка в радио-цикле: {e}")
            await update_status_panel(context, force=True)
            await asyncio.sleep(5)

# --- Интерфейс ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    async with status_lock:
        state: State = context.bot_data['state']
        current_time = asyncio.get_event_loop().time()
        if not force and current_time - state.last_status_update < Constants.STATUS_UPDATE_MIN_INTERVAL:
            return

        lines = [
            "🎵 *Radio Groove AI* 🎵",
            f"**Статус**: {'🟢 Включено' if state.is_on else '🔴 Выключено'}",
            f"**Жанр**: {escape_markdown_v2(state.genre.title())}",
            f"**Источник**: {escape_markdown_v2(state.source.title())}"
        ]
        if state.now_playing and state.now_playing.duration > 0:
            elapsed = current_time - state.now_playing.start_time
            progress = min(elapsed / state.now_playing.duration, 1.0)
            lines.append(f"**Сейчас играет**: {escape_markdown_v2(state.now_playing.title)} \\({format_duration(state.now_playing.duration)}\\)")
            lines.append(f"**Прогресс**: {get_progress_bar(progress)} {int(progress * 100)}%")
        else:
            lines.append(f"**Сейчас играет**: {escape_markdown_v2('Ожидание трека...')}")
        if state.active_poll_id:
            lines.append(f"🗳 *Идет голосование* (~{Constants.POLL_DURATION_SECONDS}с осталось)")
        if state.last_error:
            lines.append(f"⚠️ **Последняя ошибка**: {escape_markdown_v2(state.last_error)}")
        lines.append("────────────────")
        text = "\n".join(lines)

        last_status_text = context.bot_data.get('last_status_text', '')
        if not force and text == last_status_text:
            return

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="radio:refresh"), InlineKeyboardButton("⏭ Пропустить" if state.is_on else "▶️ Включить", callback_data="radio:skip" if state.is_on else "radio:on")],
            [InlineKeyboardButton("🗳 Голосовать", callback_data="vote:start")] if state.is_on and not state.active_poll_id else [],
            [InlineKeyboardButton("⏹ Стоп Радио", callback_data="radio:off")] if state.is_on else [],
            [InlineKeyboardButton("🤖 Стоп Бот", callback_data="cmd:stopbot")] if await is_admin(context._user_id) else [],
            [InlineKeyboardButton("📋 Меню", callback_data="cmd:menu")]
        ]
        try:
            if state.status_message_id:
                await context.bot.edit_message_text(chat_id=RADIO_CHAT_ID, message_id=state.status_message_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
            else:
                msg = await context.bot.send_message(RADIO_CHAT_ID, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
                async with state_lock:
                    state.status_message_id = msg.message_id
            context.bot_data['last_status_text'] = text
            async with state_lock:
                state.last_status_update = current_time
                state.last_error = None
                await save_state(state)
        except (BadRequest, TelegramError) as e:
            if "Message is not modified" in str(e):
                logger.debug("Сообщение статуса не изменилось, игнорирую")
                return
            logger.error(f"Не удалось обновить панель статуса: {e}, текст: {repr(text)}")
            await set_error(state, f"Ошибка обновления статуса: {e}")
            if "Message to edit not found" in str(e):
                async with state_lock:
                    state.status_message_id = None

# --- Команды ---
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"Команда show_menu вызвана пользователем {update.effective_user.id}")
    state: State = context.bot_data['state']
    # Закомментировано для теста в личном чате. Раскомментируйте для работы только в группе.
    # if update.effective_chat.id != RADIO_CHAT_ID:
    #     await set_error(state, f"Команда отправлена в неправильный чат: {update.effective_chat.id}")
    #     await update.message.reply_text(f"⚠️ Эта команда работает только в чате с ID {RADIO_CHAT_ID}.")
    #     return

    text = [
        "🎵 *Groove AI Bot - Меню* 🎵",
        f"**Статус радио**: {'🟢 Включено' if state.is_on else '🔴 Выключено'}",
        f"**Текущий жанр**: {escape_markdown_v2(state.genre.title())}",
        f"**Сейчас играет**: {escape_markdown_v2(state.now_playing.title if state.now_playing else 'Ничего не играет')}",
        f"**Последняя ошибка**: {escape_markdown_v2(state.last_error or 'Нет')}",
        "",
        "📜 *Команды администратора:*",
        "▶️ /ron - Включить радио",
        "⏹ /rof - Выключить радио",
        "⏭ /skip - Пропустить трек",
        "🗳 /vote - Начать голосование",
        "🔧 /source <soundcloud|youtube> - Сменить источник",
        "🛑 /stop, /stopbot - Полностью остановить бота",
        "",
        "📜 *Команды для всех:*",
        "🎧 /play <название> - Найти и воспроизвести трек",
    ]
    await update.message.reply_text("\n".join(text), parse_mode="MarkdownV2")

async def toggle_radio(context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    async with state_lock:
        state: State = context.bot_data['state']
        if state.is_on == turn_on: return
        state.is_on = turn_on
        if not turn_on:
            state.radio_playlist.clear()
            state.now_playing = None
            task = context.bot_data.get('radio_loop_task')
            if task: task.cancel()
        else:
            context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
        await save_state(state)
    logger.info(f"Радио {'включено' if turn_on else 'выключено'}")
    if turn_on: await refill_playlist(context)

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    await toggle_radio(context, turn_on)
    await update.message.reply_text(f"Радио {'включено' if turn_on else 'выключено'}.")
    await update_status_panel(context, force=True)

@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛑 Останавливаю бота.")
    await context.application.stop_running()

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['skip_event'].set()
    await update.message.reply_text("Пропускаю трек... ⏭")

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_vote(context)

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in ["soundcloud", "youtube"]:
        await update.message.reply_text("Использование: /source <soundcloud|youtube>")
        return
    async with state_lock:
        state: State = context.bot_data['state']
        state.source = context.args[0]
        state.radio_playlist.clear()
        state.now_playing = None
        await save_state(state)
    await update.message.reply_text(f"Источник переключен на: {state.source.title()}")
    await refill_playlist(context)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите название песни.")
        return
    query = " ".join(context.args)
    message = await update.message.reply_text(f'🔍 Поиск "{query}"...')
    tracks = await get_tracks(context.bot_data['state'].source, query)
    if not tracks:
        await message.edit_text("Треки не найдены. 😔")
        return
    
    filtered_tracks = [t for t in tracks if Constants.MIN_DURATION <= t.get("duration", 0) <= Constants.MAX_DURATION]
    if not filtered_tracks:
        await message.edit_text("Нет подходящих треков по длительности. 😔")
        return

    keyboard = [[InlineKeyboardButton(f"▶️ {t['title']} ({format_duration(t['duration'])})", callback_data=f"play_track:{t['url']}")] for t in filtered_tracks[:10]]
    await message.edit_text('Выберите трек:', reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    command, data = query.data.split(":", 1)

    if command == "play_track":
        await query.edit_message_text(f"Обработка: {data}")
        await download_and_send(context, data, query.message.chat_id)
        await query.edit_message_text("Трек отправлен! 🎵")
    elif command == "radio":
        if not await is_admin(query.from_user.id): return await query.answer("Только для администраторов.", show_alert=True)
        if data == "refresh": await update_status_panel(context, force=True)
        elif data == "skip": context.bot_data['skip_event'].set()
        elif data == "on": await toggle_radio(context, True)
        elif data == "off": await toggle_radio(context, False)
    elif command == "vote" and data == "start":
        if not await is_admin(query.from_user.id): return await query.answer("Только для администраторов.", show_alert=True)
        await start_vote(context)
    elif command == "cmd":
        if data == "menu":
            await show_menu(update, context)
        elif data == "stopbot" and await is_admin(query.from_user.id):
            await query.answer("🛑 Останавливаю бота...")
            await context.application.stop_running()

async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    async with state_lock:
        if state.active_poll_id: return await context.bot.send_message(RADIO_CHAT_ID, "🗳 Голосование уже идет!")
        if len(state.votable_genres) < 2: return await context.bot.send_message(RADIO_CHAT_ID, "Недостаточно жанров для голосования. 😔")
        options = random.sample(state.votable_genres, min(len(state.votable_genres), 5))
        poll = await context.bot.send_poll(
            chat_id=RADIO_CHAT_ID, question="🎵 Выберите следующий жанр:", options=[opt.title() for opt in options],
            is_anonymous=False, open_period=Constants.POLL_DURATION_SECONDS
        )
        state.active_poll_id = poll.poll.id
        state.poll_message_id = poll.message_id
        state.poll_options = options
        state.poll_votes = [0] * len(options)
        await save_state(state)
    
    asyncio.create_task(close_poll_after_timeout(context, poll.poll.id))

async def close_poll_after_timeout(context: ContextTypes.DEFAULT_TYPE, poll_id: str):
    await asyncio.sleep(Constants.POLL_DURATION_SECONDS + Constants.POLL_CHECK_TIMEOUT)
    state: State = context.bot_data['state']
    if state.active_poll_id != poll_id: return
    try:
        poll_update = await context.bot.stop_poll(RADIO_CHAT_ID, state.poll_message_id)
        await handle_poll(Update(poll=poll_update), context)
    except TelegramError as e:
        logger.error(f"Не удалось закрыть голосование {poll_id}: {e}")
    finally:
        async with state_lock:
            state.active_poll_id = None
            state.poll_message_id = None
            await save_state(state)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    poll_answer: PollAnswer = update.poll_answer
    if poll_answer.poll_id == state.active_poll_id and poll_answer.option_ids:
        async with state_lock:
            state.poll_votes[poll_answer.option_ids[0]] += 1
            await save_state(state)

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if update.poll.id != state.active_poll_id or not update.poll.is_closed: return

    max_votes = max(o.voter_count for o in update.poll.options)
    if max_votes == 0: return await context.bot.send_message(RADIO_CHAT_ID, "В голосовании никто не участвовал. 😔")
    
    winning_options = [o.text.lower() for o in update.poll.options if o.voter_count == max_votes]
    selected_genre = random.choice(winning_options)
    
    async with state_lock:
        state.genre = selected_genre
        state.radio_playlist.clear()
        state.now_playing = None
        await save_state(state)

    await context.bot.send_message(RADIO_CHAT_ID, f"🎵 Новый жанр: *{escape_markdown_v2(state.genre.title())}*", parse_mode="MarkdownV2")
    await refill_playlist(context)
    if not state.is_on: await toggle_radio(context, True)

# --- Жизненный цикл бота ---
async def check_bot_permissions(application: Application):
    try:
        bot_member = await application.bot.get_chat_member(RADIO_CHAT_ID, application.bot.id)
        if bot_member.status != "administrator":
            logger.error(f"Бот не является администратором в чате {RADIO_CHAT_ID}")
            return False
        await application.bot.send_message(RADIO_CHAT_ID, "🔍 Права бота проверены.")
        return True
    except TelegramError as e:
        logger.error(f"Не удалось проверить права бота в чате {RADIO_CHAT_ID}: {e}")
        return False

async def post_init(application: Application):
    logger.info("Запускаю post_init")
    application.bot_data['state'] = load_state()
    application.bot_data['skip_event'] = asyncio.Event()
    if not await check_bot_permissions(application):
        logger.critical("У бота отсутствуют необходимые разрешения. Завершение работы.")
        return
    if application.bot_data['state'].is_on:
        logger.info("Радио включено, запускаю радио-цикл")
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))

async def on_shutdown(application: Application):
    logger.info("Начинаю завершение работы")
    task = application.bot_data.get('radio_loop_task')
    if task: task.cancel()
    await save_state(application.bot_data['state'])
    logger.info("Завершение работы выполнено")

async def raw_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"ПОЛУЧЕНО RAW ОБНОВЛЕНИЕ: {update.to_json()}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"Команда start вызвана пользователем {update.effective_user.id}")
    await show_menu(update, context)

async def radio_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await radio_on_off_command(update, context, turn_on=True)

async def radio_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await radio_on_off_command(update, context, turn_on=False)

def main():
    if not all([BOT_TOKEN, RADIO_CHAT_ID, ADMIN_IDS]):
        raise ValueError("BOT_TOKEN, RADIO_CHAT_ID или ADMIN_IDS не установлены!")
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).build()
    
    #Диагностический обработчик
    app.add_handler(MessageHandler(filters.ALL, raw_update_handler), group=-1)

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("ron", radio_on_command))
    app.add_handler(CommandHandler("rof", radio_off_command))
    app.add_handler(CommandHandler(["stop", "stopbot"], stop_bot_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("vote", vote_command))
    app.add_handler(CommandHandler("source", set_source_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(PollHandler(handle_poll))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    
    logger.info("Запускаю опрос бота...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
