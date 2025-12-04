import os
import logging
import asyncio
import signal
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.error import BadRequest, Forbidden, TelegramError

from config import (
    BOT_TOKEN, BotState, MESSAGES, check_environment, 
    PROXY_ENABLED, PROXY_URL, MAX_QUERY_LENGTH, cleanup_temp_files,
    Source, ChatData
)
from simple_youtube_downloader import SimpleYouTubeDownloader
from deezer_simple_downloader import DeezerSimpleDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message, validate_query_length
from locks import state_lock, radio_update_lock

logger = logging.getLogger(__name__)

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.job_queue: JobQueue = self.app.job_queue
        
        # Инициализируем загрузчики
        self.youtube_downloader = SimpleYouTubeDownloader()
        self.deezer_downloader = DeezerSimpleDownloadManager()
        
        self.state = BotState()
        
        logger.info("Инициализация бота...")
        
        if PROXY_ENABLED and PROXY_URL:
            logger.info(f"Прокси включен: {PROXY_URL}")
        
        self.register_handlers()
        self.app.add_error_handler(self.on_error)
        
        logger.info("Бот инициализирован")
    
    async def initialize(self):
        """Асинхронная инициализация."""
        await self.deezer_downloader.initialize()
        
        # Запуск фоновых задач
        self.job_queue.run_repeating(
            self.update_radio_task, 
            interval=300,  # 5 минут
            first=30,
            name="radio_updater"
        )
        self.job_queue.run_repeating(
            self.update_status_messages_task,
            interval=30,
            first=10,
            name="status_updater"
        )
        
        logger.info("Фоновые задачи запущены")
    
    def register_handlers(self):
        """Регистрирует обработчики команд."""
        handlers = [
            CommandHandler("start", self.start),
            CommandHandler("menu", self.show_menu),
            CommandHandler("play", self.play_song),
            CommandHandler("audiobook", self.audiobook),
            CommandHandler(["ron", "radio_on"], self.radio_on),
            CommandHandler(["roff", "radio_off"], self.radio_off),
            CommandHandler("next", self.next_track),
            CommandHandler("source", self.source_switch),
            CommandHandler("proxy", self.show_proxy_status),
            CommandHandler("status", self.get_status),
            CallbackQueryHandler(self.button_callback)
        ]
        
        for handler in handlers:
            self.app.add_handler(handler)
        
        logger.info(f"Зарегистрировано {len(handlers)} обработчиков")
    
    async def on_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок."""
        logger.error(f"Ошибка: {context.error}", exc_info=context.error)
        
        if update and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=MESSAGES['error']
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об ошибке: {e}")
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /start"""
        await update.message.reply_text(MESSAGES['welcome'])
        await self.show_menu(update, context)
    
    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню."""
        chat_id = update.effective_chat.id
        
        async with state_lock:
            if chat_id not in self.state.active_chats:
                self.state.active_chats[chat_id] = ChatData(status_message_id=None)
                logger.info(f"Новый чат: {chat_id}")
        
        await self.update_status_message(context, chat_id)
    
    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /play"""
        chat_id = update.effective_chat.id
        
        if not context.args:
            await context.bot.send_message(chat_id, MESSAGES['play_usage'])
            return
        
        query = " ".join(context.args)
        
        # Валидация
        is_valid, error_msg = validate_query_length(query)
        if not is_valid:
            await context.bot.send_message(chat_id, error_msg)
            return
        
        status_msg = await context.bot.send_message(chat_id, MESSAGES['searching'])
        
        try:
            # Выбор источника
            if self.state.source == Source.DEEZER:
                logger.info(f"Использую Deezer для: '{query}'")
                result = await self.deezer_downloader.download_track(query)
            else:
                logger.info(f"Использую {self.state.source.value} для: '{query}'")
                result = await self.youtube_downloader.download_track(query, self.state.source)
            
            if result:
                audio_path, track_info = result
                try:
                    with open(audio_path, 'rb') as audio_file:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_file,
                            title=track_info.title,
                            performer=track_info.artist,
                            duration=track_info.duration,
                            caption=f"🎵 {track_info.artist} - {track_info.title}"
                        )
                    
                    await status_msg.delete()
                    
                except TelegramError as e:
                    logger.error(f"Ошибка Telegram: {e}")
                    await status_msg.edit_text("❌ Не удалось отправить файл")
                finally:
                    # Удаляем временный файл
                    if os.path.exists(audio_path):
                        try:
                            os.remove(audio_path)
                        except:
                            pass
            else:
                await status_msg.edit_text(MESSAGES['not_found'])
                
        except Exception as e:
            logger.error(f"Ошибка в play_song: {e}", exc_info=True)
            await status_msg.edit_text("❌ Ошибка при загрузке")
    
    async def audiobook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /audiobook"""
        chat_id = update.effective_chat.id
        
        if not context.args:
            await context.bot.send_message(chat_id, MESSAGES['audiobook_usage'])
            return
        
        query = " ".join(context.args)
        
        is_valid, error_msg = validate_query_length(query)
        if not is_valid:
            await context.bot.send_message(chat_id, error_msg)
            return
        
        status_msg = await context.bot.send_message(chat_id, MESSAGES['searching_audiobook'])
        
        try:
            if self.state.source == Source.DEEZER:
                result = await self.deezer_downloader.download_longest_track(query)
            else:
                result = await self.youtube_downloader.download_longest_track(query, self.state.source)
            
            if result:
                audio_path, track_info = result
                try:
                    with open(audio_path, 'rb') as audio_file:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_file,
                            title=track_info.title,
                            performer=track_info.artist,
                            duration=track_info.duration,
                            caption=f"📖 {track_info.artist} - {track_info.title}"
                        )
                    
                    await status_msg.delete()
                    
                except TelegramError as e:
                    logger.error(f"Ошибка Telegram: {e}")
                    await status_msg.edit_text(MESSAGES['file_too_large'])
                finally:
                    if os.path.exists(audio_path):
                        try:
                            os.remove(audio_path)
                        except:
                            pass
            else:
                await status_msg.edit_text(MESSAGES['audiobook_not_found'])
                
        except Exception as e:
            logger.error(f"Ошибка в audiobook: {e}", exc_info=True)
            await status_msg.edit_text("❌ Ошибка при загрузке")
    
    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Включает радио."""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = True
        
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_on'])
        await self.update_status_message(context, update.effective_chat.id)
    
    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выключает радио."""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = False
        
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_off'])
        await self.update_status_message(context, update.effective_chat.id)
    
    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Пропускает трек на радио."""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.last_played_time = 0
        
        await context.bot.send_message(update.effective_chat.id, MESSAGES['next_track'])
    
    async def source_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Переключает источник."""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            sources = list(Source)
            current_index = sources.index(self.state.source)
            next_index = (current_index + 1) % len(sources)
            self.state.source = sources[next_index]
        
        message = MESSAGES['source_switched'].format(source=self.state.source.value)
        await context.bot.send_message(update.effective_chat.id, message)
        await self.update_status_message(context, update.effective_chat.id)
    
    async def show_proxy_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статус прокси."""
        if PROXY_ENABLED:
            message = MESSAGES['proxy_enabled']
        else:
            message = MESSAGES['proxy_disabled']
        
        await context.bot.send_message(update.effective_chat.id, message)
    
    async def get_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статус бота."""
        await self.update_status_message(context, update.effective_chat.id)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик кнопок."""
        query = update.callback_query
        await query.answer()
        
        command = query.data
        command_map = {
            'radio_on': self.radio_on,
            'radio_off': self.radio_off,
            'next_track': self.next_track,
            'source_switch': self.source_switch,
        }
        
        if command in command_map:
            await command_map[command](update, context)
    
    async def update_radio_task(self, context: ContextTypes.DEFAULT_TYPE):
        """Обновление радио."""
        if radio_update_lock.locked():
            logger.debug("Радио уже обновляется, пропускаю")
            return
        
        async with radio_update_lock:
            try:
                await self._update_radio(context)
            except Exception as e:
                logger.error(f"Ошибка в радио: {e}", exc_info=True)
    
    async def _update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        """Логика радио."""
        async with state_lock:
            if not self.state.radio_status.is_on:
                return
            
            current_time = asyncio.get_event_loop().time()
            time_since_last = current_time - self.state.radio_status.last_played_time
            
            if time_since_last < self.state.radio_status.cooldown:
                logger.debug(f"Радио кулдаун: {int(self.state.radio_status.cooldown - time_since_last)}с")
                return
        
        logger.info("Обновление радио...")
        
        # Выбор жанра
        if self.state.source == Source.DEEZER:
            genre = self.deezer_downloader.get_random_genre()
        else:
            genre = self.youtube_downloader.get_random_genre()
        
        logger.info(f"Жанр радио: {genre}")
        
        # Скачивание трека
        result = None
        try:
            if self.state.source == Source.DEEZER:
                result = await self.deezer_downloader.download_track(f"{genre} music")
            else:
                result = await self.youtube_downloader.download_track(f"{genre} music", self.state.source)
        except Exception as e:
            logger.error(f"Ошибка скачивания для радио: {e}")
            result = None
        
        if result:
            audio_path, track_info = result
            try:
                # Отправка во все активные чаты
                async with state_lock:
                    active_chats = list(self.state.active_chats.keys())
                    self.state.radio_status.current_genre = genre
                    self.state.radio_status.current_track = track_info
                
                successful = 0
                for chat_id in active_chats:
                    try:
                        with open(audio_path, 'rb') as audio_file:
                            await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=audio_file,
                                title=track_info.title,
                                performer=track_info.artist,
                                duration=track_info.duration,
                                caption=f"📻 Радио: {genre.capitalize()}"
                            )
                        successful += 1
                    except Forbidden:
                        logger.warning(f"Бот заблокирован в чате {chat_id}")
                        async with state_lock:
                            if chat_id in self.state.active_chats:
                                del self.state.active_chats[chat_id]
                    except Exception as e:
                        logger.error(f"Ошибка отправки в {chat_id}: {e}")
                
                logger.info(f"Радио отправлено в {successful}/{len(active_chats)} чатов")
                
                # Обновляем время последнего трека
                async with state_lock:
                    self.state.radio_status.last_played_time = asyncio.get_event_loop().time()
                
            except Exception as e:
                logger.error(f"Ошибка отправки радио: {e}")
            finally:
                # Удаляем файл
                if os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except:
                        pass
        else:
            logger.warning(f"Не удалось найти трек для жанра: {genre}")
            # Устанавливаем задержку при ошибке
            async with state_lock:
                self.state.radio_status.last_played_time = asyncio.get_event_loop().time()
    
    async def update_status_messages_task(self, context: ContextTypes.DEFAULT_TYPE):
        """Обновление статус-сообщений."""
        try:
            await self.update_status_message(context)
        except Exception as e:
            logger.error(f"Ошибка обновления статуса: {e}")
    
    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        """Обновляет статус-сообщение."""
        try:
            keyboard = get_menu_keyboard()
            message_text = format_status_message(self.state)
            
            # УДАЛЯЕМ HTML-ТЕГИ ИЗ ТЕКСТА
            message_text = message_text.replace('<b>', '').replace('</b>', '')
            
            async with state_lock:
                if chat_id:
                    chats_to_update = [chat_id] if chat_id in self.state.active_chats else []
                else:
                    chats_to_update = list(self.state.active_chats.keys())
            
            for cid in chats_to_update:
                try:
                    chat_data = self.state.active_chats.get(cid)
                    
                    if chat_data and chat_data.status_message_id:
                        await context.bot.edit_message_text(
                            chat_id=cid,
                            message_id=chat_data.status_message_id,
                            text=message_text,
                            reply_markup=keyboard,
                            parse_mode=None  # Отключаем HTML парсинг
                        )
                    else:
                        sent_message = await context.bot.send_message(
                            chat_id=cid,
                            text=message_text,
                            reply_markup=keyboard,
                            parse_mode=None  # Отключаем HTML парсинг
                        )
                        
                        async with state_lock:
                            if cid in self.state.active_chats:
                                self.state.active_chats[cid].status_message_id = sent_message.message_id
                        
                except BadRequest as e:
                    if "message not found" in str(e).lower():
                        async with state_lock:
                            if cid in self.state.active_chats:
                                self.state.active_chats[cid].status_message_id = None
                    elif "not modified" in str(e).lower():
                        pass  # Это нормально, сообщение не изменилось
                    else:
                        logger.warning(f"Не удалось обновить статус в {cid}: {e}")
                except Forbidden:
                    logger.warning(f"Бот заблокирован в {cid}")
                    async with state_lock:
                        if cid in self.state.active_chats:
                            del self.state.active_chats[cid]
                except Exception as e:
                    logger.error(f"Ошибка обновления статуса для {cid}: {e}")
        except Exception as e:
            logger.error(f"Критическая ошибка в update_status_message: {e}")
    
    async def shutdown(self):
        """Завершение работы."""
        logger.info("Завершение работы бота...")
        
        # Останавливаем задачи
        for job in self.job_queue.jobs():
            job.schedule_removal()
        
        # Закрываем загрузчики
        try:
            await self.youtube_downloader.close()
        except Exception as e:
            logger.error(f"Ошибка закрытия YouTube загрузчика: {e}")
        
        try:
            await self.deezer_downloader.close()
        except Exception as e:
            logger.error(f"Ошибка закрытия Deezer загрузчика: {e}")
        
        # Очищаем временные файлы
        cleanup_temp_files()
        
        logger.info("Бот завершил работу")

async def main():
    """Основная функция."""
    if not check_environment():
        logger.error("Проверка окружения не пройдена!")
        return
    
    logger.info("Запуск бота...")
    
    try:
        app = Application.builder().token(BOT_TOKEN).build()
        bot = MusicBot(app)
        
        await bot.initialize()
        
        stop_event = asyncio.Event()
        
        def signal_handler(signame):
            logger.info(f"Сигнал {signame}, завершаю работу...")
            stop_event.set()
        
        if sys.platform != 'win32':
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda s=sig: signal_handler(s.name))
        
        await app.initialize()
        await app.start()
        
        if app.updater:
            await app.updater.start_polling()
        
        logger.info("✅ Бот запущен и готов к работе!")
        
        await stop_event.wait()
        
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C...")
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске: {e}", exc_info=True)
    finally:
        logger.info("Завершение работы...")
        
        try:
            if 'app' in locals():
                if app.updater:
                    await app.updater.stop()
                
                await app.stop()
                await app.shutdown()
            
            if 'bot' in locals():
                await bot.shutdown()
        except Exception as e:
            logger.error(f"Ошибка при завершении работы: {e}")
        
        logger.info("Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())
