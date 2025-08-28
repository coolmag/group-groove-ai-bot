import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from config import BOT_TOKEN
from models import BotState, Source, TrackInfo
from utils import get_menu_text, get_menu_keyboard
from locks import state_lock
from downloader import TrackInfoExtractor
from voice_engine import VoiceEngine

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- Основной класс бота ---
class MusicBot:
    def __init__(self, app: Application, voice_engine: VoiceEngine):
        self.app = app
        self.voice_engine = voice_engine
        self.track_extractor = TrackInfoExtractor()

    def get_chat_state(self, context: ContextTypes.DEFAULT_TYPE) -> BotState:
        if "bot_state" not in context.chat_data:
            context.chat_data["bot_state"] = BotState()
        return context.chat_data["bot_state"]

    # --- Обработчики команд ---

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        state = self.get_chat_state(context)
        await update.message.reply_text(
            text=get_menu_text(state),
            reply_markup=get_menu_keyboard(state)
        )

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
        for i, track in enumerate(state.playlist[:10], 1):
            message += f"{i}. {track.title}\n"
        if len(state.playlist) > 10:
            message += f"\n...и еще {len(state.playlist) - 10} треков."
        await update.message.reply_text(message, parse_mode='Markdown')

    async def join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await self.voice_engine.join_chat(chat_id)
        await update.message.reply_text("✅ Подключился к голосовому чату.")

    async def leave(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await self.voice_engine.leave_chat(chat_id)
        await update.message.reply_text("👋 Отключился от голосового чата.")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        state = self.get_chat_state(context)
        # ... (логика кнопок осталась прежней, но теперь она может управлять voice_engine)
        await query.edit_message_text(text=get_menu_text(state), reply_markup=get_menu_keyboard(state))

# --- Сборка и запуск приложения ---
async def main():
    # Инициализация движка
    voice_engine = VoiceEngine()

    # Инициализация основного приложения
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app, voice_engine)

    # Регистрация обработчиков
    app.add_handler(CommandHandler("menu", bot.menu))
    app.add_handler(CommandHandler("play", bot.play))
    app.add_handler(CommandHandler("playlist", bot.playlist))
    app.add_handler(CommandHandler("join", bot.join))
    app.add_handler(CommandHandler("leave", bot.leave))
    app.add_handler(CallbackQueryHandler(bot.button_handler))

    # Запуск всего вместе
    logger.info("Starting all services...")
    try:
        await voice_engine.start()
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("All services started successfully.")
        await asyncio.Event().wait() # Работать вечно
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
