import logging
import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from config import BOT_TOKEN
from downloader import AudioDownloader
from librivox_client import LibriVoxClient
from models import AudioBook, TrackInfo # Импортируем нужные модели

# --- Константы для Callback Data ---
CALLBACK_PREFIX_SELECT_BOOK = "select_book_"
CALLBACK_PREFIX_ADD_CHAPTER = "add_chapter_"

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Основной класс бота ---
class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.downloader = AudioDownloader()
        self.librivox_client = LibriVoxClient()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я умею присылать музыку и аудиокниги.\n"
            "Используй /play <песня> или /audiobook <книга>."
        )

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text("Пожалуйста, укажите название трека после /play.")
            return

        msg = await update.message.reply_text(f"🔎 Ищу и скачиваю: '{query}'...")

        try:
            loop = asyncio.get_running_loop()
            track_data = await loop.run_in_executor(None, self.downloader.download_audio, query)

            if track_data and os.path.exists(track_data['filepath']):
                await msg.edit_text("📤 Отправляю аудио...")
                filepath = track_data['filepath']
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=open(filepath, 'rb'),
                    title=track_data['title'],
                    performer=track_data['artist'],
                    filename=track_data['filename'],
                    duration=track_data['duration'],
                    write_timeout=60
                )
                await msg.delete()
            else:
                await msg.edit_text(f"❌ Не удалось найти или скачать трек: '{query}'.")

        except Exception as e:
            logger.error(f"Error in /play command: {e}")
            await msg.edit_text("Произошла внутренняя ошибка.")
        finally:
            if 'track_data' in locals() and track_data and os.path.exists(track_data['filepath']):
                os.remove(track_data['filepath'])

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

        context.chat_data['audiobooks'] = books
        keyboard = []
        for i, book in enumerate(books):
            button = InlineKeyboardButton(f"{book.title} - {book.author}", callback_data=f"{CALLBACK_PREFIX_SELECT_BOOK}{i}")
            keyboard.append([button])

        await update.message.reply_text("Вот что я нашел:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data.startswith(CALLBACK_PREFIX_SELECT_BOOK):
            book_index = int(data.split("_")[-1])
            book = context.chat_data['audiobooks'][book_index]
            context.chat_data['selected_book'] = book

            keyboard = []
            for i, chapter in enumerate(book.chapters[:20]):
                button = InlineKeyboardButton(chapter.title, callback_data=f"{CALLBACK_PREFIX_ADD_CHAPTER}{i}")
                keyboard.append([button])
            
            await query.edit_message_text(f"**{book.title}**\nВыберите главу:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

        elif data.startswith(CALLBACK_PREFIX_ADD_CHAPTER):
            chapter_index = int(data.split("_")[-1])
            book = context.chat_data['selected_book']
            chapter = book.chapters[chapter_index]

            await query.edit_message_text(f"Скачиваю главу: {chapter.title}")
            loop = asyncio.get_running_loop()
            track_data = await loop.run_in_executor(None, self.downloader.download_audio, chapter.url)

            if track_data and os.path.exists(track_data['filepath']):
                await query.message.reply_audio(
                    audio=open(track_data['filepath'], 'rb'),
                    title=chapter.title,
                    performer=book.author,
                    filename=f"{chapter.title}.mp3",
                    duration=track_data['duration'],
                    write_timeout=60
                )
                os.remove(track_data['filepath'])
                await query.delete_message() # Удаляем сообщение с кнопками
            else:
                await query.edit_message_text("❌ Не удалось скачать главу.")

# --- Сборка и запуск приложения ---
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app)

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("play", bot.play))
    app.add_handler(CommandHandler("audiobook", bot.audiobook))
    app.add_handler(CallbackQueryHandler(bot.button_handler))

    logger.info("Bot starting...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Bot started successfully.")
        await asyncio.Event().wait()  # Работать вечно
    finally:
        logger.info("Shutting down bot...")
        await app.updater.stop()
        await app.stop()
        logger.info("Bot has been shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")