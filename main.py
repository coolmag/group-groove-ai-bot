# main.py (v8 фикс)
import logging
import asyncio
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from downloader import AudioDownloadManager
from config import BOT_TOKEN, GENRES, Source, BotState, RadioStatus
from utils import get_menu_keyboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.state = BotState()

        # Планировщик (apscheduler встроен в Application.job_queue)
        jq = app.job_queue
        jq.run_repeating(self.update_radio, interval=60, first=5, name="radio_loop")
        jq.run_repeating(self.update_status_message, interval=30, first=10, name="status_loop")

        app.add_handler(CommandHandler("menu", self.menu))
        app.add_handler(CallbackQueryHandler(self.button))

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id not in self.state.active_chats:
            self.state.active_chats[chat_id] = None
            logger.info(f"New chat added: {chat_id}")

        keyboard = get_menu_keyboard(self.state)
        await update.message.reply_text(
            f"Groove AI Radio — источник: {self.state.source.value}",
            reply_markup=keyboard
        )

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        parts = data.split("_")  # фикс вместо context.split()

        if data == "radio_on":
            self.state.radio_status.is_on = True
        elif data == "radio_off":
            self.state.radio_status.is_on = False
        elif data == "next_track":
            await self.play_next_track(query.message.chat_id)
        elif data == "source_switch":
            self.state.source = (
                Source.VKMUSIC if self.state.source == Source.YOUTUBE else Source.YOUTUBE
            )
        elif data == "vote_now":
            self.state.voting_active = True

        # Обновляем меню
        keyboard = get_menu_keyboard(self.state)
        await query.message.edit_text(
            f"Groove AI Radio — источник: {self.state.source.value}
"
            f"Статус радио: {'🟢 ВКЛ' if self.state.radio_status.is_on else '🔴 ВЫКЛ'}
"
            f"Текущий жанр: {self.state.radio_status.current_genre or '-'}
"
            f"Трек: {self.state.radio_status.current_track or '—'}",
            reply_markup=keyboard
        )

    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Radio update started")
        if not self.state.radio_status.is_on:
            return

        now = time.monotonic()
        if now - self.state.radio_status.last_played_time < self.state.radio_status.cooldown:
            return

        genre = self.state.radio_status.current_genre or "Lo-fi"
        logger.info(f"Attempting to download track for radio ({genre})")

        track = await AudioDownloadManager.download_track(f"{genre} music", self.state.source)
        if track:
            self.state.radio_status.current_track = track.title
            self.state.radio_status.last_played_time = now
            logger.info(f"Playing {track.title}")

    async def play_next_track(self, chat_id: int):
        genre = self.state.radio_status.current_genre or "Lo-fi"
        track = await AudioDownloadManager.download_track(f"{genre} music", self.state.source)
        if track:
            self.state.radio_status.current_track = track.title
            self.state.radio_status.last_played_time = time.monotonic()
            logger.info(f"Next track: {track.title}")

    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE):
        for chat_id in self.state.active_chats.keys():
            keyboard = get_menu_keyboard(self.state)
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Groove AI Radio — источник: {self.state.source.value}
"
                        f"Статус радио: {'🟢 ВКЛ' if self.state.radio_status.is_on else '🔴 ВЫКЛ'}
"
                        f"Текущий жанр: {self.state.radio_status.current_genre or '-'}
"
                        f"Трек: {self.state.radio_status.current_track or '—'}"
                    ),
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Failed to update status for chat {chat_id}: {e}")

def build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    MusicBot(app)
    return app

if __name__ == "__main__":
    application = build_app()
    logger.info("Bot starting...")
    application.run_polling()
