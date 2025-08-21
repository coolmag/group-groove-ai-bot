# -*- coding: utf-8 -*-
import asyncio
import logging
import random
import re
import time
from typing import Optional

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from telegram.error import TelegramError

import config
import radio
from utils import (
    is_admin, admin_only, set_escaped_error, escape_markdown_v2, 
    get_progress_bar, format_duration, save_state_from_botdata
)

logger = logging.getLogger(__name__)

# --- UI & Menu ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    async with context.bot_data.get('status_lock', asyncio.Lock()):
        state: config.State = context.bot_data['state']
        current_time = time.time()
        
        if not force and current_time - state.last_status_update < config.Constants.STATUS_UPDATE_MIN_INTERVAL:
            return

        status_icon = '🟢 ON' if state.is_on else '🔴 OFF'
        status_lines = [
            f"🎵 *Groove AI Radio* 🎵",
            f"**Status**: {status_icon}",
            f"**Genre**: {escape_markdown_v2(state.genre.title())}",
            f"**Source**: {escape_markdown_v2(state.source.title())}"
        ]
        
        if state.now_playing:
            progress = min((current_time - state.now_playing.start_time) / state.now_playing.duration, 1.0)
            progress_bar = get_progress_bar(progress)
            status_lines.append(f"**Now Playing**: {escape_markdown_v2(state.now_playing.title)}")
            status_lines.append(f"**Progress**: {progress_bar} {int(progress * 100)}%")
        else:
            status_lines.append("**Now Playing**: _Idle_")
            
        if state.active_poll_id:
            status_lines.append(f"🗳️ **Active Poll** {escape_markdown_v2('(голосование идет)')}")
            
        if state.last_error:
            status_lines.append(f"⚠️ **Last Error**: {state.last_error}")
            
        status_text = "\n".join(status_lines)
        
        start_skip_text = '⏭️ Skip' if state.is_on else '▶️ Start'
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="radio:refresh"), InlineKeyboardButton(start_skip_text, callback_data="radio:skip" if state.is_on else "radio:on")],
            [InlineKeyboardButton("🗳️ Vote", callback_data="vote:start"), InlineKeyboardButton("⏹️ Stop", callback_data="radio:off")],
            [InlineKeyboardButton("📖 Menu", callback_data="cmd:menu")]
        ]
        
        try:
            if state.status_message_id:
                await context.bot.edit_message_text(chat_id=config.RADIO_CHAT_ID, message_id=state.status_message_id, text=status_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
            else:
                raise TelegramError("No status message to edit")
        except TelegramError:
            try:
                if state.status_message_id:
                    await context.bot.delete_message(config.RADIO_CHAT_ID, state.status_message_id)
                msg = await context.bot.send_message(chat_id=config.RADIO_CHAT_ID, text=status_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
                state.status_message_id = msg.message_id
            except Exception as final_e:
                logger.error(f"Complete failure in status update: {final_e}")
        state.last_status_update = current_time

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin_user = await is_admin(update.effective_user.id)
    
    menu_text = [
        f"🎵 *Groove AI Radio* 🎵",
        "",
        f"💿 *Commands*:",
        "`/play, /p <query>` - Найти и проиграть трек",
        "`/menu, /m` - Показать это меню",
    ]
    
    reply_keyboard_markup = ReplyKeyboardRemove()

    if is_admin_user:
        menu_text.extend([
            "",
            f"👑 *Admin Commands*:",
            "`/ron, /r_on` - Включить радио",
            "`/roff, /r_off, /stop, /t` - Выключить радио",
            "`/skip, /s` - Пропустить трек",
            "`/vote, /v` - Голосование за жанр",
            "`/source, /src <source>` - Сменить источник (yt, sc, vk, ar)",
            "`/refresh, /r` - Обновить статус панель",
        ])
        admin_keyboard = [
            ['/ron', '/roff', '/skip'],
            ['/src yt', '/src sc', '/src vk'],
            ['/vote', '/refresh']
        ]
        reply_keyboard_markup = ReplyKeyboardMarkup(
            admin_keyboard,
            resize_keyboard=True,
            input_field_placeholder="Admin Commands"
        )

    await update.message.reply_text(
        "\n".join(menu_text),
        reply_markup=reply_keyboard_markup,
        parse_mode="MarkdownV2"
    )

# --- Radio Control ---
# --- Voting ---
async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    """Starts a poll to vote for the next radio genre."""
    state: config.State = context.bot_data['state']
    
    if state.active_poll_id:
        logger.info("Vote requested, but one is already active.")
        return

    if len(state.votable_genres) < 10:
        logger.warning("Not enough genres to start a vote.")
        return
        
    options = random.sample(state.votable_genres, 10)
    
    try:
        message = await context.bot.send_poll(
            chat_id=config.RADIO_CHAT_ID,
            question="🗳️ Choose the next music genre:",
            options=[g.title() for g in options],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=config.Constants.POLL_DURATION_SECONDS
        )
        
        state.active_poll_id = message.poll.id
        state.poll_message_id = message.message_id
        state.poll_options = [g.title() for g in options]
        state.poll_votes = [0] * len(options)
        
        context.job_queue.run_once(
            tally_vote, 
            config.Constants.POLL_DURATION_SECONDS + 2,
            data={'poll_message_id': message.message_id, 'chat_id': config.RADIO_CHAT_ID},
            name=f"vote_{message.poll.id}"
        )
        
        logger.info(f"Started poll {message.poll.id}")
        await save_state_from_botdata(context.bot_data)
        await update_status_panel(context, force=True)
        
    except Exception as e:
        logger.error(f"Failed to start vote: {e}")
        set_escaped_error(state, "Failed to start poll.")

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /vote command."""
    await update.message.reply_text("Starting a new genre vote...")
    await start_vote(context)

@admin_only
async def scheduled_vote_command(context: ContextTypes.DEFAULT_TYPE):
    """Callback job to start a scheduled genre poll."""
    state: config.State = context.bot_data.get('state')
    if not state or not state.is_on:
        logger.info("Scheduled vote job skipped: radio is not running or state not found.")
        return

    logger.info(f"Running scheduled vote in chat {config.RADIO_CHAT_ID}")
    await start_vote(context)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a user's vote in a poll."""
    state: config.State = context.bot_data['state']
    answer = update.poll_answer
    
    if answer.poll_id != state.active_poll_id:
        return

    if answer.option_ids:
        chosen_option_index = answer.option_ids[0]
        if 0 <= chosen_option_index < len(state.poll_votes):
            state.poll_votes[chosen_option_index] += 1
            logger.info(f"Vote received for option {chosen_option_index}. New counts: {state.poll_votes}")

async def tally_vote(context: ContextTypes.DEFAULT_TYPE):
    """Called by the job queue to end the poll and determine the winner."""
    job = context.job
    state: config.State = context.bot_data['state']

    if not state.active_poll_id or state.poll_message_id != job.data['poll_message_id']:
        logger.warning("Tally job running for an outdated or invalid poll. Ignoring.")
        return

    try:
        await context.bot.stop_poll(job.data['chat_id'], job.data['poll_message_id'])
    except TelegramError as e:
        if "poll has already been closed" not in str(e):
            logger.error(f"Could not stop poll: {e}")

    if sum(state.poll_votes) > 0:
        max_votes = max(state.poll_votes)
        winning_indices = [i for i, v in enumerate(state.poll_votes) if v == max_votes]
        winner_idx = random.choice(winning_indices)
        new_genre_title = state.poll_options[winner_idx]
        new_genre = new_genre_title.lower()
        
        if state.genre != new_genre:
            state.genre = new_genre
            state.radio_playlist.clear()
            await context.bot.send_message(job.data['chat_id'], f"🏁 Vote finished! New genre: *{escape_markdown_v2(new_genre_title)}*", parse_mode="MarkdownV2")
            logger.info(f"Genre changed to '{new_genre}'. Refilling playlist.")
            asyncio.create_task(radio.refill_playlist(context))
        else:
            await context.bot.send_message(job.data['chat_id'], "Vote finished. Genre remains the same.")
    else:
        await context.bot.send_message(job.data['chat_id'], "No votes received. Keeping the current genre.")

    # Reset poll state
    state.active_poll_id = None
    state.poll_message_id = None
    state.poll_options = []
    state.poll_votes = []
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    if not state.is_on:
        await update.message.reply_text("Radio is not running.")
        return
    state.now_playing = None # The loop will detect this and skip
    await update.message.reply_text("⏭️ Skipping current track...")
    # The radio_loop will automatically play the next track

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    source_alias = context.args[0].lower() if context.args else ""
    
    source_map = {"yt": "youtube", "sc": "soundcloud", "vk": "vk", "ar": "archive"}
    new_source = next((v for k, v in source_map.items() if source_alias in [k, v]), None)

    if not new_source:
        await update.message.reply_text(f"Invalid source. Supported: {list(source_map.values())}")
        return
        
    if state.source == new_source:
        await update.message.reply_text(f"Source is already set to {new_source.title()}")
        return

    state.source = new_source
    state.radio_playlist.clear()
    state.now_playing = None
    await update.message.reply_text(f"Source switched to: {new_source.title()}. Refilling playlist...")
    await radio.refill_playlist(context)
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forces a refresh of the status panel."""
    await update.message.reply_text("Updating status panel...")
    await update_status_panel(context, force=True)

# --- User Commands ---
async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = None
    query_list = context.args
    if query_list and query_list[0].lower() in config.Constants.SUPPORTED_SOURCES:
        source = query_list[0].lower()
        query = " ".join(query_list[1:])
    else:
        state: config.State = context.bot_data['state']
        source = state.source
        query = " ".join(query_list)

    if not query:
        await update.message.reply_text("Please specify a search query.")
        return

    message = await update.message.reply_text(f'Searching for "{query}" on {source}...')
    tracks = await radio.music_source_manager.search_tracks(source, query)
    
    if not tracks:
        await message.edit_text("No tracks found. 😔")
        return
        
    keyboard = [[InlineKeyboardButton(f"▶️ {t['title'][:40]}... ({format_duration(t['duration'])})", callback_data=f"play_track:{t['url']}")] for t in tracks[:5]]
    await message.edit_text("Select a track to play:", reply_markup=InlineKeyboardMarkup(keyboard))

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    url = query.data.split(":", 1)[1]
    await query.edit_message_text(f"Downloading and sending track...")
    
    track_info = await radio.music_source_manager.download_track(url)
    if not track_info:
        await query.edit_message_text("[ERR] Failed to download track.")
        return

    try:
        with open(track_info["filepath"], 'rb') as f:
            await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, title=track_info['title'], duration=track_info['duration'], performer=track_info['performer'])
        await query.delete_message()
    finally:
        if os.path.exists(track_info["filepath"]):
            os.remove(track_info["filepath"])

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    command, action = query.data.split(":", 1)
    state: config.State = context.bot_data['state']
    
    if command == "radio":
        if not await is_admin(query.from_user.id):
            await query.answer("Admin only command.", show_alert=True)
            return
            
        if action == "refresh":
            await update_status_panel(context, force=True)
        elif action == "skip":
            if state.is_on:
                state.now_playing = None
        elif action == "on":
            if not state.is_on:
                state.is_on = True
                asyncio.create_task(radio.radio_loop(context))
        elif action == "off":
            if state.is_on:
                state.is_on = False
                if 'radio_loop_task' in context.bot_data and not context.bot_data['radio_loop_task'].done():
                    context.bot_data['radio_loop_task'].cancel()
                state.now_playing = None
        await update_status_panel(context, force=True)
            
    elif command == "vote":
        if not await is_admin(query.from_user.id):
            await query.answer("Admin only command.", show_alert=True)
            return
        if action == "start":
            await start_vote(context)
            
    elif command == "cmd":
        if action == "menu":
            await show_menu(query, context)

# --- Admin & System ---
@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot stopping...")
    asyncio.create_task(context.application.stop())

@admin_only
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.CONFIG_FILE.exists():
        config.CONFIG_FILE.unlink()
        await update.message.reply_text("State file deleted. Restarting...")
        asyncio.create_task(context.application.stop())
    else:
        await update.message.reply_text("No state file to delete.")

@admin_only
async def admin_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_keyboard = [['/ron', '/roff', '/skip'], ['/src yt', '/src sc', '/src vk', '/src ar'], ['/vote', '/refresh', '/menu']]
    reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True, input_field_placeholder="Admin Commands")
    await update.message.reply_text("Admin keyboard enabled.", reply_markup=reply_markup)

@admin_only
async def remove_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes the reply keyboard."""
    await update.message.reply_text("Admin keyboard removed.", reply_markup=ReplyKeyboardRemove())

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def health_check(request):
    return web.Response(text="Bot is running", status=200)