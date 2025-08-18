import os
import re
import asyncio
import logging
import json
import shutil
import sys
import random
import subprocess
import time
import tempfile
import uuid
import requests
import urllib.parse
import yt_dlp as youtube_dl
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Deque, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    Message,
    Chat,
    User,
    constants,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackContext,
)
from telegram.error import TelegramError, BadRequest, Forbidden, Conflict
from telegram.constants import ParseMode

# Конфигурация
TOKEN = os.getenv("TELEGRAM_TOKEN")
RADIO_CHAT_ID = int(os.getenv("RADIO_CHAT_ID", 0))
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]
SOUNDCLOUD_CLIENT_ID = os.getenv("SOUNDCLOUD_CLIENT_ID")
GENRES = ["lo-fi hip hop", "chillhop", "jazzhop", "synthwave", "ambient"]

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Типы данных
class Track:
    def __init__(self, title: str, url: str, duration: int = 0, source: str = ""):
        self.title = title
        self.url = url
        self.duration = duration
        self.source = source

    def __repr__(self):
        return f"Track('{self.title}', {self.duration}s, {self.source})"

class State:
    def __init__(self):
        self.is_on: bool = False
        self.volume: int = 70
        self.genre: str = GENRES[0]
        self.last_error: str = ""
        self.current_track: Optional[Track] = None
        self.status_message_id: Optional[int] = None
        self.playlist: Deque[Track] = deque()
        self.last_refill: Optional[datetime] = None
        self.play_start_time: Optional[datetime] = None
        self.playback_position: int = 0
        self.skip_requested: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_on": self.is_on,
            "volume": self.volume,
            "genre": self.genre,
            "last_error": self.last_error,
            "current_track": {
                "title": self.current_track.title if self.current_track else "",
                "url": self.current_track.url if self.current_track else "",
                "duration": self.current_track.duration if self.current_track else 0,
                "source": self.current_track.source if self.current_track else "",
            } if self.current_track else None,
            "status_message_id": self.status_message_id,
            "playlist": [
                {
                    "title": track.title,
                    "url": track.url,
                    "duration": track.duration,
                    "source": track.source,
                }
                for track in self.playlist
            ],
            "last_refill": self.last_refill.isoformat() if self.last_refill else None,
            "play_start_time": self.play_start_time.isoformat() if self.play_start_time else None,
            "playback_position": self.playback_position,
            "skip_requested": self.skip_requested,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        state = cls()
        state.is_on = data.get("is_on", False)
        state.volume = data.get("volume", 70)
        state.genre = data.get("genre", GENRES[0])
        state.last_error = data.get("last_error", "")
        
        if data.get("current_track"):
            track_data = data["current_track"]
            state.current_track = Track(
                track_data["title"],
                track_data["url"],
                track_data.get("duration", 0),
                track_data.get("source", ""),
            )
        
        state.status_message_id = data.get("status_message_id")
        
        state.playlist = deque()
        for track_data in data.get("playlist", []):
            state.playlist.append(Track(
                track_data["title"],
                track_data["url"],
                track_data.get("duration", 0),
                track_data.get("source", ""),
            ))
        
        if data.get("last_refill"):
            state.last_refill = datetime.fromisoformat(data["last_refill"])
        
        if data.get("play_start_time"):
            state.play_start_time = datetime.fromisoformat(data["play_start_time"])
        
        state.playback_position = data.get("playback_position", 0)
        state.skip_requested = data.get("skip_requested", False)
        return state

# Утилиты
def escape_markdown(text: str) -> str:
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

def save_state(state: State):
    with open("bot_state.json", "w") as f:
        json.dump(state.to_dict(), f, indent=2)

def load_state() -> State:
    try:
        if os.path.exists("bot_state.json"):
            with open("bot_state.json", "r") as f:
                data = json.load(f)
                return State.from_dict(data)
    except Exception as e:
        logger.error(f"Ошибка загрузки состояния: {e}")
    return State()

async def download_file(url: str, filename: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    with open(filename, 'wb') as f:
                        while True:
                            chunk = await response.content.read(1024)
                            if not chunk:
                                break
                            f.write(chunk)
                    return True
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
    return False

async def convert_to_opus(input_path: str, output_path: str, volume: int = 100) -> bool:
    try:
        volume_factor = volume / 100.0
        command = [
            'ffmpeg',
            '-i', input_path,
            '-c:a', 'libopus',
            '-b:a', '48k',
            '-vbr', 'on',
            '-compression_level', '10',
            '-application', 'audio',
            '-af', f'volume={volume_factor}',
            '-y',  # Перезаписать файл без подтверждения
            output_path
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(f"Ошибка конвертации: {stderr.decode()}")
            return False
        return True
    except Exception as e:
        logger.error(f"Ошибка конвертации в OPUS: {e}")
        return False

# Функции работы с музыкой
async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if len(state.playlist) >= 5:
        return
    
    logger.info(f"Пополнение плейлиста для жанра: {state.genre}")
    
    try:
        # SoundCloud поиск
        query = urllib.parse.quote(f"{state.genre} radio")
        url = f"https://api-v2.soundcloud.com/search/tracks?q={query}&client_id={SOUNDCLOUD_CLIENT_ID}&limit=10"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    for track in data.get('collection', []):
                        if track.get('streamable') and track.get('media', {}).get('transcodings'):
                            transcodings = track['media']['transcodings']
                            opus_transcoding = next(
                                (t for t in transcodings if t['format']['protocol'] == 'progressive'),
                                None
                            )
                            if opus_transcoding:
                                title = track['title']
                                track_url = opus_transcoding['url'] + f"?client_id={SOUNDCLOUD_CLIENT_ID}"
                                duration = int(track['duration'] / 1000)
                                state.playlist.append(Track(title, track_url, duration, "soundcloud"))
                                logger.info(f"Добавлен трек: {title}")
                                
                                if len(state.playlist) >= 10:
                                    break
        
        state.last_refill = datetime.now()
        save_state(state)
        logger.info(f"Плейлист пополнен, треков: {len(state.playlist)}")
    except Exception as e:
        logger.error(f"Ошибка пополнения плейлиста: {e}")
        state.last_error = f"Ошибка пополнения плейлиста: {e}"
        save_state(state)

async def play_next_track(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    
    if not state.playlist:
        logger.info("Плейлист пуст, пополняю...")
        await refill_playlist(context)
        if not state.playlist:
            logger.error("Не удалось пополнить плейлист")
            return
    
    state.current_track = state.playlist.popleft()
    state.play_start_time = datetime.now()
    state.playback_position = 0
    state.skip_requested = False
    save_state(state)
    
    logger.info(f"Начинаю воспроизведение: {state.current_track.title}")
    
    try:
        # Скачивание и конвертация
        temp_dir = tempfile.gettempdir()
        input_file = os.path.join(temp_dir, f"input_{uuid.uuid4().hex}.mp3")
        output_file = os.path.join(temp_dir, f"output_{uuid.uuid4().hex}.opus")
        
        if await download_file(state.current_track.url, input_file):
            if await convert_to_opus(input_file, output_file, state.volume):
                with open(output_file, 'rb') as audio_file:
                    message = await context.bot.send_audio(
                        chat_id=RADIO_CHAT_ID,
                        audio=audio_file,
                        title=state.current_track.title,
                        performer="Radio Groove AI",
                        disable_notification=True
                    )
                
                # Обновляем статусную панель
                await update_status_panel(context)
                
                # Рассчитываем продолжительность воспроизведения
                play_duration = state.current_track.duration
                
                # Ждем окончания трека или команды пропуска
                start_time = time.time()
                while time.time() - start_time < play_duration:
                    await asyncio.sleep(1)
                    state.playback_position = int(time.time() - start_time)
                    if state.skip_requested:
                        logger.info("Трек пропущен по запросу")
                        state.skip_requested = False
                        save_state(state)
                        break
                
                # Удаляем временные файлы
                try:
                    os.remove(input_file)
                    os.remove(output_file)
                except:
                    pass
            else:
                logger.error("Ошибка конвертации трека")
        else:
            logger.error("Ошибка загрузки трека")
    except Exception as e:
        logger.error(f"Ошибка воспроизведения трека: {e}")
        state.last_error = f"Ошибка воспроизведения: {e}"
        save_state(state)

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info("Запуск радио-цикла")
    
    while state.is_on:
        await play_next_track(context)
        
        # Проверяем, не выключили ли радио
        if not state.is_on:
            break
        
        # Проверяем, нужно ли пополнить плейлист
        if len(state.playlist) < 3:
            await refill_playlist(context)
    
    logger.info("Радио-цикл остановлен")

# Панель управления
def create_control_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Запустить радио", callback_data="start_radio")],
        [InlineKeyboardButton("⏹️ Остановить радио", callback_data="stop_radio")],
        [InlineKeyboardButton("⏭️ Пропустить трек", callback_data="skip_track")],
        [InlineKeyboardButton("🔊 Громкость", callback_data="volume_settings")],
        [InlineKeyboardButton("🎵 Сменить жанр", callback_data="change_genre")]
    ])

async def update_status_panel(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    
    if not state.current_track:
        return
    
    # Рассчитываем прогресс воспроизведения
    progress = ""
    if state.current_track.duration > 0 and state.playback_position > 0:
        position_min = state.playback_position // 60
        position_sec = state.playback_position % 60
        duration_min = state.current_track.duration // 60
        duration_sec = state.current_track.duration % 60
        progress = f"\nПрогресс: {position_min}:{position_sec:02d} / {duration_min}:{duration_sec:02d}"
    
    status = "ВКЛ" if state.is_on else "ВЫКЛ"
    genre = state.genre.capitalize()
    
    text = (
        f"*Сейчас играет:*\n"
        f"{escape_markdown(state.current_track.title)}\n\n"
        f"• Громкость: `{state.volume}%`\n"
        f"• Статус: `{status}`\n"
        f"• Жанр: `{genre}`"
        f"{progress}"
    )
    
    try:
        if state.status_message_id:
            await context.bot.edit_message_text(
                chat_id=RADIO_CHAT_ID,
                message_id=state.status_message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            message = await context.bot.send_message(
                chat_id=RADIO_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            state.status_message_id = message.message_id
            save_state(state)
    except (BadRequest, Forbidden) as e:
        logger.error(f"Ошибка обновления статуса: {e}")
        # Сбрасываем ID сообщения статуса
        state.status_message_id = None
        save_state(state)

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я музыкальный радио-бот. Используй /help для списка команд.",
        reply_markup=create_control_panel()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = [
        "/start - Начать работу",
        "/help - Помощь",
        "/play - Запустить радио",
        "/stop - Остановить радио",
        "/skip - Пропустить трек",
        "/volume [0-100] - Настроить громкость",
        "/genre - Выбрать жанр"
    ]
    await update.message.reply_text(
        "📝 *Доступные команды:*\n\n" + "\n".join(commands),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def play_radio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if state.is_on:
        await update.message.reply_text("Радио уже запущено!")
        return
    
    state.is_on = True
    save_state(state)
    
    await update.message.reply_text("🚀 Запускаю радио...")
    
    # Запускаем радио-цикл
    if 'radio_loop_task' not in context.bot_data or context.bot_data['radio_loop_task'].done():
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    
    await refill_playlist(context)
    await update_status_panel(context)

async def stop_radio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not state.is_on:
        await update.message.reply_text("Радио уже остановлено!")
        return
    
    state.is_on = False
    save_state(state)
    
    # Останавливаем задачу
    if 'radio_loop_task' in context.bot_data:
        context.bot_data['radio_loop_task'].cancel()
        del context.bot_data['radio_loop_task']
    
    await update.message.reply_text("⏹ Радио остановлено")
    await update_status_panel(context)

async def skip_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    if not state.is_on:
        await update.message.reply_text("Радио не запущено!")
        return
    
    state.skip_requested = True
    save_state(state)
    await update.message.reply_text("⏭ Пропускаю текущий трек...")

async def set_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    try:
        volume = int(context.args[0])
        if 0 <= volume <= 100:
            state.volume = volume
            save_state(state)
            await update.message.reply_text(f"🔊 Громкость установлена на {volume}%")
            await update_status_panel(context)
        else:
            await update.message.reply_text("Укажите громкость от 0 до 100")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /volume [0-100]")

async def set_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    try:
        genre = " ".join(context.args).strip().lower()
        if genre in GENRES:
            state.genre = genre
            save_state(state)
            await update.message.reply_text(f"🎵 Жанр изменён на {genre.capitalize()}")
            await update_status_panel(context)
        else:
            await update.message.reply_text(f"Доступные жанры: {', '.join(GENRES)}")
    except IndexError:
        await update.message.reply_text("Использование: /genre [название жанра]")

# Обработчики кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "start_radio":
        await play_radio(update, context)
    elif query.data == "stop_radio":
        await stop_radio(update, context)
    elif query.data == "skip_track":
        await skip_track(update, context)
    elif query.data == "volume_settings":
        await query.edit_message_text(
            "🔊 Установите громкость:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔈 50%", callback_data="vol_50"),
                [InlineKeyboardButton("🔉 70%", callback_data="vol_70"),
                [InlineKeyboardButton("🔊 100%", callback_data="vol_100"),
                [InlineKeyboardButton("◀️ Назад", callback_data="back")]
            ])
        )
    elif query.data == "change_genre":
        buttons = []
        for genre in GENRES:
            buttons.append([InlineKeyboardButton(
                genre.capitalize(), 
                callback_data=f"genre_{genre}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
        
        await query.edit_message_text(
            "🎵 Выберите жанр:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif query.data.startswith("vol_"):
        volume = int(query.data.split("_")[1])
        state: State = context.bot_data['state']
        state.volume = volume
        save_state(state)
        await query.edit_message_text(f"🔊 Громкость установлена на {volume}%")
        await update_status_panel(context)
    elif query.data.startswith("genre_"):
        genre = query.data.split("_", 1)[1]
        state: State = context.bot_data['state']
        state.genre = genre
        save_state(state)
        await query.edit_message_text(f"🎵 Жанр изменён на {genre.capitalize()}")
        await update_status_panel(context)
    elif query.data == "back":
        await query.edit_message_text(
            "Панель управления:",
            reply_markup=create_control_panel()
        )

# Проверка прав бота
async def check_bot_permissions(context: ContextTypes.DEFAULT_TYPE) -> bool:
    logger.info("Проверка прав бота...")
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            chat = await context.bot.get_chat(RADIO_CHAT_ID)
            
            if chat.type in ["group", "supergroup", "channel"]:
                bot_member = await context.bot.get_chat_member(RADIO_CHAT_ID, context.bot.id)
                
                if bot_member.status != "administrator":
                    logger.warning(f"Бот не администратор (попытка {attempt+1}/{max_attempts})")
                    continue
                
                required_permissions = [
                    'can_send_messages',
                    'can_send_audios',
                    'can_send_media_messages',
                    'can_manage_messages'
                ]
                
                missing_perms = [
                    perm for perm in required_permissions
                    if not getattr(bot_member, perm, False)
                ]
                
                if missing_perms:
                    logger.warning(f"Не хватает прав: {', '.join(missing_perms)} (попытка {attempt+1}/{max_attempts})")
                    continue
                
                return True
            
            # Для личных чатов права всегда есть
            return True
        
        except TelegramError as e:
            logger.error(f"Ошибка проверки прав: {e}")
        except Exception as e:
            logger.exception(f"Непредвиденная ошибка: {e}")
        
        if attempt < max_attempts - 1:
            await asyncio.sleep(10)
    
    return False

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str):
    logger.info(f"Уведомление администраторов: {message}")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except TelegramError as e:
            logger.error(f"Ошибка отправки уведомления администратору {admin_id}: {e}")

# Инициализация бота
async def post_init(application: Application):
    logger.info("Инициализация бота...")
    application.bot_data['state'] = load_state()
    state: State = application.bot_data['state']
    
    # Проверка зависимостей
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.error("FFmpeg не найден!")
        state.last_error = "FFmpeg не установлен"
        await notify_admins(application, "⚠️ *FFmpeg не установлен!* Бот не может работать без него.")
        return
    
    # Проверка прав
    if not await check_bot_permissions(application):
        logger.error("Проверка прав не пройдена после 3 попыток")
        state.last_error = "Недостаточно прав в чате"
        
        error_msg = (
            "🚫 *Ошибка настройки бота!*\n\n"
            "Боту не хватает необходимых прав в чате.\n"
            "Пожалуйста:\n"
            "1. Сделайте бота *администратором* чата\n"
            "2. Убедитесь, что выданы права:\n"
            "   • Отправка сообщений\n"
            "   • Отправка аудио\n"
            "   • Отправка медиа\n"
            "   • Управление сообщениями\n\n"
            f"ID чата: `{RADIO_CHAT_ID}`\n"
            "После исправления перезапустите бота."
        )
        
        await notify_admins(application, error_msg)
        return
    
    # Проверка режима конфиденциальности
    try:
        bot_info = await application.bot.get_me()
        if bot_info.can_read_all_group_messages is False:
            privacy_msg = (
                "🔒 *Включен режим конфиденциальности!*\n\n"
                "Пожалуйста отключите его через @BotFather:\n"
                "1. Откройте @BotFather\n"
                "2. Выберите своего бота\n"
                "3. Отправьте `/setprivacy`\n"
                "4. Выберите *Disable*\n\n"
                "Бот не может работать с включенным режимом конфиденциальности."
            )
            await notify_admins(application, privacy_msg)
            return
    except Exception as e:
        logger.error(f"Ошибка проверки режима конфиденциальности: {e}")
    
    # Запуск радио, если оно было включено
    if state.is_on:
        logger.info("Запуск радио-цикла")
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
        await refill_playlist(application)
    
    logger.info("Бот успешно инициализирован")
    await application.bot.send_message(
        RADIO_CHAT_ID,
        "🎵 *Radio Groove AI запущен!*\n"
        "Панель статуса появится в ближайшее время...",
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    logger.error(f"Ошибка: {error}", exc_info=error)
    
    if isinstance(error, Conflict):
        logger.critical("Обнаружен конфликт: запущен другой экземпляр бота. Завершаю работу.")
        await context.application.stop()
        sys.exit(1)
    
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Произошла ошибка: {error}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except:
        pass

async def main():
    # Создаем Application
    application = ApplicationBuilder() \
        .token(TOKEN) \
        .post_init(post_init) \
        .build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("play", play_radio))
    application.add_handler(CommandHandler("stop", stop_radio))
    application.add_handler(CommandHandler("skip", skip_track))
    application.add_handler(CommandHandler("volume", set_volume))
    application.add_handler(CommandHandler("genre", set_genre))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Бот успешно запущен")
        
        # Бесконечный цикл
        while True:
            await asyncio.sleep(3600)
    
    except Conflict as e:
        logger.critical(f"Конфликт: {e}\nЗавершаю работу. Убедитесь, что запущен только один экземпляр бота.")
        sys.exit(1)
    finally:
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
