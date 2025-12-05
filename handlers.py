import asyncio
import os
from typing import Optional

from telegram import Update, Message
from telegram.ext import (
    Application, 
    ContextTypes, 
    CommandHandler, 
    CallbackQueryHandler
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden

from config import settings, TrackInfo, Source
from keyboards import get_main_keyboard, get_source_keyboard
from states import BotState
from youtube_downloader import YouTubeDownloader
from deezer_downloader import DeezerDownloader
from radio_service import RadioService
from utils import is_admin, validate_query
from logger import logger


class BotHandlers:
    """Обработчики команд бота"""
    
    def __init__(self, app: Application):
        self.state = BotState()
        self.youtube = YouTubeDownloader()
        self.deezer = DeezerDownloader()
        self.radio = RadioService(self.state, app.bot, self.youtube)

    async def register_handlers(self, app: Application):
        """Регистрация всех обработчиков"""
        commands = [
            ("start", self.start),
            ("menu", self.show_menu),
            ("play", self.handle_play), 
            ("p", self.handle_play),
            ("audiobook", self.handle_audiobook), 
            ("ab", self.handle_audiobook),
            ("radio", self.handle_radio),
            ("source", self.handle_source), 
            ("src", self.handle_source),
            ("status", self.handle_status), 
            ("stat", self.handle_status),
            ("help", self.handle_help),
        ]
        
        for command, handler in commands:
            app.add_handler(CommandHandler(command, handler))
        
        app.add_handler(CallbackQueryHandler(self.handle_callback))

    async def _send_audio_safe(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        search_msg: Message,
        result: "DownloadResult"
    ):
        """Безопасно отправляет аудио, обрабатывая ошибки."""
        try:
            with open(result.file_path, 'rb') as audio:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio,
                    title=result.track_info.title,
                    performer=result.track_info.artist,
                    duration=result.track_info.duration,
                    caption=f"🎵 {result.track_info.display_name}"
                )
            await search_msg.delete()
        except Forbidden:
            logger.warning(f"Не могу отправить аудио в чат {chat_id}: бот заблокирован или исключен.")
            await search_msg.edit_text("❌ Ошибка: не могу отправить аудио. Возможно, бот заблокирован.")
        except BadRequest as e:
            logger.error(f"Ошибка отправки аудио в чат {chat_id}: {e}")
            await search_msg.edit_text("❌ Ошибка: не удалось отправить аудиофайл.")
        finally:
            if os.path.exists(result.file_path):
                os.remove(result.file_path)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        user = update.effective_user
        logger.info(f"Пользователь {user.id} запустил бота")
        
        welcome = f"""
🎵 Привет, {user.first_name}!

Я могу искать и скачивать музыку с:
• YouTube (полные треки)
• YouTube Music
• Deezer (30-секундные превью)

✨ Команды:
/play <название> - найти трек
/audiobook <название> - найти аудиокнигу
/radio on/off - радио (админ)
/source - выбрать источник
/menu - меню
/help - справка
        """.strip()
        
        await update.message.reply_text(welcome)
        await self.show_menu(update, context)
    
    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать меню"""
        keyboard = get_main_keyboard()
        status = await self._get_status_text()
        await update.message.reply_text(status, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    async def handle_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка /play"""
        if not context.args:
            await update.message.reply_text("🎶 Использование: /play <название трека>")
            return
        
        query = " ".join(context.args)
        chat_id = update.effective_chat.id
        
        is_valid, error = validate_query(query)
        if not is_valid:
            await update.message.reply_text(error)
            return
        
        search_msg = await update.message.reply_text(f"🔍 Ищу '{query}'...")
        
        try:
            result = None
            if self.state.source == Source.DEEZER:
                result = await self.deezer.download_with_retry(query)
            
            if not result or not result.success:
                result = await self.youtube.download_with_retry(query)

            if result and result.success:
                await self._send_audio_safe(context, chat_id, search_msg, result)
            else:
                await search_msg.edit_text(f"❌ Не удалось найти '{query}' ни на одном источнике.")
        
        except Exception as e:
            logger.error(f"Критическая ошибка в /play: {e}", exc_info=True)
            await search_msg.edit_text("⚠️ Произошла непредвиденная ошибка при поиске.")

    async def handle_audiobook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка /audiobook"""
        if not context.args:
            await update.message.reply_text("📖 Использование: /audiobook <название книги>")
            return
        
        query = " ".join(context.args)
        chat_id = update.effective_chat.id
        
        is_valid, error = validate_query(query)
        if not is_valid:
            await update.message.reply_text(error)
            return
        
        search_msg = await update.message.reply_text(f"📚 Ищу аудиокнигу '{query}'...")
        
        try:
            # Для аудиокниг всегда используем YouTube
            result = await self.youtube.download_long(f"{query} аудиокнига")
            
            if result and result.success:
                await self._send_audio_safe(context, chat_id, search_msg, result)
            else:
                await search_msg.edit_text(f"❌ Не удалось найти аудиокнигу '{query}'.")
        
        except Exception as e:
            logger.error(f"Критическая ошибка в /audiobook: {e}", exc_info=True)
            await search_msg.edit_text("⚠️ Ошибка при поиске аудиокниги.")

    async def handle_radio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Управление радио"""
        if not await is_admin(update, context):
            await update.message.reply_text("⛔ Только для администраторов")
            return
        
        if not context.args:
            await update.message.reply_text("📻 Использование: /radio <on/off>")
            return
        
        action = context.args[0].lower()
        chat_id = update.effective_chat.id
        
        if action == 'on':
            await self.radio.start(chat_id)
            await update.message.reply_text("📻 Радио включено!")
        elif action == 'off':
            await self.radio.stop()
            await update.message.reply_text("📻 Радио выключено.")
        else:
            await update.message.reply_text("📻 Использование: /radio <on/off>")

    async def handle_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Смена источника"""
        keyboard = get_source_keyboard()
        await update.message.reply_text("💿 Выберите источник:", reply_markup=keyboard)
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий кнопок"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data.startswith('source_'):
            source_map = {
                'source_youtube': Source.YOUTUBE,
                'source_ytmusic': Source.YOUTUBE_MUSIC,
                'source_deezer': Source.DEEZER,
            }
            new_source = source_map.get(data)
            if new_source:
                self.state.source = new_source
                await query.edit_message_text(f"💿 Источник изменен на: {self.state.source.value}")
        
        elif data == 'source_switch':
            keyboard = get_source_keyboard()
            await query.edit_message_text("💿 Выберите источник:", reply_markup=keyboard)
        
        elif data == 'radio_on':
            if await is_admin(update, context):
                await self.radio.start(update.effective_chat.id)
                await query.edit_message_text("📻 Радио включено!")
            else:
                await query.answer("⛔ Только для админов", show_alert=True)

        elif data == 'radio_off':
            if await is_admin(update, context):
                await self.radio.stop()
                await query.edit_message_text("📻 Радио выключено.")
            else:
                await query.answer("⛔ Только для админов", show_alert=True)

        elif data == 'next_track':
            if await is_admin(update, context):
                await self.radio.skip()
                await query.answer("⏭️ Пропускаем трек...")
            else:
                await query.answer("⛔ Только для админов", show_alert=True)
        
        elif data == 'menu_refresh' and query.message:
            try:
                status_text = await self._get_status_text()
                await query.edit_message_text(status_text, reply_markup=get_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
            except BadRequest:  # Сообщение не изменилось
                pass

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /help"""
        help_text = """
🎵 *Music Bot - Помощь*

*Основные команды:*
/play <название> - Найти и скачать трек
/audiobook <название> - Найти аудиокнигу
/radio <on/off> - Управление радио (админ)
/source - Выбрать источник
/menu - Показать меню
/status - Статус бота
/help - Эта справка

*Быстрые команды:*
/p <название> - То же что /play
/ab <название> - То же что /audiobook
/src - То же что /source
/stat - То же что /status

*Советы:*
1. Используйте точные названия
2. Для аудиокниг укажите автора
3. Cookies нужны для YouTube
        """.strip()
        
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
    
    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status"""
        status_text = await self._get_status_text()
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
    
    async def _get_status_text(self) -> str:
        """Генерация текста статуса"""
        radio_status = '🟢 ВКЛ' if self.state.radio.is_on else '🔴 ВЫКЛ'
        if self.state.radio.is_on and self.state.radio.current_genre:
            radio_status += f" (жанр: {self.state.radio.current_genre})"

        try:
            import psutil
            cpu = psutil.cpu_percent()
            memory = psutil.virtual_memory()
            status = f"""
🎵 *Music Bot Status*

*Система:*
• CPU: {cpu:.1f}%
• RAM: {memory.percent:.1f}%

*Бот:*
• Источник: {self.state.source.value}
• Радио: {radio_status}
            """.strip()
        except ImportError:
            status = f"""
🎵 *Music Bot Status*

*Бот:*
• Источник: {self.state.source.value}
• Радио: {radio_status}
            """.strip()
        
        return status
