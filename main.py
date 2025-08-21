# -*- coding: utf-8 -*-
import logging
import asyncio
import shutil
import time
import random
import os
from typing import List

from aiohttp import web
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollAnswerHandler,
    JobQueue,
)
from telegram.error import TelegramError

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


class MusicBot:
    def __init__(self, token: str):
        self.app = Application.builder().token(token).build()
        self.state: config.State = utils.load_state()
        self.downloader = radio.AudioDownloadManager()
        
        # Put self into bot_data so handlers can access this instance
        self.app.bot_data['music_bot'] = self
        self.app.bot_data['status_lock'] = locks.status_lock
        self.app.bot_data['refill_lock'] = locks.refill_lock

    def register_handlers(self):
        self.app.add_handler(CommandHandler(["start", "menu", "m"], handlers.start_command))
        self.app.add_handler(CommandHandler(["ron", "r_on"], lambda u, c: self.radio_on_off_command(u, c, turn_on=True)))
        self.app.add_handler(CommandHandler(["roff", "r_off", "stop", "t"], lambda u, c: self.radio_on_off_command(u, c, turn_on=False)))
        self.app.add_handler(CommandHandler("stopbot", handlers.stop_bot_command))
        self.app.add_handler(CommandHandler(["skip", "s"], handlers.skip_command))
        self.app.add_handler(CommandHandler(["vote", "v"], handlers.vote_command))
        self.app.add_handler(CommandHandler(["source", "src"], handlers.set_source_command))
        self.app.add_handler(CommandHandler(["reset"], handlers.reset_command))
        self.app.add_handler(CommandHandler(["play", "p"], handlers.play_command))
        self.app.add_handler(CommandHandler(["keyboard", "kb"], handlers.admin_keyboard_command))
        self.app.add_handler(CallbackQueryHandler(handlers.play_button_callback, pattern=r"^play_track:"))
        self.app.add_handler(CallbackQueryHandler(handlers.radio_buttons_callback, pattern=r"^(radio|vote|cmd):" ))
        self.app.add_handler(PollAnswerHandler(handlers.handle_poll_answer))
        self.app.add_error_handler(handlers.error_handler)

    async def post_init(self):
        logger.info("Initializing bot...")
        await self.set_bot_commands()
        if self.state.is_on:
            logger.info("Radio was on at startup, resuming...")
            self.app.bot_data['radio_loop_task'] = asyncio.create_task(self.radio_loop())
        self.app.job_queue.run_repeating(self.scheduled_vote_command, interval=config.Constants.VOTING_INTERVAL_SECONDS, first=10, name="hourly_vote_job")
        logger.info("Bot initialized successfully")

    async def on_shutdown(self):
        logger.info("Shutting down bot...")
        await utils.save_state_from_botdata(self.app.bot_data)
        if 'radio_loop_task' in self.app.bot_data and not self.app.bot_data['radio_loop_task'].done():
            self.app.bot_data['radio_loop_task'].cancel()

    async def set_bot_commands(self):
        bot_commands = [
            BotCommand("play", "/p <query> - Найти трек"),
            BotCommand("menu", "/m - Показать меню и статус"),
            BotCommand("ron", "/r_on - Включить радио (админ)"),
            BotCommand("roff", "/r_off - Выключить радио (админ)"),
            BotCommand("skip", "/s - Пропустить трек (админ)"),
            BotCommand("vote", "/v - Голосование за жанр (админ)"),
            BotCommand("source", "/src <source> - Сменить источник (админ)"),
            BotCommand("reset", "Сбросить состояние (админ)"),
        ]
        await self.app.bot.set_my_commands(bot_commands)

    # --- Business Logic Methods ---
    async def radio_loop(self):
        logger.info("Radio loop started.")
        while self.state.is_on:
            if not self.state.radio_playlist:
                await self.refill_playlist()
                if not self.state.radio_playlist:
                    self.state.is_on = False
                    await self.app.bot.send_message(config.RADIO_CHAT_ID, "[ERR] Playlist is empty. Radio stopped.")
                    break
                continue
            url = self.state.radio_playlist.popleft()
            self.state.played_radio_urls.append(url)
            track_info = await self.downloader.download_track(url)
            if track_info:
                try:
                    self.state.now_playing = config.NowPlaying(title=track_info["title"], duration=track_info["duration"], url=url)
                    await self.update_status_panel(force=True)
                    with open(track_info["filepath"], 'rb') as audio_file:
                        await self.app.bot.send_audio(chat_id=config.RADIO_CHAT_ID, audio=audio_file, title=track_info["title"], duration=track_info["duration"], performer=track_info["performer"])
                    sleep_duration = track_info["duration"] + config.Constants.PAUSE_BETWEEN_TRACKS
                    await asyncio.sleep(sleep_duration)
                finally:
                    if os.path.exists(track_info["filepath"]):
                        os.remove(track_info["filepath"])
                    self.state.now_playing = None
            if len(self.state.radio_playlist) < config.Constants.REFILL_THRESHOLD and not self.app.bot_data['refill_lock'].locked():
                async with self.app.bot_data['refill_lock']:
                    asyncio.create_task(self.refill_playlist())
        logger.info("Radio loop finished.")

    async def refill_playlist(self):
        logger.info(f"Refilling playlist from {self.state.source} for genre: {self.state.genre}")
        tracks = await self.downloader.search_tracks(self.state.source, self.state.genre)
        if tracks:
            filtered_urls = [t["url"] for t in tracks if t.get("duration") and config.Constants.MIN_DURATION <= t["duration"] <= config.Constants.MAX_DURATION and t["url"] not in self.state.played_radio_urls]
            if filtered_urls:
                random.shuffle(filtered_urls)
                self.state.radio_playlist.extend(filtered_urls)
                logger.info(f"Added {len(filtered_urls)} new tracks to the playlist.")
                await utils.save_state_from_botdata(self.app.bot_data)

    # --- Handler Implementations ---
    async def show_menu(self, update: Update):
        is_admin_user = await utils.is_admin(update.effective_user.id)
        menu_text = [f"🎵 *Groove AI Radio v3.0* 🎵", "", "`/play, /p <query>` - Найти трек", "`/menu, /m` - Показать меню"]
        reply_keyboard_markup = ReplyKeyboardRemove()
        if is_admin_user:
            menu_text.extend(["", "*👑 Admin Commands*:", "`/ron` - Вкл. радио", "`/roff` - Выкл. радио", "`/skip` - Пропустить", "`/vote` - Голосовать", "`/source <src>` - Сменить источник", "`/keyboard` - Клавиатура"])
            reply_keyboard_markup = ReplyKeyboardMarkup([['/ron', '/roff', '/skip'], ['/src yt', '/src sc', '/src vk'], ['/vote', '/refresh']], resize_keyboard=True)
        await update.message.reply_text("\n".join(menu_text), reply_markup=reply_keyboard_markup, parse_mode="MarkdownV2")

    async def radio_on(self, update: Update):
        if self.state.is_on:
            await update.message.reply_text("Radio is already running!")
            return
        self.state.is_on = True
        await update.message.reply_text("🚀 Radio starting...")
        self.app.bot_data['radio_loop_task'] = asyncio.create_task(self.radio_loop())
        await utils.save_state_from_botdata(self.app.bot_data)
        await self.update_status_panel(force=True)

    async def radio_off(self, update: Update):
        if not self.state.is_on:
            await update.message.reply_text("Radio is already stopped!")
            return
        self.state.is_on = False
        if 'radio_loop_task' in self.app.bot_data and not self.app.bot_data['radio_loop_task'].done():
            self.app.bot_data['radio_loop_task'].cancel()
        self.state.now_playing = None
        await update.message.reply_text("⏹️ Radio stopped.")
        await utils.save_state_from_botdata(self.app.bot_data)
        await self.update_status_panel(force=True)

    async def skip_track(self, update: Update):
        if not self.state.is_on:
            await update.message.reply_text("Radio is not running.")
            return
        self.state.now_playing = None
        await update.message.reply_text("⏭️ Skipping...")

    async def set_source(self, update: Update, args: List[str]):
        source_alias = args[0].lower() if args else ""
        source_map = {"yt": "youtube", "sc": "soundcloud", "vk": "vk", "ar": "archive"}
        new_source = next((v for k, v in source_map.items() if source_alias in [k, v]), None)
        if not new_source:
            await update.message.reply_text(f"Invalid source. Supported: {list(source_map.keys())}")
            return
        self.state.source = new_source
        self.state.radio_playlist.clear()
        await update.message.reply_text(f"Source switched to: {new_source.title()}.")
        await self.refill_playlist()
        await utils.save_state_from_botdata(self.app.bot_data)

    async def play_track_search(self, update: Update, args: List[str]):
        # ... implementation ...
        pass

    # ... other handler implementations ...

    def run(self):
        self.app.post_init = self.post_init
        self.app.post_shutdown = self.on_shutdown
        self.register_handlers()
        logger.info("Starting bot polling...")
        self.app.run_polling()


def main():
    # --- Startup Validation ---
    logger.info("--- Running Environment Variable Check ---")
    required_vars = ["BOT_TOKEN", "ADMIN_IDS", "RADIO_CHAT_ID"]
    all_vars_ok = True
    for var in required_vars:
        if not getattr(config, var):
            logger.error(f"FATAL: Environment variable {var} is not set!")
            all_vars_ok = False
    
    if config.VK_COOKIES_DATA:
        logger.info("VK_COOKIES_DATA is present.")
    else:
        logger.warning("VK_COOKIES_DATA is missing. VK source will not work.")

    if config.YOUTUBE_COOKIES_DATA:
        logger.info("YOUTUBE_COOKIES_DATA is present.")
    else:
        logger.warning("YOUTUBE_COOKIES_DATA is missing. YouTube source may be restricted.")
    
    if not all_vars_ok:
        logger.fatal("Bot cannot start due to missing required environment variables.")
        return # Stop execution if required vars are missing

    logger.info("--- Environment Variable Check Passed ---")

    try:
        if not config.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is not set!")
        bot = MusicBot(config.BOT_TOKEN)
        bot.run()
    except Exception as e:
        logger.fatal(f"Bot failed to start: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()