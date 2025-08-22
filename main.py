import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.error import BadRequest, Forbidden

# Импортируем наши модули
from config import BOT_TOKEN, BotState, MESSAGES
from radio import AudioDownloadManager
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

        # Регистрация обработчиков
        self.register_handlers()

        # Запуск фоновых задач
        self.job_queue.run_repeating(self.update_radio, interval=5, first=10)
        self.job_queue.run_repeating(self.update_status_message, interval=10, first=5)

    def register_handlers(self):
        handlers = [
            CommandHandler("start", self.start),
            CommandHandler("menu", self.show_menu),
            CommandHandler("play", self.play_song),
            CommandHandler("ron", self.radio_on),
            CommandHandler("roff", self.radio_off),
            CommandHandler("next", self.next_track),
            CommandHandler("source", self.source_switch),
            CallbackQueryHandler(self.button_callback)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_menu(update, context)

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id

        async with state_lock:
            if chat_id not in self.state.active_chats:
                # Создаем новую запись о чате и сохраняем ID сообщения со статусом
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text="Инициализация...", # Временный текст
                    parse_mode='HTML'
                )
                self.state.active_chats[chat_id] = BotState.ChatData(status_message_id=sent_message.message_id)
                logger.info(f"New chat added: {chat_id}, status message_id: {sent_message.message_id}")
        
        # Теперь обновляем это сообщение
        await self.update_status_message(context, chat_id)


    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = " ".join(context.args)
        if not query:
            await context.bot.send_message(chat_id, MESSAGES['play_usage'])
            return

        await context.bot.send_message(chat_id, MESSAGES['searching'])
        
        audio_path, track_info = await self.downloader.download_track(query, self.state.source)
        
        if audio_path and track_info:
            try:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=open(audio_path, 'rb'),
                    title=track_info.title,
                    performer=track_info.artist,
                    duration=track_info.duration,
                    caption=f"🎵 Заказал: {update.effective_user.first_name}"
                )
            finally:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                    logger.info(f"File deleted: {audio_path}")
        else:
            await context.bot.send_message(chat_id, MESSAGES['not_found'])

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
            current_source_index = list(self.state.source.__class__).index(self.state.source)
            next_source_index = (current_source_index + 1) % len(self.state.source.__class__)
            self.state.source = list(self.state.source.__class__)[next_source_index]
            message = MESSAGES['source_switched'].format(source=self.state.source.value)
        
        await context.bot.send_message(update.effective_chat.id, message)
        await self.update_status_message(context, update.effective_chat.id)

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        command = query.data

        # Сопоставляем callback_data с методами
        commands = {
            'radio_on': self.radio_on,
            'radio_off': self.radio_off,
            'next_track': self.next_track,
            'source_switch': self.source_switch,
        }
        if command in commands:
            # We need to pass the original update to the command handlers
            # so they can properly check for admin rights and reply.
            await commands[command](update, context)

    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        async with radio_lock:
            if not self.state.radio_status.is_on:
                return

            current_time = asyncio.get_event_loop().time()
            if (current_time - self.state.radio_status.last_played_time) < self.state.radio_status.cooldown:
                return

            logger.info("Radio check: time to play new track.")
            
            genre = self.downloader.get_random_genre()
            async with state_lock:
                self.state.radio_status.current_genre = genre

            audio_path, track_info = await self.downloader.download_track(f"{genre} music", self.state.source)

            if audio_path and track_info:
                async with state_lock:
                    self.state.radio_status.current_track = track_info
                
                for chat_id in list(self.state.active_chats.keys()):
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
                    except Exception as e:
                        logger.error(f"Failed to send radio track to {chat_id}: {e}")
                
                # Delete the file ONLY after the loop is finished
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                    logger.info(f"File deleted: {audio_path}")

                async with state_lock:
                    self.state.radio_status.last_played_time = current_time
            else:
                logger.warning(f"Radio could not find a track for genre: {genre}")

    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        keyboard = await get_menu_keyboard()
        message_text = format_status_message(self.state)
        
        chats_to_update = [chat_id] if chat_id else list(self.state.active_chats.keys())

        for cid in chats_to_update:
            chat_data = self.state.active_chats.get(cid)
            if not chat_data or not chat_data.status_message_id:
                continue

            try:
                await context.bot.edit_message_text(
                    chat_id=cid,
                    message_id=chat_data.status_message_id,
                    text=message_text,
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
            except BadRequest:
                logger.warning(f"Failed to update status for chat {cid}: message not found or not modified.")
            except Forbidden:
                logger.warning(f"Bot is blocked in chat {cid}. Can't update status.")
            except Exception as e:
                logger.error(f"Unexpected error updating status for {cid}: {e}")

def main():
    """Запускает бота."""
    app = Application.builder().token(BOT_TOKEN).build()

    # Создаем экземпляр нашего бота
    MusicBot(app)

    logger.info("Bot starting...")
    # Запускаем бота до тех пор, пока пользователь не нажмет Ctrl-C
    app.run_polling()

if __name__ == "__main__":
    main()