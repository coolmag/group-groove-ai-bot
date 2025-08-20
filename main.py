# -*- coding: utf-8 -*-
import logging
import asyncio
import json
import random
import shutil
import re
import yt_dlp
from typing import List, Optional, Deque
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollAnswerHandler,
    JobQueue,
)
from telegram.error import BadRequest, TelegramError
from functools import wraps
from asyncio import Lock
from config import *
from utils import *
from radio import *
from handlers import *

# --- Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

state_lock = Lock()
status_lock = Lock()
refill_lock = Lock()

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

async def save_state_from_botdata(bot_data: dict):
    async with state_lock:
        state: Optional[State] = bot_data.get('state')
        if state:
            try:
                CONFIG_FILE.write_text(state.model_dump_json(indent=4))
                logger.debug("State saved to config file")
            except Exception as e:
                logger.error(f"Failed to save state: {e}")





# --- Bot Lifecycle ---
async def check_bot_permissions(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the bot is an administrator in the radio chat."""
    try:
        bot_id = context.bot.id
        chat_member = await context.bot.get_chat_member(RADIO_CHAT_ID, bot_id)
        logger.info(f"DEBUG: Received chat_member object: {chat_member}")

        if chat_member.status == "administrator":
            logger.info("Bot is an administrator. Permission check passed.")
            if not getattr(chat_member, 'can_manage_messages', False):
                logger.warning("Bot is admin but lacks 'can_manage_messages'. Status panel deletion might fail.")
            return True
        else:
            logger.error(f"Bot is not an administrator in chat {RADIO_CHAT_ID}. Current status: {chat_member.status}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Bot is not an administrator. Current status: {chat_member.status}. Please grant admin rights.")
            return False

    except Exception as e:
        logger.error(f"Unexpected error during permission check for chat {RADIO_CHAT_ID}: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Unexpected error during permission check: {e}")
        return False

async def post_init(application: Application):
    logger.info("Initializing bot...")
    
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
        BotCommand("source", "Сменить источник: /source youtube (админ)"),
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

    if not await check_bot_permissions(application):
        logger.error("Permission check failed! See chat for details.")
        state.last_error = "Bot lacks required permissions"
        return
        
    if state.active_poll_id:
        logger.warning("Active poll found in state on startup. Resetting due to possible bot restart.")
        state.active_poll_id = None
        state.poll_message_id = None
        state.poll_options = []

    application.job_queue.run_repeating(
        vote_command, 
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
        try:
            CONFIG_FILE.write_text(application.bot_data['state'].model_dump_json(indent=4))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
    if 'radio_loop_task' in application.bot_data:
        application.bot_data['radio_loop_task'].cancel()
        try:
            await application.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass
    
    logger.info("Shutdown complete")

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set!")
    if not ADMIN_IDS:
        raise ValueError("ADMIN_IDS not set!")
    if not RADIO_CHAT_ID:
        raise ValueError("RADIO_CHAT_ID not set!")
    
    DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
    
    job_queue = JobQueue()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(on_shutdown).job_queue(job_queue).build()
    
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
    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
