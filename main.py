import logging
import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import BOT_TOKEN
from downloader import AudioDownloader

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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Отправь мне команду /play <название песни>, "
            "и я найду, скачаю и пришлю ее тебе."
        )

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text("Пожалуйста, укажите название трека после /play.")
            return

        msg = await update.message.reply_text(f"🔎 Ищу и скачиваю: '{query}'...")

        try:
            # Запускаем скачивание в отдельном потоке, чтобы не блокировать бота
            loop = asyncio.get_running_loop()
            filepath = await loop.run_in_executor(
                None, self.downloader.download_audio, query
            )

            if filepath and os.path.exists(filepath):
                await msg.edit_text("📤 Отправляю аудио...")
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=open(filepath, 'rb'),
                    title=query,
                    write_timeout=60 # Увеличиваем таймаут для больших файлов
                )
                await msg.delete()
            else:
                await msg.edit_text(f"❌ Не удалось найти или скачать трек: '{query}'.")

        except Exception as e:
            logger.error(f"Error in /play command: {e}")
            await msg.edit_text("Произошла внутренняя ошибка.")
        finally:
            # Очистка файла после отправки
            if 'filepath' in locals() and filepath and os.path.exists(filepath):
                os.remove(filepath)

# --- Сборка и запуск приложения ---
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app)

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("play", bot.play))

    logger.info("Bot starting in simple mode...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot started successfully.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")