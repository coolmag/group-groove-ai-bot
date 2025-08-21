# -*- coding: utf-8 -*-
import asyncio
import logging
import random
import os

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
    async with context.bot_data.get('status_lock'):
        state: config.State = context.bot_data['state']
        if not force and (time.time() - state.last_status_update) < config.Constants.STATUS_UPDATE_MIN_INTERVAL:
            return
        status_icon = '🟢 ON' if state.is_on else '🔴 OFF'
        status_lines = [f"🎵 *Groove AI Radio v2.1* 🎵", f"**Status**: {status_icon}", f"**Genre**: {escape_markdown_v2(state.genre.title())}", f"**Source**: {escape_markdown_v2(state.source.title())}"]
        if state.now_playing:
            progress = min((time.time() - state.now_playing.start_time) / state.now_playing.duration, 1.0)
            status_lines.append(f"**Now Playing**: {escape_markdown_v2(state.now_playing.title)}")
            status_lines.append(f"**Progress**: {get_progress_bar(progress)} {int(progress * 100)}%")
        else:
            status_lines.append("**Now Playing**: _Idle_")
        if state.active_poll_id:
            status_lines.append(f"🗳️ **Active Poll**")
        if state.last_error:
            status_lines.append(f"⚠️ **Last Error**: {state.last_error}")
        keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="radio:refresh"), InlineKeyboardButton('⏭️ Skip' if state.is_on else '▶️ Start', callback_data="radio:skip" if state.is_on else "radio:on")], [InlineKeyboardButton("🗳️ Vote", callback_data="vote:start"), InlineKeyboardButton("⏹️ Stop", callback_data="radio:off")], [InlineKeyboardButton("📖 Menu", callback_data="cmd:menu")] ]
        try:
            if state.status_message_id:
                await context.bot.edit_message_text(chat_id=config.RADIO_CHAT_ID, message_id=state.status_message_id, text="\n".join(status_lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
            else:
                raise TelegramError("No status message to edit")
        except TelegramError:
            try:
                if state.status_message_id:
                    await context.bot.delete_message(config.RADIO_CHAT_ID, state.status_message_id)
                msg = await context.bot.send_message(chat_id=config.RADIO_CHAT_ID, text="\n".join(status_lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
                state.status_message_id = msg.message_id
            except Exception as e:
                logger.error(f"Status update failed: {e}")
        state.last_status_update = time.time()

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin_user = await is_admin(update.effective_user.id)
    menu_text = [f"🎵 *Groove AI Radio v2.1* 🎵", "", f"💿 *Commands*:", "`/play, /p <query>` - Найти и проиграть трек", "`/menu, /m` - Показать это меню"]
    reply_keyboard_markup = ReplyKeyboardRemove()
    if is_admin_user:
        menu_text.extend(["", f"👑 *Admin Commands*:", "`/ron, /r_on` - Включить радио", "`/roff, /r_off, /stop, /t` - Выключить радио", "`/skip, /s` - Пропустить трек", "`/vote, /v` - Голосование за жанр", "`/source, /src <source>` - Сменить источник (yt, sc, vk, ar)", "`/refresh, /r` - Обновить статус панель", "`/keyboard` - Показать/скрыть клавиатуру", "`/stopbot` - Остановить бота"])
        reply_keyboard_markup = ReplyKeyboardMarkup([['/ron', '/roff', '/skip'], ['/src yt', '/src sc', '/src vk'], ['/vote', '/refresh']], resize_keyboard=True, input_field_placeholder="Admin Commands")
    await update.message.reply_text("\n".join(menu_text), reply_markup=reply_keyboard_markup, parse_mode="MarkdownV2")

# --- Radio Control ---
@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: config.State = context.bot_data['state']
    if turn_on == state.is_on:
        await update.message.reply_text(f"Radio is already {'on' if turn_on else 'off'}.")
        return
    state.is_on = turn_on
    if turn_on:
        await update.message.reply_text("🚀 Radio starting...")
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio.radio_loop(context))
    else:
        if 'radio_loop_task' in context.bot_data and not context.bot_data['radio_loop_task'].done():
            context.bot_data['radio_loop_task'].cancel()
        state.now_playing = None
        await update.message.reply_text("⏹️ Radio stopped.")
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    if not state.is_on:
        await update.message.reply_text("Radio is not running.")
        return
    state.now_playing = None
    await update.message.reply_text("⏭️ Skipping...")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    source_alias = context.args[0].lower() if context.args else ""
    source_map = {"yt": "youtube", "sc": "soundcloud", "vk": "vk", "ar": "archive"}
    new_source = next((v for k, v in source_map.items() if source_alias in [k, v]), None)
    if not new_source:
        await update.message.reply_text(f"Invalid source. Supported: {list(source_map.keys())}")
        return
    if state.source == new_source:
        await update.message.reply_text(f"Source is already {new_source.title()}.")
        return
    state.source = new_source
    state.radio_playlist.clear()
    await update.message.reply_text(f"Source switched to: {new_source.title()}.")
    await radio.refill_playlist(context)
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

# --- Voting ---
async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    if state.active_poll_id:
        return
    options = random.sample(state.votable_genres, 10)
    message = await context.bot.send_poll(chat_id=config.RADIO_CHAT_ID, question="🗳️ Choose the next music genre:", options=[g.title() for g in options], is_anonymous=False, open_period=config.Constants.POLL_DURATION_SECONDS)
    state.active_poll_id = message.poll.id
    state.poll_message_id = message.message_id
    state.poll_options = [g.title() for g in options]
    state.poll_votes = [0] * len(options)
    context.job_queue.run_once(tally_vote, config.Constants.POLL_DURATION_SECONDS + 2, data={'poll_message_id': message.message_id, 'chat_id': config.RADIO_CHAT_ID}, name=f"vote_{message.poll.id}")
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_vote(context)

@admin_only
async def scheduled_vote_command(context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data['state'].is_on:
        await start_vote(context)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    answer = update.poll_answer
    if answer.poll_id == state.active_poll_id and answer.option_ids:
        state.poll_votes[answer.option_ids[0]] += 1

async def tally_vote(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    state: config.State = context.bot_data['state']
    if not state.active_poll_id or state.poll_message_id != job.data['poll_message_id']:
        return
    try:
        await context.bot.stop_poll(job.data['chat_id'], job.data['poll_message_id'])
    except TelegramError:
        pass
    if sum(state.poll_votes) > 0:
        new_genre = state.poll_options[max(range(len(state.poll_votes)), key=state.poll_votes.__getitem__)]
        if state.genre != new_genre.lower():
            state.genre = new_genre.lower()
            state.radio_playlist.clear()
            await context.bot.send_message(job.data['chat_id'], f"🏁 Vote finished! New genre: *{escape_markdown_v2(new_genre)}*", parse_mode="MarkdownV2")
            asyncio.create_task(radio.refill_playlist(context))
    state.active_poll_id = None
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

# --- User Commands ---
async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = next((s for s in config.Constants.SUPPORTED_SOURCES if context.args and context.args[0].lower() in [s, s[:2]]), context.bot_data['state'].source)
    query = " ".join(context.args[1:] if source != context.bot_data['state'].source else context.args)
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
    await query.edit_message_text(f"Downloading track...")
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

# --- Callbacks & System ---
async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    command, action = query.data.split(":", 1)
    user_id = query.from_user.id
    if command == "radio":
        if not await is_admin(user_id):
            return
        if action == "refresh":
            await update_status_panel(context, force=True)
        elif action == "skip":
            await skip_command(update, context)
        elif action == "on":
            await radio_on_off_command(update, context, True)
        elif action == "off":
            await radio_on_off_command(update, context, False)
    elif command == "vote" and action == "start":
        if not await is_admin(user_id):
            return
        await start_vote(context)
    elif command == "cmd" and action == "menu":
        await show_menu(query.message, context)

@admin_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context, force=True)

@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is shutting down...")
    asyncio.create_task(context.application.stop())

@admin_only
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.CONFIG_FILE.exists():
        config.CONFIG_FILE.unlink()
        await update.message.reply_text("State file deleted. Restarting...")
        asyncio.create_task(context.application.stop())

@admin_only
async def admin_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_keyboard = [['/ron', '/roff', '/skip'], ['/src yt', '/src sc', '/src vk', '/src ar'], ['/vote', '/refresh']]
    reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True, input_field_placeholder="Admin Commands")
    await update.message.reply_text("Admin keyboard enabled.", reply_markup=reply_markup)

@admin_only
async def remove_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Admin keyboard removed.", reply_markup=ReplyKeyboardRemove())

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def health_check(request):
    return web.Response(text="Bot is running", status=200)