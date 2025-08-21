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

# --- Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- Bot Lifecycle ---
async def post_init(application: Application):
    logger.info("Initializing bot...")
    application.bot_data['status_lock'] = locks.status_lock
    application.bot_data['refill_lock'] = locks.refill_lock
    application.bot_data['state'] = utils.load_state()
    state: config.State = application.bot_data['state']
    
    bot_commands = [
        BotCommand("play", "/p <запрос> - Найти и проиграть трек"),
        BotCommand("menu", "/m - Показать главное меню и статус"),
        BotCommand("ron", "/r_on - Включить радио (админ)"),
        BotCommand("roff", "/r_off, /stop, /t - Выключить радио (админ)"),
        BotCommand("skip", "/s - Пропустить трек (админ)"),
        BotCommand("vote", "/v - Голосование за жанр (админ)"),
        BotCommand("source", "/src <yt|sc|vk|ar> - Сменить источник (админ)"),
        BotCommand("refresh", "/r - Обновить статус панель (админ)"),
        BotCommand("keyboard", "Показать/скрыть клавиатуру (админ)"),
        BotCommand("reset", "Сбросить состояние бота (админ)"),
        BotCommand("stopbot", "Остановить бота (админ)"),
    ]
    await application.bot.set_my_commands(bot_commands)
    
    if state.is_on:
        logger.info("Radio was on at startup, resuming...")
        application.bot_data['radio_loop_task'] = asyncio.create_task(radio.radio_loop(application))
    
    async def job_callback(context: ContextTypes.DEFAULT_TYPE):
        """Wrapper for the scheduled job to ensure context is passed correctly."""
        logger.info(f"Job callback called. Context type: {type(context)}")
        await handlers.scheduled_vote_command(context)

    application.job_queue.run_repeating(job_callback, interval=config.Constants.VOTING_INTERVAL_SECONDS, first=10, name="hourly_vote_job")
    logger.info("Bot initialized successfully")

async def on_shutdown(application: Application):
    logger.info("Shutting down bot...")
    if 'state' in application.bot_data:
        await utils.save_state_from_botdata(application.bot_data)
    if 'radio_loop_task' in application.bot_data and not application.bot_data['radio_loop_task'].done():
        application.bot_data['radio_loop_task'].cancel()

def main():
    try:
        if not all([config.BOT_TOKEN, config.ADMIN_IDS, config.RADIO_CHAT_ID]):
            raise ValueError("One or more required environment variables are not set!")
        config.DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
        
        app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).build()
        
        # --- Handlers ---
        app.add_handler(CommandHandler(["start", "menu", "m"], handlers.start_command))
        app.add_handler(CommandHandler(["ron", "r_on"], lambda u, c: handlers.radio_on_off_command(u, c, True)))
        app.add_handler(CommandHandler(["roff", "r_off", "stop", "t"], lambda u, c: handlers.radio_on_off_command(u, c, False)))
        app.add_handler(CommandHandler("stopbot", handlers.stop_bot_command))
        app.add_handler(CommandHandler(["skip", "s"], handlers.skip_command))
        app.add_handler(CommandHandler(["vote", "v"], handlers.vote_command))
        app.add_handler(CommandHandler(["refresh", "r"], handlers.refresh_command))
        app.add_handler(CommandHandler(["source", "src"], handlers.set_source_command))
        app.add_handler(CommandHandler(["reset"], handlers.reset_command))
        app.add_handler(CommandHandler(["play", "p"], handlers.play_command))
        app.add_handler(CommandHandler(["keyboard", "kb"], handlers.admin_keyboard_command))
        app.add_handler(CallbackQueryHandler(handlers.play_button_callback, pattern=r"^play_track:"))
        app.add_handler(CallbackQueryHandler(handlers.radio_buttons_callback, pattern=r"^(radio|vote|cmd):" ))
        app.add_handler(PollAnswerHandler(handlers.handle_poll_answer))
        app.add_error_handler(handlers.error_handler)
        
        # --- Health Check Server ---
        async def run_server():
            server_app = web.Application()
            server_app.router.add_get("/", handlers.health_check)
            runner = web.AppRunner(server_app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', config.PORT)
            await site.start()
            logger.info(f"Health check server running on port {config.PORT}")
        
        loop = asyncio.get_event_loop()
        loop.create_task(run_server())
        logger.info("Starting bot polling...")
        app.run_polling()

    except Exception as e:
        logger.fatal(f"Bot failed to start: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
