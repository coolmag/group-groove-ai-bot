import logging
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from config import BOT_TOKEN
from state import BotState, RadioStatus, Source

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.state = BotState()

    async def menu(self, update, context):
        text = (
            f"Groove AI Radio — источник: {self.state.source.value}\n"
            f"Статус радио: {'🟢 ВКЛ' if self.state.radio_status.is_on else '🔴 ВЫКЛ'}\n"
            f"Текущий жанр: {self.state.radio_status.current_genre or '—'}\n"
            f"Трек: {self.state.radio_status.current_track or '—'}"
        )
        await update.message.reply_text(text)

def build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app)
    app.add_handler(CommandHandler("menu", bot.menu))
    return app

if __name__ == "__main__":
    app = build_app()
    logger.info("Bot starting...")
    app.run_polling()
