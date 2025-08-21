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
        
        self.app.bot_data['music_bot'] = self
        self.app.bot_data['status_lock'] = locks.status_lock
        self.app.bot_data['refill_lock'] = locks.refill_lock

    def register_handlers(self):
        self.app.add_handler(CommandHandler(["start", "menu", "m"], self.show_menu))
        self.app.add_handler(CommandHandler(["ron", "r_on"], self.radio_on))
        self.app.add_handler(CommandHandler(["roff", "r_off", "stop", "t"], self.radio_off))
        self.app.add_handler(CommandHandler("stopbot", self.stop_bot))
        self.app.add_handler(CommandHandler(["skip", "s"], self.skip_track))
        self.app.add_handler(CommandHandler(["vote", "v"], self.start_vote_command))
        self.app.add_handler(CommandHandler(["source", "src"], self.set_source))
        self.app.add_handler(CommandHandler(["reset"], self.reset))
        self.app.add_handler(CommandHandler(["play", "p"], self.play_command))
        self.app.add_handler(CommandHandler(["keyboard", "kb"], self.admin_keyboard))
        self.app.add_handler(CallbackQueryHandler(self.play_button_callback, pattern=r"^play_track:"))
        self.app.add_handler(CallbackQueryHandler(self.panel_callback, pattern=r"^(radio|vote|cmd):" ))
        self.app.add_handler(PollAnswerHandler(self.handle_poll_answer))
        self.app.add_error_handler(self.error_handler)

    async def post_init(self, application: Application):
        logger.info("Initializing bot...")
        await self.set_bot_commands()
        if self.state.is_on:
            logger.info("Radio was on at startup, resuming...")
            self.app.bot_data['radio_loop_task'] = asyncio.create_task(self.radio_loop())
        self.app.job_queue.run_repeating(self.scheduled_vote, interval=config.Constants.VOTING_INTERVAL_SECONDS, first=10, name="hourly_vote_job")
        logger.info("Bot initialized successfully")

    async def on_shutdown(self, application: Application):
        logger.info("Shutting down bot...")
        await utils.save_state_from_botdata(self.app.bot_data)
        if 'radio_loop_task' in self.app.bot_data and not self.app.bot_data['radio_loop_task'].done():
            self.app.bot_data['radio_loop_task'].cancel()

    async def set_bot_commands(self):
        bot_commands = [
            BotCommand("play", "/p <query> - Найти трек"),
            BotCommand("menu", "/m - Показать меню"),
            BotCommand("ron", "/r_on - Включить радио"),
            BotCommand("roff", "/r_off - Выключить радио"),
            BotCommand("skip", "/s - Пропустить трек"),
            BotCommand("vote", "/v - Голосование за жанр"),
            BotCommand("source", "/src <source> - Сменить источник"),
            BotCommand("keyboard", "Показать/скрыть клавиатуру"),
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
    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        is_admin_user = await utils.is_admin(update.effective_user.id)
        menu_text = [f"🎵 *Groove AI Radio v3.0* 🎵", "", "`/play, /p <query>` - Найти трек", "`/menu, /m` - Показать меню"]
        reply_keyboard_markup = ReplyKeyboardRemove()
        if is_admin_user:
            menu_text.extend(["", "*👑 Admin Commands*:", "`/ron` - Вкл. радио", "`/roff` - Выкл. радио", "`/skip` - Пропустить", "`/vote` - Голосовать", "`/source <src>` - Сменить источник", "`/keyboard` - Клавиатура"])
            reply_keyboard_markup = ReplyKeyboardMarkup([['/ron', '/roff', '/skip'], ['/src yt', '/src sc', '/src vk'], ['/vote', '/refresh']], resize_keyboard=True)
        await update.message.reply_text("\n".join(menu_text), reply_markup=reply_keyboard_markup, parse_mode="MarkdownV2")

    @utils.admin_only
    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.state.is_on:
            await update.message.reply_text("Radio is already running!")
            return
        self.state.is_on = True
        await update.message.reply_text("🚀 Radio starting...")
        self.app.bot_data['radio_loop_task'] = asyncio.create_task(self.radio_loop())
        await utils.save_state_from_botdata(self.app.bot_data)
        await self.update_status_panel(force=True)

    @utils.admin_only
    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    @utils.admin_only
    async def skip_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.state.is_on:
            await update.message.reply_text("Radio is not running.")
            return
        self.state.now_playing = None
        await update.message.reply_text("⏭️ Skipping...")

    @utils.admin_only
    async def set_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
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

    async def play_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        source = next((s for s in config.Constants.SUPPORTED_SOURCES if args and args[0].lower() in [s, s[:2]]), self.state.source)
        query = " ".join(args[1:] if source != self.state.source else args)
        if not query:
            await update.message.reply_text("Please specify a search query.")
            return
        message = await update.message.reply_text(f'Searching for "{query}" on {source}...')
        tracks = await self.downloader.search_tracks(source, query)
        if not tracks:
            await message.edit_text("No tracks found. 😔")
            return
        keyboard = [[InlineKeyboardButton(f"▶️ {t['title'][:40]}... ({utils.format_duration(t['duration'])})", callback_data=f"play_track:{t['url']}")] for t in tracks[:5]]
        await message.edit_text("Select a track to play:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def play_button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        url = query.data.split(":", 1)[1]
        await query.edit_message_text(f"Downloading track...")
        track_info = await self.downloader.download_track(url)
        if not track_info:
            await query.edit_message_text("[ERR] Failed to download track.")
            return
        try:
            with open(track_info["filepath"], 'rb') as f:
                await self.app.bot.send_audio(chat_id=query.message.chat_id, audio=f, title=track_info['title'], duration=track_info['duration'], performer=track_info['performer'])
            await query.delete_message()
        finally:
            if os.path.exists(track_info["filepath"]):
                os.remove(track_info["filepath"])

    @utils.admin_only
    async def start_vote_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.start_vote()

    async def scheduled_vote(self, context: ContextTypes.DEFAULT_TYPE):
        if self.state.is_on:
            await self.start_vote()

    async def start_vote(self):
        if self.state.active_poll_id:
            return
        options = random.sample(self.state.votable_genres, 10)
        message = await self.app.bot.send_poll(chat_id=config.RADIO_CHAT_ID, question="🗳️ Choose the next music genre:", options=[g.title() for g in options], is_anonymous=False, open_period=config.Constants.POLL_DURATION_SECONDS)
        self.state.active_poll_id = message.poll.id
        self.state.poll_message_id = message.message_id
        self.state.poll_options = [g.title() for g in options]
        self.state.poll_votes = [0] * len(options)
        self.app.job_queue.run_once(self.tally_vote, config.Constants.POLL_DURATION_SECONDS + 2, data={'poll_message_id': message.message_id, 'chat_id': config.RADIO_CHAT_ID}, name=f"vote_{message.poll.id}")
        await utils.save_state_from_botdata(self.app.bot_data)

    async def handle_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        answer = update.poll_answer
        if answer.poll_id == self.state.active_poll_id and answer.option_ids:
            self.state.poll_votes[answer.option_ids[0]] += 1

    async def tally_vote(self, context: ContextTypes.DEFAULT_TYPE):
        job = context.job
        if not self.state.active_poll_id or self.state.poll_message_id != job.data['poll_message_id']:
            return
        try:
            await self.app.bot.stop_poll(job.data['chat_id'], job.data['poll_message_id'])
        except TelegramError:
            pass
        if sum(self.state.poll_votes) > 0:
            new_genre = self.state.poll_options[max(range(len(self.state.poll_votes)), key=self.state.poll_votes.__getitem__)]
            if self.state.genre != new_genre.lower():
                self.state.genre = new_genre.lower()
                self.state.radio_playlist.clear()
                await self.app.bot.send_message(job.data['chat_id'], f"🏁 Vote finished! New genre: *{utils.escape_markdown_v2(new_genre)}*", parse_mode="MarkdownV2")
                asyncio.create_task(self.refill_playlist())
        self.state.active_poll_id = None
        await utils.save_state_from_botdata(self.app.bot_data)
        await self.update_status_panel(force=True)

    async def panel_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        command, action = query.data.split(":", 1)
        if command == "radio":
            if not await utils.is_admin(query.from_user.id):
                return
            if action == "refresh":
                await self.update_status_panel(force=True)
            elif action == "skip":
                await self.skip_track(update)
            elif action == "on":
                await self.radio_on(update)
            elif action == "off":
                await self.radio_off(update)
        elif command == "vote" and action == "start":
            if not await utils.is_admin(query.from_user.id):
                return
            await self.start_vote_command(update)
        elif command == "cmd" and action == "menu":
            await self.show_menu(query.message, context)

    async def update_status_panel(self, force: bool = False):
        async with self.app.bot_data.get('status_lock'):
            if not force and (time.time() - self.state.last_status_update) < config.Constants.STATUS_UPDATE_MIN_INTERVAL:
                return
            status_icon = '🟢 ON' if self.state.is_on else '🔴 OFF'
            status_lines = [f"🎵 *Groove AI Radio v3.0* 🎵", f"**Status**: {status_icon}", f"**Genre**: {utils.escape_markdown_v2(self.state.genre.title())}", f"**Source**: {utils.escape_markdown_v2(self.state.source.title())}"]
            if self.state.now_playing:
                progress = min((time.time() - self.state.now_playing.start_time) / self.state.now_playing.duration, 1.0)
                status_lines.append(f"**Now Playing**: {utils.escape_markdown_v2(self.state.now_playing.title)}")
                status_lines.append(f"**Progress**: {utils.get_progress_bar(progress)} {int(progress * 100)}%")
            else:
                status_lines.append("**Now Playing**: _Idle_")
            if self.state.active_poll_id:
                status_lines.append(f"🗳️ **Active Poll**")
            if self.state.last_error:
                status_lines.append(f"⚠️ **Last Error**: {self.state.last_error}")
            keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="radio:refresh"), InlineKeyboardButton('⏭️ Skip' if self.state.is_on else '▶️ Start', callback_data="radio:skip" if self.state.is_on else "radio:on")], [InlineKeyboardButton("🗳️ Vote", callback_data="vote:start"), InlineKeyboardButton("⏹️ Stop", callback_data="radio:off")], [InlineKeyboardButton("📖 Menu", callback_data="cmd:menu")] ]
            try:
                if self.state.status_message_id:
                    await self.app.bot.edit_message_text(chat_id=config.RADIO_CHAT_ID, message_id=self.state.status_message_id, text="\n".join(status_lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
                else:
                    raise TelegramError("No status message to edit")
            except TelegramError:
                try:
                    if self.state.status_message_id:
                        await self.app.bot.delete_message(config.RADIO_CHAT_ID, self.state.status_message_id)
                    msg = await self.app.bot.send_message(chat_id=config.RADIO_CHAT_ID, text="\n".join(status_lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
                    self.state.status_message_id = msg.message_id
                except Exception as e:
                    logger.error(f"Status update failed: {e}")
            self.state.last_status_update = time.time()

    @utils.admin_only
    async def stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Bot is shutting down...")
        asyncio.create_task(self.app.stop())

    @utils.admin_only
    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if config.CONFIG_FILE.exists():
            config.CONFIG_FILE.unlink()
            await update.message.reply_text("State file deleted. Restarting...")
            asyncio.create_task(self.app.stop())

    @utils.admin_only
    async def admin_keyboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        admin_keyboard = [['/ron', '/roff', '/skip'], ['/src yt', '/src sc', '/src vk', '/src ar'], ['/vote', '/refresh']]
        reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True, input_field_placeholder="Admin Commands")
        await update.message.reply_text("Admin keyboard enabled.", reply_markup=reply_markup)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

    def run(self):
        self.app.post_init = self.post_init
        self.app.post_shutdown = self.on_shutdown
        self.register_handlers()
        
        async def run_server():
            server_app = web.Application()
            server_app.router.add_get("/", self.health_check)
            runner = web.AppRunner(server_app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', config.PORT)
            await site.start()
        
        loop = asyncio.get_event_loop()
        loop.create_task(run_server())
        logger.info("Starting bot polling...")
        self.app.run_polling()

    async def health_check(self, request):
        return web.Response(text="Bot is running", status=200)

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
        return

    logger.info("--- Environment Variable Check Passed ---")

    try:
        bot = MusicBot(config.BOT_TOKEN)
        bot.run()
    except Exception as e:
        logger.fatal(f"Bot failed to start: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
