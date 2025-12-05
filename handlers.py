import asyncio
import os
from telegram import Update
from telegram.ext import Application, ContextTypes
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
        await update.message.reply_text(status, reply_markup=keyboard)
    
    async def handle_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка /play"""
        if not context.args:
            await update.message.reply_text("🎶 Использование: /play <название трека>")
            return
        
        query = " ".join(context.args)
        chat_id = update.effective_chat.id
        
        # Проверка запроса
        is_valid, error = validate_query(query)
        if not is_valid:
            await update.message.reply_text(error)
            return
        
        # Сообщение о поиске
        search_msg = await update.message.reply_text(f"🔍 Ищу '{query}'...")
        
        try:
            # Скачиваем в зависимости от источника
            if self.state.source == Source.DEEZER:
                result = await self.deezer.download_with_retry(query)
            else:
                result = await self.youtube.download_with_retry(query)
            
            if result and result.success:
                # Отправляем аудио
                with open(result.file_path, 'rb') as audio:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio,
                        title=result.track_info.title,
                        performer=result.track_info.artist,
                        duration=result.track_info.duration,
                        caption=f"🎵 {result.track_info.display_name}"
                    )
                
                # Удаляем файл
                try:
                    os.remove(result.file_path)
                except:
                    pass
                
                # Удаляем сообщение о поиске
                try:
                    await search_msg.delete()
                except:
                    pass
            else:
                # Пробуем другой источник
                if self.state.source != Source.DEEZER:
                    await search_msg.edit_text("Пробую Deezer...")
                    result = await self.deezer.download_with_retry(query)
                
                if result and result.success:
                    with open(result.file_path, 'rb') as audio:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio,
                            title=result.track_info.title,
                            performer=result.track_info.artist,
                            duration=result.track_info.duration,
                            caption=f"🎵 {result.track_info.display_name} (Deezer Preview)"
                        )
                    try:
                        os.remove(result.file_path)
                        await search_msg.delete()
                    except:
                        pass
                else:
                    await search_msg.edit_text(f"❌ Не удалось найти '{query}'")
        
        except Exception as e:
            logger.error(f"Ошибка в /play: {e}")
            await search_msg.edit_text("⚠️ Произошла ошибка при поиске")
    
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
            if self.state.source == Source.DEEZER:
                result = await self.deezer.download_long(query)
            else:
                result = await self.youtube.download_long(f"{query} аудиокнига")
            
            if result and result.success:
                with open(result.file_path, 'rb') as audio:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio,
                        title=result.track_info.title,
                        performer=result.track_info.artist,
                        duration=result.track_info.duration,
                        caption=f"📖 {result.track_info.display_name}"
                    )
                try:
                    os.remove(result.file_path)
                    await search_msg.delete()
                except:
                    pass
            else:
                await search_msg.edit_text(f"❌ Не удалось найти аудиокнигу '{query}'")
        
        except Exception as e:
            logger.error(f"Ошибка в /audiobook: {e}")
            await search_msg.edit_text("⚠️ Ошибка при поиске аудиокниги")
    
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
            self.state.radio.is_on = True
            await update.message.reply_text("📻 Радио включено!")
            await self.radio.start(chat_id)
        elif action == 'off':
            self.state.radio.is_on = False
            await update.message.reply_text("📻 Радио выключено")
            await self.radio.stop()
        else:
            await update.message.reply_text("📻 Использование: /radio <on/off>")
    
    async def handle_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Смена источника"""
        if context.args:
            source_map = {
                'youtube': Source.YOUTUBE,
                'yt': Source.YOUTUBE,
                'deezer': Source.DEEZER,
                'dz': Source.DEEZER,
                'ytmusic': Source.YOUTUBE_MUSIC,
            }
            
            source_arg = context.args[0].lower()
            if source_arg in source_map:
                self.state.source = source_map[source_arg]
                await update.message.reply_text(f"💿 Источник изменен на: {self.state.source.value}")
                return
        
        keyboard = get_source_keyboard()
        await update.message.reply_text("💿 Выберите источник:", reply_markup=keyboard)
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий кнопок"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == 'source_youtube':
            self.state.source = Source.YOUTUBE
            await query.edit_message_text("💿 Источник: YouTube")
        elif data == 'source_ytmusic':
            self.state.source = Source.YOUTUBE_MUSIC
            await query.edit_message_text("💿 Источник: YouTube Music")
        elif data == 'source_deezer':
            self.state.source = Source.DEEZER
            await query.edit_message_text("💿 Источник: Deezer")
        elif data == 'source_switch':
            keyboard = get_source_keyboard()
            await query.edit_message_text("💿 Выберите источник:", reply_markup=keyboard)
        elif data == 'radio_on':
            if await is_admin(update, context):
                self.state.radio.is_on = True
                await query.edit_message_text("📻 Радио включено!")
                await self.radio.start(update.effective_chat.id)
            else:
                await query.answer("⛔ Только для админов", show_alert=True)
        elif data == 'radio_off':
            if await is_admin(update, context):
                self.state.radio.is_on = False
                await query.edit_message_text("📻 Радио выключено")
                await self.radio.stop()
            else:
                await query.answer("⛔ Только для админов", show_alert=True)
        elif data == 'next_track':
            if await is_admin(update, context):
                await self.radio.skip()
                await query.answer("⏭️ Пропускаем трек...")
            else:
                await query.answer("⛔ Только для админов", show_alert=True)
        elif data == 'menu_refresh':
            await self.show_menu(update, context)
    
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
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status"""
        status_text = await self._get_status_text()
        await update.message.reply_text(status_text)
    
    async def _get_status_text(self) -> str:
        """Генерация текста статуса"""
        radio_status = '🟢 ВКЛ' if self.state.radio.is_on else '🔴 ВЫКЛ'
        if self.state.radio.is_on and self.state.radio.current_genre:
            radio_status += f" ({self.state.radio.current_genre})"

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
        except:
            status = f"""
🎵 *Music Bot Status*

*Бот:*
• Источник: {self.state.source.value}
• Радио: {radio_status}
            """.strip()
        
        return status