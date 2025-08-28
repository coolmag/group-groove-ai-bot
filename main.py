import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from config import BOT_TOKEN
from models import BotState, Source, TrackInfo
from utils import get_menu_text, get_menu_keyboard
from locks import state_lock
from downloader import TrackInfoExtractor
from librivox_client import LibriVoxClient
from voice_engine import VoiceEngine

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Константы для Callback Data ---
CALLBACK_PREFIX_SELECT_BOOK = "select_book_"
CALLBACK_PREFIX_ADD_CHAPTER = "add_chapter_"

# --- Основной класс бота ---
class MusicBot:
    def __init__(self, app: Application, voice_engine: VoiceEngine):
        self.app = app
        self.voice_engine = voice_engine
        self.track_extractor = TrackInfoExtractor()
        self.librivox_client = LibriVoxClient()

    def get_chat_state(self, context: ContextTypes.DEFAULT_TYPE) -> BotState:
        if "bot_state" not in context.chat_data:
            context.chat_data["bot_state"] = BotState()
        return context.chat_data["bot_state"]

    # --- Обработчики команд ---

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = self.get_chat_state(context)
        await update.message.reply_text(text=get_menu_text(state), reply_markup=get_menu_keyboard(state))

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text("Укажите название трека после /play.")
            return

        await update.message.reply_text(f"🔎 Ищу: '{query}'...")
        track_info = await self.track_extractor.extract_track_info(query, source=Source.YOUTUBE)

        if track_info:
            state = self.get_chat_state(context)
            async with state_lock:
                state.playlist.append(track_info)
                logger.info(f"Track '{track_info.title}' added to playlist for chat {update.message.chat_id}")
            await update.message.reply_text(f"✅ В очередь: <b>{track_info.title}</b>", parse_mode='HTML')
        else:
            await update.message.reply_text(f"❌ Не удалось найти: '{query}'.")

    async def playlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = self.get_chat_state(context)
        if not state.playlist:
            await update.message.reply_text("Плейлист пуст.")
            return

        message = "🎵 **Плейлист:**\n\n"
        for i, track in enumerate(state.playlist[:15], 1):
            message += f"{i}. {track.title}\n"
        if len(state.playlist) > 15:
            message += f"\n...и еще {len(state.playlist) - 15} треков."
        await update.message.reply_text(message, parse_mode='Markdown')

    async def join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await self.voice_engine.join_chat(chat_id)
        await update.message.reply_text("✅ Подключился к голосовому чату.")

    async def leave(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await self.voice_engine.leave_chat(chat_id)
        await update.message.reply_text("👋 Отключился от голосового чата.")

    async def audiobook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text("Укажите название книги после /audiobook.")
            return

        await update.message.reply_text(f"📚 Ищу аудиокниги: '{query}'...")
        books = await self.librivox_client.search_books(query)

        if not books:
            await update.message.reply_text("Не удалось найти аудиокниги по вашему запросу.")
            return

        context.chat_data['latest_audiobook_search'] = books

        keyboard = []
        for i, book in enumerate(books):
            button = InlineKeyboardButton(f"{book.title} - {book.author}", callback_data=f"{CALLBACK_PREFIX_SELECT_BOOK}{i}")
            keyboard.append([button])

        await update.message.reply_text("Вот что я нашел:", reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Обработчик кнопок ---

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data.startswith(CALLBACK_PREFIX_SELECT_BOOK):
            await self._handle_book_selection(query, context)
        elif data.startswith(CALLBACK_PREFIX_ADD_CHAPTER):
            await self._handle_chapter_selection(query, context)
        else:
            await self._handle_menu_buttons(query, context)

    # --- Приватные методы-обработчики для кнопок ---

    async def _handle_book_selection(self, query, context: ContextTypes.DEFAULT_TYPE):
        book_index = int(query.data.split("_")[-1])
        book = context.chat_data['latest_audiobook_search'][book_index]

        keyboard = []
        for i, chapter in enumerate(book.chapters[:20]):  # Ограничиваем вывод до 20 глав
            button = InlineKeyboardButton(chapter.title, callback_data=f"{CALLBACK_PREFIX_ADD_CHAPTER}{book_index}_{i}")
            keyboard.append([button])
        
        await query.edit_message_text(f"**{book.title}**\nВыберите главу:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def _handle_chapter_selection(self, query, context: ContextTypes.DEFAULT_TYPE):
        _, book_index_str, chapter_index_str = query.data.split("_")
        book_index = int(book_index_str)
        chapter_index = int(chapter_index_str)

        book = context.chat_data['latest_audiobook_search'][book_index]
        chapter = book.chapters[chapter_index]
        state = self.get_chat_state(context)

        track_info = TrackInfo(title=f"{book.title} - {chapter.title}", url=chapter.url)
        
        async with state_lock:
            state.playlist.append(track_info)
            logger.info(f"Chapter '{track_info.title}' added to playlist for chat {query.message.chat_id}")

        await query.edit_message_text(f"✅ Глава добавлена в очередь: {chapter.title}")

    async def _handle_menu_buttons(self, query, context: ContextTypes.DEFAULT_TYPE):
        state = self.get_chat_state(context)
        async with state_lock:
            if query.data == "radio_on":
                state.radio_status.is_on = True
            elif query.data == "radio_off":
                state.radio_status.is_on = False
            # Другие кнопки меню можно будет добавить сюда

        await query.edit_message_text(text=get_menu_text(state), reply_markup=get_menu_keyboard(state))

# --- Сборка и запуск приложения ---
async def main():
    voice_engine = VoiceEngine()
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app, voice_engine)

    handlers = [
        CommandHandler("menu", bot.menu),
        CommandHandler("play", bot.play),
        CommandHandler("playlist", bot.playlist),
        CommandHandler("join", bot.join),
        CommandHandler("leave", bot.leave),
        CommandHandler("audiobook", bot.audiobook),
        CallbackQueryHandler(bot.button_handler)
    ]
    app.add_handlers(handlers)

    logger.info("Starting all services...")
    try:
        await voice_engine.start()
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("All services started successfully.")
        await asyncio.Event().wait()
    finally:
        logger.info("Stopping all services...")
        await voice_engine.stop()
        await app.updater.stop()
        await app.stop()
        logger.info("All services stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
