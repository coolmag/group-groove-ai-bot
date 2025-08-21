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
            "`/keyboard` - Показать/скрыть клавиатуру",
        ])
    await update.message.reply_text("\n".join(menu_text), parse_mode="MarkdownV2")

# --- Radio Control ---
@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: config.State = context.bot_data['state']
    if turn_on == state.is_on:
        await update.message.reply_text(f"Radio is already {'running' if turn_on else 'stopped'}!")
        return

    state.is_on = turn_on
    if turn_on:
        await update.message.reply_text("🚀 Radio starting... Searching for music.")
        context.bot_data['radio_loop_task'] = asyncio.create_task(radio.radio_loop(context))
    else:
        if 'radio_loop_task' in context.bot_data and not context.bot_data['radio_loop_task'].done():
            context.bot_data['radio_loop_task'].cancel()
        state.now_playing = None
        state.radio_playlist.clear()
        await update.message.reply_text("⏹️ Radio stopped!")
    
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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def health_check(request):
    return web.Response(text="Bot is running", status=200)