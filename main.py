import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.error import BadRequest, Forbidden

from config import BOT_TOKEN, BotState, MESSAGES
from radio import AudioDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message
from locks import state_lock, radio_lock

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
        self.register_handlers()
        self.job_queue.run_repeating(self.update_radio, interval=10, first=5) # Увеличим интервал для стабильности
        self.job_queue.run_repeating(self.update_status_display, interval=15, first=3)

    def register_handlers(self):
        handlers = [
            CommandHandler("start", self.show_menu),
            CommandHandler("menu", self.show_menu),
            CommandHandler("play", self.play_song),
            CommandHandler("ron", self.radio_on),
            CommandHandler("roff", self.radio_off),
            CommandHandler("next", self.next_track),
            CallbackQueryHandler(self.button_callback)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        async with state_lock:
            if chat_id not in self.state.active_chats:
                sent_message = await context.bot.send_message(chat_id=chat_id, text="Инициализация меню...")
                self.state.active_chats[chat_id] = BotState.ChatData(status_message_id=sent_message.message_id)
                logger.info(f"New chat added: {chat_id}")
        await self.update_status_display(context, chat_id)

    async def play_song(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = " ".join(context.args)
        if not query:
            await context.bot.send_message(chat_id, MESSAGES['play_usage'])
            return

        await context.bot.send_message(chat_id, MESSAGES['searching'])
        audio_path, track_info = await self.downloader.download_track(query)
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
        else:
            await context.bot.send_message(chat_id, MESSAGES['not_found'])

    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            return await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
        async with state_lock:
            self.state.radio_status.is_on = True
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_on'])
        await self.update_status_display(context, update.effective_chat.id)

    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            return await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
        async with state_lock:
            self.state.radio_status.is_on = False
        await context.bot.send_message(update.effective_chat.id, MESSAGES['radio_off'])
        await self.update_status_display(context, update.effective_chat.id)

    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_admin(update, context):
            return await context.bot.send_message(update.effective_chat.id, MESSAGES['admin_only'])
        async with radio_lock:
            self.state.radio_status.last_played_time = 0
        await context.bot.send_message(update.effective_chat.id, MESSAGES['next_track'])

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        command_map = {'radio_on': self.radio_on, 'radio_off': self.radio_off, 'next_track': self.next_track}
        if query.data in command_map:
            await command_map[query.data](update, context)

    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        async with radio_lock:
            if not self.state.radio_status.is_on:
                return
            if (asyncio.get_event_loop().time() - self.state.radio_status.last_played_time) < self.state.radio_status.cooldown:
                return

            genre = self.downloader.get_random_genre()
            logger.info(f"Radio check: time to play new track, genre: {genre}")
            audio_path, track_info = await self.downloader.download_track(f"{genre} music")
            if audio_path and track_info:
                async with state_lock:
                    self.state.radio_status.current_track = track_info
                for chat_id in list(self.state.active_chats.keys()):
                    try:
                        with open(audio_path, 'rb') as audio_file:
                            await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=track_info.title, performer=track_info.artist, duration=track_info.duration, caption=f"📻 Радио: {genre.capitalize()}")
                    except Exception as e:
                        logger.error(f"Failed to send radio track to {chat_id}: {e}")
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                async with state_lock:
                    self.state.radio_status.last_played_time = asyncio.get_event_loop().time()
            else:
                logger.warning(f"Radio could not find a track for genre: {genre}")

    async def update_status_display(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
        chats_to_update = [chat_id] if chat_id else self.state.active_chats.keys()
        for cid in chats_to_update:
            chat_data = self.state.active_chats.get(cid)
            if not chat_data or not chat_data.status_message_id:
                continue
            keyboard = await get_menu_keyboard(self.state.radio_status.is_on)
            message_text = format_status_message(self.state)
            try:
                await context.bot.edit_message_text(chat_id=cid, message_id=chat_data.status_message_id, text=message_text, reply_markup=keyboard, parse_mode='HTML')
            except (BadRequest, Forbidden) as e:
                logger.warning(f"Failed to update status for chat {cid}: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    MusicBot(app)
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()