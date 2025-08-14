import logging
import os
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# --- Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# --- Bot Commands & Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message when the command /start is issued."""
    await update.message.reply_text("Hello, World!")

async def post_init(application: Application) -> None:
    logger.info("post_init called")
    bot_commands = [
        BotCommand("start", "Start the bot"),
    ]
    await application.bot.set_my_commands(bot_commands)

def main() -> None:
    logger.info("--- Bot Starting ---")
    
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN environment variable not found.")
        return

    logger.info("BOT_TOKEN found.")
    
    if not ADMIN_ID:
        logger.warning(f"ADMIN_ID is not set.")

    logger.info("All environment variables loaded.")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Add handlers
    logger.info("Adding handlers...")
    application.add_handler(CommandHandler("start", start))
    logger.info("Handlers added.")

    logger.info("Running application.run_polling()...")
    application.run_polling()
    logger.info("--- Bot Stopped ---")

if __name__ == "__main__":
    main()