import logging
import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import BOT_TOKEN
from downloader import SmartDownloader

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Основной класс бота ---
class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.downloader = SmartDownloader()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я умею присылать музыку и аудиокниги.\n"
            "Используй /play <песня> или /audiobook <книга>."
        )

    async def _process_media_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE, search_type: str):
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text(f"Пожалуйста, укажите название после команды /{search_type}.")
            return

        msg = await update.message.reply_text(f"🔎 Ищу: '{query}'...")

        try:
            loop = asyncio.get_running_loop()
            media_data = await loop.run_in_executor(
                None, self.downloader.download_media, query, search_type
            )

            if media_data and os.path.exists(media_data['filepath']):
                await msg.edit_text("📤 Отправляю файл...")
                filepath = media_data['filepath']
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=open(filepath, 'rb'),
                    title=media_data['title'],
                    performer=media_data['artist'],
                    filename=media_data['filename'],
                    duration=media_data['duration'],
                    write_timeout=120 # Увеличиваем таймаут еще больше для аудиокниг
                )
                await msg.delete() # Удаляем сообщение с индикатором поиска
            else:
                await msg.edit_text(f"❌ Не удалось найти подходящий медиафайл для: '{query}'.")

        except Exception as e:
            logger.error(f"Error in /{search_type} command: {e}")
            await msg.edit_text("Произошла внутренняя ошибка.")
        finally:
            if 'media_data' in locals() and media_data and os.path.exists(media_data['filepath']):
                os.remove(media_data['filepath'])

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._process_media_request(update, context, search_type='music')

    async def audiobook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._process_media_request(update, context, search_type='audiobook')

# --- Сборка и запуск приложения ---
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app)

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("play", bot.play))
    app.add_handler(CommandHandler("audiobook", bot.audiobook))

    logger.info("Bot starting...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Bot started successfully.")
        await asyncio.Event().wait() # Работать вечно
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
