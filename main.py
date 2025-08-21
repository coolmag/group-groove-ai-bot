# -*- coding: utf-8 -*-
import logging
import asyncio
import json
import shutil
from typing import Optional

from aiohttp import web
from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollAnswerHandler,
    JobQueue,
)

from config import *
from utils import *
from radio import *
from handlers import *
from locks import *

# --- Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- State ---
def load_state() -> State:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
                return State.model_validate(data)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return State()
    logger.info("No config file found, using default state")
    return State()


# --- Bot Lifecycle ---
async def post_init(application: Application):
    logger.info("Initializing bot...")

    # --- Setup bot_data ---
    application.bot_data['status_lock'] = status_lock
    application.bot_data['refill_lock'] = refill_lock
    application.bot_data['state_lock'] = state_lock
    
    application.bot_data['state'] = load_state()
    state: State = application.bot_data['state']
    
    logger.info("Setting bot commands...")
    commands = [
        BotCommand("play", "Найти и воспроизвести трек"),
        BotCommand("menu", "Показать главное меню"),
        BotCommand("ron", "Включить радио (админ)"),
        BotCommand("roff", "Выключить радио (админ)"),
        BotCommand("skip", "Пропустить трек (админ)"),
        BotCommand("vote", "Голосование за жанр (админ)"),
        BotCommand("source", "Сменить источник: /source <yt|sc|vk> (админ)"),
        BotCommand("refresh", "Обновить статус (админ)"),
        BotCommand("reset", "Сбросить состояние бота (админ)"),
    ]
    await application.bot.set_my_commands(commands)
    
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.error("FFmpeg not found!")
        state.last_error = "FFmpeg or ffprobe not installed"
        await application.bot.send_message(RADIO_CHAT_ID, "[ERR] FFmpeg not installed!")
        return
        
    try:
        bot_info = await application.bot.get_me()
        if getattr(bot_info, 'can_read_all_group_messages', True) is False:
            logger.error("Privacy mode is enabled. Bot will not receive poll answers.")
            await application.bot.send_message(
                RADIO_CHAT_ID,
                "[ERR] **Critical Error: Privacy Mode is enabled.**\n\n" 
                "The bot cannot receive poll answers or most messages from users.\n" 
                "Please disable it via @BotFather:\n" 
                "1. Open @BotFather\n" 
                "2. Select your bot (`@Aigrooves_bot`)\n" 
                "3. Go to `Bot Settings` -> `Group Privacy`\n" 
                "4. Click '***Turn off***'.\n\n" 
                "After turning it off, please **restart the bot** on the hosting.",
                parse_mode="MarkdownV2"
            )
            return
    except Exception as e:
        logger.error(f"Could not check privacy mode: {e}")

    # Permission check removed from here as it depends on context, which is not available in post_init
        
    if state.active_poll_id:
        logger.warning("Active poll found in state on startup. Resetting due to possible bot restart.")
        state.active_poll_id = None
        state.poll_message_id = None
        state.poll_options = []

    application.job_queue.run_repeating(
        scheduled_vote_command, 
        interval=Constants.VOTING_INTERVAL_SECONDS, 
        first=10, 
        name="hourly_vote_job"
    )
    logger.info(f"Scheduled hourly vote job. First run in 10 seconds.")

    if state.is_on:
        logger.info("Starting radio loop")
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(application))
        await refill_playlist(application)
        
    logger.info("Bot initialized successfully")

async def on_shutdown(application: Application):
    logger.info("Shutting down bot...")
    
    if 'state' in application.bot_data:
        await save_state_from_botdata(application.bot_data)
    
    if 'radio_loop_task' in application.bot_data:
        application.bot_data['radio_loop_task'].cancel()
        try:
            await application.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass
    
    logger.info("Shutdown complete")

def main():
    try:
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN is not set in environment variables!")
        if not ADMIN_IDS:
            raise ValueError("ADMIN_IDS is not set in environment variables!")
        if not RADIO_CHAT_ID:
            raise ValueError("RADIO_CHAT_ID is not set in environment variables!")
        
        DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
        
        job_queue = JobQueue()

        app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).job_queue(job_queue).build()
        
        # --- Handlers ---
        app.add_handler(CommandHandler(["start", "menu", "m"], start_command))
        app.add_handler(CommandHandler(["ron", "r_on"], lambda u, c: radio_on_off_command(u, c, True)))
        app.add_handler(CommandHandler(["rof", "r_off", "stop", "t"], lambda u, c: radio_on_off_command(u, c, False)))
        app.add_handler(CommandHandler("stopbot", stop_bot_command))
        app.add_handler(CommandHandler(["skip", "s"], skip_command))
        app.add_handler(CommandHandler(["vote", "v"], vote_command))
        app.add_handler(CommandHandler(["refresh", "r"], refresh_command))
        app.add_handler(CommandHandler(["source", "src"], set_source_command))
        app.add_handler(CommandHandler(["reset"], reset_command))
        app.add_handler(CommandHandler(["play", "p"], play_command))
        app.add_handler(CallbackQueryHandler(play_button_callback, pattern=r"^play_track:"))
        app.add_handler(CallbackQueryHandler(radio_buttons_callback, pattern=r"^(radio|vote|cmd):" ))
        app.add_handler(PollAnswerHandler(handle_poll_answer))
        app.add_error_handler(error_handler)
        
        async def run_server():
            app_web = web.Application()
            app_web.router.add_get("/", health_check)
            runner = web.AppRunner(app_web)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', PORT)
            await site.start()
            logger.info(f"Health check server running on port {PORT}")
        
        loop = asyncio.get_event_loop()
        loop.create_task(run_server())
        logger.info("Starting bot polling...")
        app.run_polling()

    except Exception as e:
        print(f"FATAL: Bot failed to start: {e}")
        logger.fatal(f"Bot failed to start: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
