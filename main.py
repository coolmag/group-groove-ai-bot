import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.error import BadRequest, Forbidden

# Импортируем наши модули
from config import BOT_TOKEN, BotState, MESSAGES, check_environment, PROXY_ENABLED, PROXY_URL
from downloader import AudioDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message
from locks import state_lock, radio_lock

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.job_queue: JobQueue = self.app.job_queue
        self.downloader = AudioDownloadManager()
        self.state = BotState()
        
        # Принудительно включаем радио для тестирования
        self.state.radio_status.is_on = True
        self.state.radio_status.last_played_time = 0
        
        logger.info(f"Initialized bot with state: {self.state}")
        
        if PROXY_ENABLED and PROXY_URL:
            logger.info(f"Proxy enabled: {PROXY_URL}")
        
        # Регистрация обработчиков
        self.register_handlers()
        
        # Регистрация обработчика ошибок
        self.app.add_error_handler(self.on_error)

        # Запуск фоновых задач
        self.job_queue.run_repeating(self.update_radio, interval=60, first=10)  # Каждую минуту
        self.job_queue.run_repeating(self.update_status_message, interval=30, first=5)  # Каждые 30 секунд

    def register_handlers(self):
        handlers = [
            CommandHandler("start", self.start),
            CommandHandler("menu", self.show_menu),
            CommandHandler("play", self.play_song),
            CommandHandler(["ron", "radio_on"], self.radio_on),
            CommandHandler(["roff", "radio_off"], self.radio_off),
            CommandHandler("next", self.next_track),
            CommandHandler("source", self.source_switch),
            CommandHandler("proxy", self.toggle_proxy),
            CallbackQueryHandler(self.button_callback)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def on_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"Update {update} caused error {context.error}")
        
        if update and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Произошла ошибка при обработке запроса. Попробуйте позже."
                )
            except Exception:
                logger.error("Failed to send error message")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(MESSAGES['welcome'])
        await self.show_menu(update, context)

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id

        async with state_lock:
            if chat_id not in self.state.active_chats:
                # Создаем новую запись о чате
                self.state.active_chats[chat_id] = BotState.ChatData(status_message_id=None)
                logger.info(f"New chat added: {chat_id}")
        
        # Отправляем или обновляем статус сообщение
        await self.update_status_message(context, chat_id)

    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = " ".join(context.args)
        if not query:
            await context.bot.send_message(chat_id, MESSAGES['play_usage'])
            return

        status_msg = await context.bot.send_message(chat_id, MESSAGES['searching'])
        
        try:
            audio_path, track_info = await self.downloader.download_track(query, self.state.source)
            
            if audio_path and track_info:
                try:
                    with open(audio_path, 'rb') as audio_file:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio_file,
                            title=track_info.title,
                            performer=track_info.artist,
                            duration=track_info.duration,
                            caption=f"🎵 {track_info.artist} - {track_info.title} (источник: {track_info.source})"
                        )
                    await status_msg.delete()
                finally:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                        logger.info(f"File deleted: {audio_path}")
            else:
                await status_msg.edit_text(MESSAGES['not_found'])
        except Exception as e:
            logger.error(f"Error in play_song: {e}")
            await status_msg.edit_text("❌ Ошибка при загрузке трека")

    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with state_lock:
            self.state.radio_status.is_on = True
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_on'])
        await self.update_status_message(context, update.effective_chat.id)

    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return

        async with state_lock:
            self.state.radio_status.is_on = False
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_off'])
        await self.update_status_message(context, update.effective_chat.id)

    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        async with radio_lock:
            self.state.radio_status.last_played_time = 0
        await context.bot.send_message(update.effective_chat.id, MESSAGES['next_track'])

    async def source_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    async def toggle_proxy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
            return
        
        # Это просто демонстрация - в реальности нужно перезагружать бота для применения изменений прокси
        if PROXY_ENABLED:
            message = MESSAGES['proxy_enabled']
        else:
            message = MESSAGES['proxy_disabled']
            
        await context.bot.send_message(update.effective_chat.id, message)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        command = query.data

        commands = {
            'radio_on': self.radio_on,
            'radio_off': self.radio_off,
            'next_track': self.next_track,
            'source_switch': self.source_switch,
        }
        
        if command in commands:
            await commands[command](update, context)

    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Radio update started")
        
        async with radio_lock:
            logger.info(f"Radio status: {self.state.radio_status.is_on}")
            
            if not self.state.radio_status.is_on:
                logger.info("Radio is off, skipping update")
                return

            current_time = asyncio.get_event_loop().time()
            logger.info(f"Current time: {current_time}, Last played: {self.state.radio_status.last_played_time}")
            
            if (current_time - self.state.radio_status.last_played_time) < self.state.radio_status.cooldown:
                logger.info("Cooldown active, skipping update")
                return

            logger.info("Attempting to download track for radio")
            
            genre = self.downloader.get_random_genre()
            async with state_lock:
                self.state.radio_status.current_genre = genre

            # Пытаемся скачать трек
            audio_path, track_info = await self.downloader.download_track(f"{genre} music", self.state.source)

            if audio_path and track_info:
                logger.info(f"Successfully downloaded: {track_info.title}")
                
                async with state_lock:
                    self.state.radio_status.current_track = track_info
                
                # Отправляем трек во все активные чаты
                for chat_id in list(self.state.active_chats.keys()):
                    try:
                        with open(audio_path, 'rb') as audio_file:
                            await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=audio_file,
                                title=track_info.title,
                                performer=track_info.artist,
                                duration=track_info.duration,
                                caption=f"📻 Радио: {genre.capitalize()} (источник: {track_info.source})"
                            )
                        logger.info(f"Track sent to chat {chat_id}")
                    except Exception as e:
                        logger.error(f"Failed to send radio track to {chat_id}: {e}")
                
                # Удаляем файл после отправки
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                    logger.info(f"File deleted: {audio_path}")

                async with state_lock:
                    self.state.radio_status.last_played_time = current_time
            else:
                logger.warning(f"Radio could not find a track for genre: {genre}")
                # Устанавливаем задержку при ошибке
                async with state_lock:
                    self.state.radio_status.last_played_time = current_time - self.state.radio_status.cooldown + 30

    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        keyboard = get_menu_keyboard()
        message_text = format_status_message(self.state)
        
        chats_to_update = [chat_id] if chat_id else list(self.state.active_chats.keys())

        for cid in chats_to_update:
            chat_data = self.state.active_chats.get(cid)
            
            try:
                if chat_data and chat_data.status_message_id:
                    # Пытаемся обновить существующее сообщение
                    await context.bot.edit_message_text(
                        chat_id=cid,
                        message_id=chat_data.status_message_id,
                        text=message_text,
                        reply_markup=keyboard,
                        parse_mode='HTML'
                    )
                else:
                    # Создаем новое сообщение
                    sent_message = await context.bot.send_message(
                        chat_id=cid,
                        text=message_text,
                        reply_markup=keyboard,
                        parse_mode='HTML'
                    )
                    # Сохраняем ID сообщения
                    async with state_lock:
                        if cid in self.state.active_chats:
                            self.state.active_chats[cid].status_message_id = sent_message.message_id
                            logger.info(f"Created status message for chat {cid}: {sent_message.message_id}")
            except BadRequest as e:
                if "message not found" in str(e).lower():
                    # Сообщение было удалено, создаем новое
                    async with state_lock:
                        if cid in self.state.active_chats:
                            self.state.active_chats[cid].status_message_id = None
                    logger.warning(f"Status message not found for chat {cid}, will create new one")
                else:
                    logger.warning(f"Failed to update status for chat {cid}: {e}")
            except Forbidden:
                logger.warning(f"Bot is blocked in chat {cid}. Removing from active chats.")
                async with state_lock:
                    if cid in self.state.active_chats:
                        del self.state.active_chats[cid]
            except Exception as e:
                logger.error(f"Unexpected error updating status for {cid}: {e}")

    async def shutdown(self):
        """Очистка ресурсов при завершении"""
        await self.downloader.close()

def main():
    """Запускает бота."""
    if not check_environment():
        return
    
    app = Application.builder().token(BOT_TOKEN).build()

    # Создаем экземпляр нашего бота
    bot = MusicBot(app)

    # Добавляем обработчик завершения
    import signal
    import functools
    
    def signal_handler(app, signal_name):
        logger.info(f"Received signal {signal_name}, shutting down...")
        loop = asyncio.get_event_loop()
        loop.create_task(bot.shutdown())
    
    signal.signal(signal.SIGTERM, functools.partial(signal_handler, app))
    signal.signal(signal.SIGINT, functools.partial(signal_handler, app))

    logger.info("Bot starting...")
    # Запускаем бота
    app.run_polling()

if __name__ == "__main__":
    main()