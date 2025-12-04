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
    PROXY_ENABLED, PROXY_URL, MAX_QUERY_LENGTH, cleanup_temp_files
)
from downloader import AudioDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message, validate_query_length
from locks import state_lock, radio_update_lock

logger = logging.getLogger(__name__)

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.job_queue: JobQueue = self.app.job_queue
        self.downloader = AudioDownloadManager()
        self.state = BotState()
        
        logger.info("Инициализация бота...")
        
        if PROXY_ENABLED and PROXY_URL:
            logger.info(f"Прокси включен: {PROXY_URL}")
        
        # Регистрация обработчиков
        self.register_handlers()
        
        # Регистрация обработчика ошибок
        self.app.add_error_handler(self.on_error)
        
        # Запуск фоновых задач
        self.job_queue.run_repeating(
            self.update_radio_task, 
            interval=60, 
            first=10,
            name="radio_updater"
        )
        self.job_queue.run_repeating(
            self.update_status_messages_task,
            interval=30,
            first=5,
            name="status_updater"
        )
        
        logger.info("Бот инициализирован")
    
    def register_handlers(self):
        """Регистрирует все обработчики команд"""
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
        """Глобальный обработчик ошибок"""
        logger.error(f"Ошибка: {context.error}", exc_info=context.error)
        
        if update and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Произошла ошибка. Попробуйте позже."
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об ошибке: {e}")
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        await update.message.reply_text(MESSAGES['welcome'])
        await self.show_menu(update, context)
    
    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню"""
        chat_id = update.effective_chat.id
        
        async with state_lock:
            if chat_id not in self.state.active_chats:
                self.state.active_chats[chat_id] = BotState.ChatData(status_message_id=None)
                logger.info(f"Новый чат: {chat_id}")
        
        await self.update_status_message(context, chat_id)
    
    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /play"""
        chat_id = update.effective_chat.id
        
        if not context.args:
            await context.bot.send_message(chat_id, MESSAGES['play_usage'])
            return
        
        query = " ".join(context.args)
        
        # Валидация запроса
        is_valid, error_msg = validate_query_length(query)
        if not is_valid:
            await context.bot.send_message(chat_id, error_msg)
            return
        
        status_msg = await context.bot.send_message(chat_id, MESSAGES['searching'])
        
        try:
            result = await self.downloader.download_track(query, self.state.source)
            
            if result:
                audio_path, track_info = result
                try:
                    # Отправляем файл по частям (stream)
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
                    logger.error(f"Ошибка Telegram при отправке: {e}")
                    await status_msg.edit_text("❌ Не удалось отправить файл")
                except Exception as e:
                    logger.error(f"Ошибка при отправке: {e}")
                    await status_msg.edit_text("❌ Ошибка отправки")
                finally:
                    # Всегда удаляем файл
                    if os.path.exists(audio_path):
                        try:
                            os.remove(audio_path)
                            logger.debug(f"Удален файл: {audio_path}")
                        except Exception as e:
                            logger.error(f"Не удалось удалить файл: {e}")
            else:
                await status_msg.edit_text(MESSAGES['not_found'])
                
        except Exception as e:
            logger.error(f"Ошибка в play_song: {e}")
            await status_msg.edit_text("❌ Ошибка при загрузке")
    
    async def audiobook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /audiobook"""
        chat_id = update.effective_chat.id
        
        if not context.args:
            await context.bot.send_message(chat_id, MESSAGES['audiobook_usage'])
            return
        
        query = " ".join(context.args)
        
        # Валидация запроса
        is_valid, error_msg = validate_query_length(query)
        if not is_valid:
            await context.bot.send_message(chat_id, error_msg)
            return
        
        status_msg = await context.bot.send_message(chat_id, MESSAGES['searching_audiobook'])
        
        try:
            result = await self.downloader.download_longest_track(query, self.state.source)
            
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
                except Exception as e:
                    logger.error(f"Ошибка: {e}")
                    await status_msg.edit_text("❌ Ошибка отправки")
                finally:
                    if os.path.exists(audio_path):
                        try:
                            os.remove(audio_path)
                        except:
                            pass
            else:
                await status_msg.edit_text(MESSAGES['audiobook_not_found'])
                
        except Exception as e:
            logger.error(f"Ошибка в audiobook: {e}")
            await status_msg.edit_text("❌ Ошибка при загрузке")
    
    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Включает радио"""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = True
        
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_on'])
        await self.update_status_message(context, update.effective_chat.id)
    
    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выключает радио"""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = False
        
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_off'])
        await self.update_status_message(context, update.effective_chat.id)
    
    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Пропускает текущий трек на радио"""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.last_played_time = 0
        
        await context.bot.send_message(update.effective_chat.id, MESSAGES['next_track'])
    
    async def source_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Переключает источник музыки"""
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            sources = list(self.state.source.__class__)
            current_index = sources.index(self.state.source)
            next_index = (current_index + 1) % len(sources)
            self.state.source = sources[next_index]
        
        message = MESSAGES['source_switched'].format(source=self.state.source.value)
        await context.bot.send_message(update.effective_chat.id, message)
        await self.update_status_message(context, update.effective_chat.id)
    
    async def show_proxy_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статус прокси"""
        if PROXY_ENABLED:
            message = MESSAGES['proxy_enabled']
        else:
            message = MESSAGES['proxy_disabled']
        
        await context.bot.send_message(update.effective_chat.id, message)
    
    async def get_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает статус бота"""
        await self.update_status_message(context, update.effective_chat.id)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик инлайн-кнопок"""
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
        """Фоновая задача для обновления радио"""
        # Проверяем блокировку
        if radio_update_lock.locked():
            logger.debug("Обновление радио уже выполняется, пропускаю")
            return
        
        async with radio_update_lock:
            try:
                await self._update_radio(context)
            except Exception as e:
                logger.error(f"Критическая ошибка в update_radio_task: {e}")
    
    async def _update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        """Основная логика обновления радио"""
        # Проверяем состояние радио
        async with state_lock:
            if not self.state.radio_status.is_on:
                logger.debug("Радио выключено")
                return
            
            current_time = asyncio.get_event_loop().time()
            time_since_last = current_time - self.state.radio_status.last_played_time
            
            if time_since_last < self.state.radio_status.cooldown:
                logger.debug(f"Кулдаун активен: {int(self.state.radio_status.cooldown - time_since_last)}с осталось")
                return
        
        logger.info("Начинаю обновление радио...")
        
        # Получаем случайный жанр
        genre = self.downloader.get_random_genre()
        logger.info(f"Выбран жанр: {genre}")
        
        # Пытаемся скачать трек
        result = await self.downloader.download_track(
            f"{genre} music", 
            self.state.source
        )
        
        if result:
            audio_path, track_info = result
            try:
                # Отправляем во все активные чаты
                async with state_lock:
                    active_chats = list(self.state.active_chats.keys())
                    self.state.radio_status.current_genre = genre
                    self.state.radio_status.current_track = track_info
                
                logger.info(f"Отправляю трек в {len(active_chats)} чатов")
                
                successful_sends = 0
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
                        successful_sends += 1
                    except Forbidden:
                        logger.warning(f"Бот заблокирован в чате {chat_id}")
                        async with state_lock:
                            if chat_id in self.state.active_chats:
                                del self.state.active_chats[chat_id]
                    except Exception as e:
                        logger.error(f"Ошибка отправки в {chat_id}: {e}")
                
                logger.info(f"Успешно отправлено в {successful_sends}/{len(active_chats)} чатов")
                
                # Обновляем время последнего трека
                async with state_lock:
                    self.state.radio_status.last_played_time = asyncio.get_event_loop().time()
                
            finally:
                # Всегда удаляем файл
                if os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except Exception as e:
                        logger.error(f"Не удалось удалить файл: {e}")
        else:
            logger.warning(f"Не удалось найти трек для жанра: {genre}")
            # Устанавливаем задержку при ошибке (5 минут вместо обычных)
            async with state_lock:
                self.state.radio_status.last_played_time = asyncio.get_event_loop().time()
    
    async def update_status_messages_task(self, context: ContextTypes.DEFAULT_TYPE):
        """Фоновая задача для обновления статус-сообщений"""
        try:
            await self.update_status_message(context)
        except Exception as e:
            logger.error(f"Ошибка в update_status_messages_task: {e}")
    
    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        """Обновляет статус-сообщение"""
        keyboard = get_menu_keyboard()
        message_text = format_status_message(self.state)
        
        # Определяем какие чаты обновлять
        async with state_lock:
            if chat_id:
                chats_to_update = [chat_id] if chat_id in self.state.active_chats else []
            else:
                chats_to_update = list(self.state.active_chats.keys())
        
        for cid in chats_to_update:
            try:
                chat_data = self.state.active_chats.get(cid)
                
                if chat_data and chat_data.status_message_id:
                    # Пытаемся обновить существующее
                    await context.bot.edit_message_text(
                        chat_id=cid,
                        message_id=chat_data.status_message_id,
                        text=message_text,
                        reply_markup=keyboard,
                        parse_mode='HTML'
                    )
                else:
                    # Создаем новое
                    sent_message = await context.bot.send_message(
                        chat_id=cid,
                        text=message_text,
                        reply_markup=keyboard,
                        parse_mode='HTML'
                    )
                    
                    async with state_lock:
                        if cid in self.state.active_chats:
                            self.state.active_chats[cid].status_message_id = sent_message.message_id
                    
            except BadRequest as e:
                if "message not found" in str(e).lower():
                    # Сообщение удалено, сбрасываем ID
                    async with state_lock:
                        if cid in self.state.active_chats:
                            self.state.active_chats[cid].status_message_id = None
                else:
                    logger.warning(f"Не удалось обновить статус в {cid}: {e}")
            except Forbidden:
                logger.warning(f"Бот заблокирован в {cid}")
                async with state_lock:
                    if cid in self.state.active_chats:
                        del self.state.active_chats[cid]
            except Exception as e:
                logger.error(f"Ошибка обновления статуса для {cid}: {e}")
    
    async def shutdown(self):
        """Корректное завершение работы"""
        logger.info("Завершение работы бота...")
        
        # Останавливаем задачи
        for job in self.job_queue.jobs():
            job.schedule_removal()
        
        # Закрываем загрузчик
        await self.downloader.close()
        
        # Очищаем временные файлы
        cleanup_temp_files()
        
        logger.info("Бот завершил работу")

async def main():
    """Основная функция запуска"""
    if not check_environment():
        logger.error("Проверка окружения не пройдена!")
        return
    
    logger.info("Запуск бота...")
    
    # Создаем приложение
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Создаем экземпляр бота
    bot = MusicBot(app)
    
    # Настраиваем обработку сигналов
    stop_event = asyncio.Event()
    
    def signal_handler(signame):
        logger.info(f"Получен сигнал {signame}, завершаю работу...")
        stop_event.set()
    
    if sys.platform != 'win32':
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s.name))
    
    try:
        # Запускаем бота
        await app.initialize()
        await app.start()
        
        if app.updater:
            await app.updater.start_polling()
        
        logger.info("✅ Бот запущен и готов к работе!")
        
        # Ждем сигнала остановки
        await stop_event.wait()
        
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C...")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        # Корректное завершение
        logger.info("Завершение...")
        
        if app.updater:
            await app.updater.stop()
        
        await app.stop()
        await app.shutdown()
        
        await bot.shutdown()
        
        logger.info("Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())