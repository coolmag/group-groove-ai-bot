# -*- coding: utf-8 -*-
import logging
import asyncio
import shutil

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

import config
import utils
import radio
import handlers
import locks
from utils import load_state, save_state_from_botdata

# --- Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- Bot Lifecycle ---
async def post_init(application: Application):
    logger.info("Initializing bot...")

    # --- Setup bot_data ---
    application.bot_data['status_lock'] = locks.status_lock
    application.bot_data['refill_lock'] = locks.refill_lock
    application.bot_data['state_lock'] = locks.state_lock
    
    application.bot_data['state'] = utils.load_state()
    state: config.State = application.bot_data['state']
    
    logger.info("Setting bot commands...")
    bot_commands = [
        BotCommand("play", "/p <запрос> - Найти и проиграть трек"),
        BotCommand("menu", "/m - Показать главное меню и статус"),
        BotCommand("ron", "/r_on - Включить радио (админ)"),
        BotCommand("roff", "/r_off, /stop, /t - Выключить радио (админ)"),
        BotCommand("skip", "/s - Пропустить трек (админ)"),
        BotCommand("vote", "/v - Голосование за жанр (админ)"),
        BotCommand("source", "/src <yt|sc|vk> - Сменить источник (админ)"),
        BotCommand("refresh", "/r - Обновить статус (админ)"),
        BotCommand("reset", "Сбросить состояние бота (админ)"),
    ]
    await application.bot.set_my_commands(bot_commands)
    
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.error("FFmpeg not found!")
        state.last_error = "FFmpeg or ffprobe not installed"
        await application.bot.send_message(config.RADIO_CHAT_ID, "[ERR] FFmpeg not installed!")
        return
        
    try:
        bot_info = await application.bot.get_me()
        if getattr(bot_info, 'can_read_all_group_messages', True) is False:
            logger.error("Privacy mode is enabled. Bot will not receive poll answers.")
            await application.bot.send_message(
                config.RADIO_CHAT_ID,
                "[ERR] **Critical Error: Privacy Mode is enabled.**\n\n" 
                "The bot cannot receive poll answers or most messages from users.\n" 
                "Please disable it via @BotFather.",
                parse_mode="MarkdownV2"
            )
            return
    except Exception as e:
        logger.error(f"Could not check privacy mode: {e}")

    if state.active_poll_id:
        logger.warning("Active poll found in state on startup. Resetting due to possible bot restart.")
        state.active_poll_id = None
        state.poll_message_id = None
        state.poll_options = []

    async def job_callback(context: ContextTypes.DEFAULT_TYPE):
        """Wrapper for the scheduled job to ensure context is passed correctly."""
        await handlers.scheduled_vote_command(context)

    application.job_queue.run_repeating(
        callback=job_callback, 
        interval=config.Constants.VOTING_INTERVAL_SECONDS, 
        first=10, 
        name="hourly_vote_job"
    )
    logger.info(f"Scheduled hourly vote job. First run in 10 seconds.")

    if state.is_on:
        logger.info("Starting radio loop")
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio.radio_loop(application))
        await radio.refill_playlist(application)
        
    logger.info("Bot initialized successfully")

async def on_shutdown(application: Application):
    logger.info("Shutting down bot...")
    
    if 'state' in application.bot_data:
        await utils.save_state_from_botdata(application.bot_data)
    
    if 'radio_loop_task' in application.bot_data:
        application.bot_data['radio_loop_task'].cancel()
        try:
            await application.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass
    
    logger.info("Shutdown complete")

def main():
    try:
        if not config.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is not set in environment variables!")
        if not config.ADMIN_IDS:
            raise ValueError("ADMIN_IDS is not set in environment variables!")
        if not config.RADIO_CHAT_ID:
            raise ValueError("RADIO_CHAT_ID is not set in environment variables!")
        
        config.DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
        
        job_queue = JobQueue()

        app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).job_queue(job_queue).build()
        
        # --- Handlers ---
        app.add_handler(CommandHandler(["start", "menu", "m"], handlers.start_command))
        app.add_handler(CommandHandler(["ron", "r_on"], lambda u, c: handlers.radio_on_off_command(u, c, True)))
        app.add_handler(CommandHandler(["rof", "r_off", "stop", "t"], lambda u, c: handlers.radio_on_off_command(u, c, False)))
        app.add_handler(CommandHandler("stopbot", handlers.stop_bot_command))
        app.add_handler(CommandHandler(["skip", "s"], handlers.skip_command))
        app.add_handler(CommandHandler(["vote", "v"], handlers.vote_command))
        app.add_handler(CommandHandler(["refresh", "r"], handlers.refresh_command))
        app.add_handler(CommandHandler(["source", "src"], handlers.set_source_command))
        app.add_handler(CommandHandler(["reset"], handlers.reset_command))
        app.add_handler(CommandHandler(["play", "p"], handlers.play_command))
        app.add_handler(CommandHandler("keyboard", handlers.admin_keyboard_command))
        app.add_handler(CommandHandler("nokeyboard", handlers.remove_keyboard_command))
        app.add_handler(CallbackQueryHandler(handlers.play_button_callback, pattern=r"^play_track:"))
        app.add_handler(CallbackQueryHandler(handlers.radio_buttons_callback, pattern=r"^(radio|vote|cmd):" ))
        app.add_handler(PollAnswerHandler(handlers.handle_poll_answer))
        app.add_error_handler(handlers.error_handler)
        
        async def run_server():
            app_web = web.Application()
            app_web.router.add_get("/", handlers.health_check)
            runner = web.AppRunner(app_web)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', config.PORT)
            await site.start()
            logger.info(f"Health check server running on port {config.PORT}")
        
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