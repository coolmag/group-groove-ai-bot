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
        
        self.register_handlers()
        self.app.add_error_handler(self.on_error)
        
        logger.info("Бот инициализирован")
    
    async def initialize(self):
        """Асинхронная инициализация."""
        await self.deezer_downloader.initialize()
        
        # Запуск фоновых задач
        self.job_queue.run_repeating(
            self.update_radio_task, 
            interval=300,
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
            CommandHandler("p", self.play_song),
            CommandHandler("audiobook", self.audiobook),
            CommandHandler("ab", self.audiobook),
            CommandHandler(["ron", "radio_on"], self.radio_on),
            CommandHandler(["roff", "radio_off"], self.radio_off),
            CommandHandler("next", self.next_track),
            CommandHandler("n", self.next_track),
            CommandHandler("source", self.source_switch),
            CommandHandler("src", self.source_switch),
            CommandHandler("proxy", self.show_proxy_status),
            CommandHandler("status", self.get_status),
            CommandHandler("stat", self.get_status),
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
    
    async def download_with_timeout(self, query: str, source: Source, timeout: int = 30):
        """Скачивает трек с таймаутом."""
        try:
            if source == Source.DEEZER:
                logger.info(f"Использую Deezer: '{query}'")
                return await asyncio.wait_for(
                    self.deezer_downloader.download_track(query),
                    timeout=timeout
                )
            else:
                logger.info(f"Использую {source.value}: '{query}'")
                return await asyncio.wait_for(
                    self.youtube_downloader.download_track(query, source),
                    timeout=timeout
                )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут {source.value}: '{query}'")
            return None
        except Exception as e:
            if "YouTube заблокировал запрос" in str(e):
                raise  # Пробрасываем для специальной обработки
            logger.error(f"Ошибка {source.value}: {e}")
            return None
    
    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик /play и /p."""
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
            # Пробуем текущий источник
            result = None
            current_source = self.state.source
            
            try:
                result = await self.download_with_timeout(query, current_source, timeout=35)
            except Exception as e:
                if "YouTube заблокировал запрос" in str(e):
                    await status_msg.edit_text(MESSAGES['youtube_blocked'])
                    return
            
            # Если не получилось, пробуем Deezer как резервный
            if not result and current_source != Source.DEEZER:
                logger.info(f"Пробую Deezer как резерв для: '{query}'")
                await status_msg.edit_text("⚠️ Основной источник не ответил, пробую Deezer...")
                result = await self.download_with_timeout(query, Source.DEEZER, timeout=25)
            
            # Если Deezer не сработал, пробуем другие источники
            if not result:
                sources_to_try = [s for s in Source.get_available_sources() 
                                if s not in [current_source, Source.DEEZER]]
                
                for source in sources_to_try:
                    logger.info(f"Пробую {source.value} для: '{query}'")
                    result = await self.download_with_timeout(query, source, timeout=20)
                    if result:
                        break
            
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
        """Обработчик /audiobook и /ab."""
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
            result = None
            
            # Для аудиокниг используем YouTube (если не Deezer)
            if self.state.source == Source.DEEZER:
                result = await asyncio.wait_for(
                    self.deezer_downloader.download_longest_track(f"{query} аудиокнига"),
                    timeout=40
                )
            else:
                # Используем специализированный поиск аудиокниг
                result = await asyncio.wait_for(
                    self.youtube_downloader.download_audiobook(query, self.state.source),
                    timeout=60
                )
            
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
                            caption=f"📖 Аудиокнига: {track_info.artist} - {track_info.title}"
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
                
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при поиске аудиокниги: '{query}'")
            await status_msg.edit_text("⏰ Поиск занял слишком много времени. Попробуйте другой запрос.")
        except Exception as e:
            logger.error(f"Ошибка в audiobook: {e}", exc_info=True)
            await status_msg.edit_text("❌ Ошибка при поиске аудиокниги")
    
    # ... остальные методы остаются без изменений ...
    
    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        """Обновляет статус-сообщение БЕЗ HTML."""
        try:
            keyboard = get_menu_keyboard()
            message_text = format_status_message(self.state)
            
            # ВАЖНО: Удаляем все HTML-теги
            import re
            message_text = re.sub(r'<[^>]+>', '', message_text)
            
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
                            parse_mode=None  # Отключаем HTML полностью
                        )
                    else:
                        sent_message = await context.bot.send_message(
                            chat_id=cid,
                            text=message_text,
                            reply_markup=keyboard,
                            parse_mode=None  # Отключаем HTML полностью
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
                        pass
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