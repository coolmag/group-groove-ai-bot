import os
import logging
import asyncio
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.error import BadRequest, Forbidden

from config import (
    BOT_TOKEN, BotState, MESSAGES, check_environment, 
    cleanup_temp_files, Source, ChatData, ADMIN_IDS, 
    PROXY_ENABLED, PROXY_URL, MAX_QUERY_LENGTH
)
from simple_youtube_downloader import SimpleYouTubeDownloader
from deezer_simple_downloader import DeezerSimpleDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message, validate_query_length
from locks import state_lock

logger = logging.getLogger(__name__)

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.job_queue = app.job_queue
        self.youtube_downloader = SimpleYouTubeDownloader()
        self.deezer_downloader = DeezerSimpleDownloadManager()
        self.state = BotState()
        
    async def initialize(self):
        await self.deezer_downloader.initialize()
        self.job_queue.run_repeating(self.update_radio_task, interval=300, first=30)
        self.job_queue.run_repeating(self.update_status_task, interval=30, first=10)
        
    def register_handlers(self):
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
            CommandHandler("source", self.source_switch),
            CommandHandler("src", self.source_switch),
            CommandHandler("proxy", self.show_proxy_status),
            CommandHandler("status", self.get_status),
            CommandHandler("stat", self.get_status),
            CallbackQueryHandler(self.button_callback)
        ]
        for handler in handlers:
            self.app.add_handler(handler)
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(MESSAGES['welcome'])
        await self.show_menu(update, context)
    
    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        async with state_lock:
            if chat_id not in self.state.active_chats:
                self.state.active_chats[chat_id] = ChatData()
        await self.update_status_message(context, chat_id)
    
    async def download_with_timeout(self, query: str, source: Source, timeout=30):
        try:
            if source == Source.DEEZER:
                return await asyncio.wait_for(
                    self.deezer_downloader.download_track(query),
                    timeout=timeout
                )
            else:
                return await asyncio.wait_for(
                    self.youtube_downloader.download_track(query, source),
                    timeout=timeout
                )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут {source.value}: '{query}'")
            return None
        except Exception as e:
            logger.error(f"Ошибка {source.value}: {e}")
            return None
    
    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(MESSAGES['play_usage'])
            return
        
        query = " ".join(context.args)
        chat_id = update.effective_chat.id
        
        is_valid, error_msg = validate_query_length(query)
        if not is_valid:
            await update.message.reply_text(error_msg)
            return
        
        status_msg = await update.message.reply_text(MESSAGES['searching'])
        
        try:
            # Пробуем текущий источник
            result = await self.download_with_timeout(query, self.state.source, timeout=35)
            
            # Если не получилось, пробуем Deezer
            if not result and self.state.source != Source.DEEZER:
                result = await self.download_with_timeout(query, Source.DEEZER, timeout=30)
            
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
                except Exception as e:
                    logger.error(f"Ошибка отправки: {e}")
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
            logger.error(f"Ошибка: {e}")
            await status_msg.edit_text("❌ Ошибка при загрузке")
    
    async def audiobook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(MESSAGES['audiobook_usage'])
            return
        
        query = " ".join(context.args)
        chat_id = update.effective_chat.id
        
        is_valid, error_msg = validate_query_length(query)
        if not is_valid:
            await update.message.reply_text(error_msg)
            return
        
        status_msg = await update.message.reply_text(MESSAGES['searching_audiobook'])
        
        try:
            if self.state.source == Source.DEEZER:
                result = await asyncio.wait_for(
                    self.deezer_downloader.download_longest_track(f"{query} аудиокнига"),
                    timeout=50
                )
            else:
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
                            caption=f"📖 {track_info.artist} - {track_info.title}"
                        )
                    await status_msg.delete()
                except Exception as e:
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
            await status_msg.edit_text("⏰ Поиск занял слишком много времени")
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            await status_msg.edit_text("❌ Ошибка при поиске")
    
    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await update.message.reply_text(MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = True
        await update.message.reply_text(MESSAGES['radio_on'])
        await self.update_status_message(context, update.effective_chat.id)
    
    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await update.message.reply_text(MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = False
        await update.message.reply_text(MESSAGES['radio_off'])
        await self.update_status_message(context, update.effective_chat.id)
    
    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await update.message.reply_text(MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.last_played_time = 0
        await update.message.reply_text(MESSAGES['next_track'])
    
    async def source_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await update.message.reply_text(MESSAGES['admin_only'])
            return
        
        async with state_lock:
            sources = list(Source)
            current_index = sources.index(self.state.source)
            next_index = (current_index + 1) % len(sources)
            self.state.source = sources[next_index]
        
        message = MESSAGES['source_switched'].format(source=self.state.source.value)
        await update.message.reply_text(message)
        await self.update_status_message(context, update.effective_chat.id)
    
    async def show_proxy_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if PROXY_ENABLED:
            message = MESSAGES['proxy_enabled']
        else:
            message = MESSAGES['proxy_disabled']
        
        await update.message.reply_text(message)
    
    async def get_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.update_status_message(context, update.effective_chat.id)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        command = query.data
        if command == 'radio_on':
            await self.radio_on(update, context)
        elif command == 'radio_off':
            await self.radio_off(update, context)
        elif command == 'source_switch':
            await self.source_switch(update, context)
    
    async def update_radio_task(self, context: ContextTypes.DEFAULT_TYPE):
        async with state_lock:
            if not self.state.radio_status.is_on:
                return
        
        logger.info("Обновление радио...")
        
        genre = "lofi"  # Простой жанр
        result = None
        
        try:
            if self.state.source == Source.DEEZER:
                result = await self.deezer_downloader.download_track(f"{genre} music")
            else:
                result = await self.youtube_downloader.download_track(f"{genre} music", self.state.source)
        except:
            pass
        
        if result:
            audio_path, track_info = result
            async with state_lock:
                active_chats = list(self.state.active_chats.keys())
            
            for chat_id in active_chats:
                try:
                    with open(audio_path, 'rb') as audio_file:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_file,
                            title=track_info.title,
                            performer=track_info.artist,
                            duration=track_info.duration,
                            caption=f"📻 Радио: {genre}"
                        )
                except:
                    pass
            
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except:
                    pass
    
    async def update_status_task(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.update_status_message(context)
        except Exception as e:
            logger.error(f"Ошибка обновления статуса: {e}")
    
    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        try:
            keyboard = get_menu_keyboard()
            message_text = format_status_message(self.state)
            
            # Удаляем HTML
            import re
            message_text = re.sub(r'<[^>]+>', '', message_text)
            
            async with state_lock:
                if chat_id:
                    chats = [chat_id] if chat_id in self.state.active_chats else []
                else:
                    chats = list(self.state.active_chats.keys())
            
            for cid in chats:
                try:
                    chat_data = self.state.active_chats.get(cid)
                    
                    if chat_data and chat_data.status_message_id:
                        await context.bot.edit_message_text(
                            chat_id=cid,
                            message_id=chat_data.status_message_id,
                            text=message_text,
                            reply_markup=keyboard,
                            parse_mode=None
                        )
                    else:
                        msg = await context.bot.send_message(
                            chat_id=cid,
                            text=message_text,
                            reply_markup=keyboard,
                            parse_mode=None
                        )
                        async with state_lock:
                            if cid in self.state.active_chats:
                                self.state.active_chats[cid].status_message_id = msg.message_id
                except BadRequest:
                    pass
                except Forbidden:
                    async with state_lock:
                        if cid in self.state.active_chats:
                            del self.state.active_chats[cid]
                except Exception as e:
                    logger.error(f"Ошибка для {cid}: {e}")
        except Exception as e:
            logger.error(f"Ошибка update_status_message: {e}")
    
    async def shutdown(self):
        logger.info("Завершение работы...")
        try:
            await self.youtube_downloader.close()
        except:
            pass
        try:
            await self.deezer_downloader.close()
        except:
            pass
        cleanup_temp_files()

async def main():
    if not check_environment():
        sys.exit(1)
    
    try:
        app = Application.builder().token(BOT_TOKEN).build()
        bot = MusicBot(app)
        
        bot.register_handlers()
        await bot.initialize()
        
        await app.initialize()
        await app.start()
        
        if app.updater:
            await app.updater.start_polling()
        
        logger.info("✅ Бот запущен!")
        
        # Ожидание Ctrl+C
        stop = asyncio.Event()
        try:
            await stop.wait()
        except KeyboardInterrupt:
            pass
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        logger.info("Завершение...")
        try:
            if 'app' in locals():
                if app.updater:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            if 'bot' in locals():
                await bot.shutdown()
        except:
            pass

if __name__ == "__main__":
    asyncio.run(main())