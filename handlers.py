# -*- coding: utf-8 -*-
import asyncio
import logging
import random
import re
import time
from functools import wraps
from typing import Optional
from pathlib import Path

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ContextTypes, Application, CommandHandler, CallbackQueryHandler, PollAnswerHandler
from telegram.error import BadRequest, TelegramError

from config import *
from utils import *
from radio import radio_loop, refill_playlist, check_track_validity

logger = logging.getLogger(__name__)


# --- Admin ---
async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id or not await is_admin(user_id):
            state: State = context.bot_data['state']
            set_escaped_error(state, "Unauthorized access attempt")
            if update.message:
                await update.message.reply_text("This command is for admins only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# --- UI ---
async def update_status_panel(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    async with context.bot_data['status_lock']:
        state: State = context.bot_data['state']
        current_time = time.time()
        
        if not force and current_time - state.last_status_update < Constants.STATUS_UPDATE_MIN_INTERVAL:
            return

        status_icon = '\U0001F7E2 ON' if state.is_on else '\U0001F534 OFF'
        status_lines = [
            f"\U0001F3B5 *Radio Groove AI* \U0001F3B5",
            f"**Status**: {status_icon}",
            f"**Genre**: {escape_markdown_v2(state.genre.title())}",
            f"**Source**: {escape_markdown_v2(state.source.title())}"
        ]
        
        if state.now_playing:
            elapsed = current_time - state.now_playing.start_time
            progress = min(elapsed / state.now_playing.duration, 1.0)
            progress_bar = get_progress_bar(progress)
            duration = format_duration(state.now_playing.duration)
            status_lines.append(f"**Now Playing**: {escape_markdown_v2(state.now_playing.title)}")
            status_lines.append(f"**Progress**: {progress_bar} {int(progress * 100)}%")
        else:
            status_lines.append("**Now Playing**: _Idle_")
            
        if state.active_poll_id:
            status_lines.append(f"\U0001F4DC **Active Poll** {escape_markdown_v2('(голосование идет)')}")
            
        if state.last_error:
            status_lines.append(f"\U000026A0\U0000FE0F **Last Error**: {state.last_error}")
            
        status_text = "\n".join(status_lines)
        
        start_skip_text = f'\u23ED\ufe0f Skip' if state.is_on else f'\u25B6\ufe0f Start'
        keyboard = []
        keyboard.append([
            InlineKeyboardButton("\U0001F504 Refresh", callback_data="radio:refresh"),
            InlineKeyboardButton(start_skip_text, callback_data="radio:skip" if state.is_on else "radio:on")
        ])
        
        if state.is_on and not state.active_poll_id:
            keyboard.append([InlineKeyboardButton("\U0001F4DC Vote", callback_data="vote:start")])
            
        if state.is_on:
            keyboard.append([InlineKeyboardButton("\u23F9\ufe0f Stop", callback_data="radio:off")])
            
        keyboard.append([InlineKeyboardButton("\U0001F4CB Menu", callback_data="cmd:menu")])
        
        try:
            if state.status_message_id:
                try:
                    await context.bot.delete_message(RADIO_CHAT_ID, state.status_message_id)
                except TelegramError as e:
                    logger.warning(f"Could not delete old status message: {e}")
            
            msg = await context.bot.send_message(
                chat_id=RADIO_CHAT_ID,
                text=status_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2"
            )
            state.status_message_id = msg.message_id
            state.last_status_update = current_time
            
        except Exception as e:
            logger.error(f"Status update failed: {e}")
            state.status_message_id = None 
            try:
                await context.bot.send_message(
                    RADIO_CHAT_ID,
                    re.sub(r'[*_`]', '', status_text),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as final_e:
                logger.error(f"Complete failure in status update: {final_e}")


# --- Commands ---
async def _start_radio_logic(context: ContextTypes.DEFAULT_TYPE):
    """The core logic for starting the radio, designed to be run in the background."""
    state: State = context.bot_data['state']
    
    if 'radio_loop_task' in context.bot_data and not context.bot_data['radio_loop_task'].done():
        context.bot_data['radio_loop_task'].cancel()
        try:
            await context.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass 

    state.now_playing = None
    state.radio_playlist.clear()
    state.played_radio_urls.clear()
    
    context.bot_data['radio_loop_task'] = asyncio.create_task(radio_loop(context))
    
    await refill_playlist(context)
    await update_status_panel(context, force=True)

@admin_only
async def radio_on_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    state: State = context.bot_data['state']
    
    if turn_on:
        if state.is_on:
            await update.message.reply_text("Radio is already running!")
            return
        state.is_on = True
        await update.message.reply_text(f"\U0001F680 Radio starting... Searching for music.")
        asyncio.create_task(_start_radio_logic(context))
    else:
        if not state.is_on:
            await update.message.reply_text("Radio is already stopped!")
            return
        state.is_on = False
        if 'radio_loop_task' in context.bot_data:
            context.bot_data['radio_loop_task'].cancel()
            try:
                await context.bot_data['radio_loop_task']
            except asyncio.CancelledError:
                pass
            del context.bot_data['radio_loop_task']
            
        state.now_playing = None
        state.radio_playlist.clear()
        await update.message.reply_text("\U0001F517 Radio stopped!")
        
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    state.is_on = False
    
    if 'radio_loop_task' in context.bot_data:
        context.bot_data['radio_loop_task'].cancel()
        try:
            await context.bot_data['radio_loop_task']
        except asyncio.CancelledError:
            pass
        del context.bot_data['radio_loop_task']
    
    await update.message.reply_text("\U0001F6D1 Bot stopping...")
    await save_state_from_botdata(context.bot_data)
    
    asyncio.create_task(context.application.stop())

@admin_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    state.now_playing = None
    await update.message.reply_text("\u23ED\ufe0f Skipping current track...")
    await update_status_panel(context, force=True)
    await save_state_from_botdata(context.bot_data)

@admin_only
async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_vote(context)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    answer = update.poll_answer
    
    if answer.poll_id != state.active_poll_id:
        return
        
    if answer.option_ids:
        chosen_option = answer.option_ids[0]
        if 0 <= chosen_option < len(state.poll_votes):
            state.poll_votes[chosen_option] += 1
            logger.info(f"Vote received for option {chosen_option}. New counts: {state.poll_votes}")
            await save_state_from_botdata(context.bot_data)

async def tally_vote(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    state: State = context.bot_data['state']

    if not state.active_poll_id or state.poll_message_id != job.data['poll_message_id']:
        logger.warning("Tally job running for an outdated or invalid poll. Ignoring.")
        return

    try:
        await context.bot.stop_poll(job.data['chat_id'], job.data['poll_message_id'])
        logger.info("Poll stopped visually.")
    except TelegramError as e:
        if "poll has already been closed" in str(e):
            logger.info("Poll was already closed by Telegram, proceeding with tally.")
        else:
            logger.error(f"Could not stop poll: {e}")

    total_votes = sum(state.poll_votes)
    if total_votes == 0:
        await context.bot.send_message(job.data['chat_id'], "No votes received. Selecting a random genre\!")
        new_genre = random.choice(state.votable_genres)
        state.genre = new_genre.lower()
        state.radio_playlist.clear()
        
        await context.bot.send_message(
            job.data['chat_id'],
            f"🎶 Randomly selected genre: *{escape_markdown_v2(new_genre.title())}*",
            parse_mode="MarkdownV2"
        )
        
        logger.info(f"Genre randomly changed to '{new_genre}'. Refilling playlist.")
        await refill_playlist(context)
    else:
        max_votes = max(state.poll_votes)
        winning_indices = [i for i, v in enumerate(state.poll_votes) if v == max_votes]
        winner_idx = random.choice(winning_indices)
        new_genre = state.poll_options[winner_idx]
        
        state.genre = new_genre.lower()
        state.radio_playlist.clear()
        
        await context.bot.send_message(
            job.data['chat_id'],
            f"🏁 Vote finished! New genre: *{escape_markdown_v2(new_genre)}*",
            parse_mode="MarkdownV2"
        )
        
        logger.info(f"Genre changed to '{new_genre}'. Refilling playlist.")
        await refill_playlist(context)

    state.active_poll_id = None
    state.poll_message_id = None
    state.poll_options = []
    state.poll_votes = []
    await save_state_from_botdata(context.bot_data)
    await update_status_panel(context, force=True)

@admin_only
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update_status_panel(context, force=True)
    await update.message.reply_text("\U0001F504 Status refreshed!")

@admin_only
async def set_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    
    if not context.args:
        await update.message.reply_text("Usage: /source soundcloud|youtube")
        return
        
    new_source = context.args[0].lower()
    if new_source not in ["soundcloud", "youtube"]:
        await update.message.reply_text("Invalid source. Use 'soundcloud' or 'youtube'")
        return
        
    state.source = new_source
    state.radio_playlist.clear()
    state.now_playing = None
    state.retry_count = 0
    
    await refill_playlist(context)
    await update.message.reply_text(f"Source switched to: {new_source.title()}")
    await save_state_from_botdata(context.bot_data)

@admin_only
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the config file to reset the bot's state."""
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
            await update.message.reply_text(
                "\u2705 State file (radio_config.json) deleted. "
                "Restarting the bot to apply default settings..."
            )
            asyncio.create_task(context.application.stop())
        except Exception as e:
            await update.message.reply_text(f"\u274C Could not delete state file: {e}")
    else:
        await update.message.reply_text("No state file to delete.")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please specify a song title. Usage: /play <song title>")
        return
        
    query = " ".join(context.args)
    state: State = context.bot_data['state']
    message = await update.message.reply_text(f'\U0001F50D Searching for "{query}"...')

    try:
        search_prefix = "scsearch10" if state.source == "soundcloud" else "ytsearch10"
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'default_search': search_prefix,
            'extract_flat': True
        }
        
        if state.source == "youtube" and YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
            
        if not info or 'entries' not in info or not info['entries']:
            await message.edit_text("No tracks found. \U0001F614")
            return
            
        tracks = []
        for entry in info['entries']:
            if not entry:
                continue
            tracks.append({
                "url": entry['url'],
                "title": entry.get('title', 'Unknown Track'),
                "duration": entry.get('duration', 0)
            })
        
        if not tracks:
            await message.edit_text("No tracks found. \U0001F614")
            return
            
        keyboard = []
        for track in tracks[:5]:
            title = track['title'][:30] + "..." if len(track['title']) > 30 else track['title']
            duration = format_duration(track['duration'])
            keyboard.append([InlineKeyboardButton(
                f"\u25B6\ufe0f {title} ({duration})",
                callback_data=f"play_track:{track['url']}"
            )])
            
        await message.edit_text(
            "Select a track:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.edit_text(f"[ERR] Search failed: {e}")

async def play_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("play_track:"):
        return
        
    url = query.data.split(":", 1)[1]
    await query.edit_message_text("\u2B07\ufe0f Downloading track...")
    
    try:
        state: State = context.bot_data['state']
        track_info = await check_track_validity(url)
        
        if not track_info:
            await query.edit_message_text("[ERR] Invalid track URL")
            return
            
        DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
            'quiet': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        }
        
        if "youtube.com" in url or "youtu.be" in url:
            if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
                ydl_opts['cookiefile'] = YOUTUBE_COOKIES
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filepath = Path(ydl.prepare_filename(info)).with_suffix('.mp3')
            
            if not filepath.exists():
                filepath = Path(ydl.prepare_filename(info)).with_suffix('.m4a')
                
            with open(filepath, 'rb') as f:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    title=info.get('title', 'Unknown Track'),
                    duration=info.get('duration', 0),
                    performer=info.get('uploader', 'Unknown Artist')
                )
                
        await query.edit_message_text("\u2705 Track sent!")
        
    except Exception as e:
        logger.error(f"Track download failed: {e}")
        await query.edit_message_text(f"[ERR] Failed to download track: {e}")
        
    finally:
        if 'filepath' in locals() and filepath.exists():
            try:
                filepath.unlink()
            except Exception:
                pass

async def radio_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        command, action = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        return
        
    state: State = context.bot_data['state']
    
    if command == "radio":
        if not await is_admin(query.from_user.id):
            await query.answer("Admin only command.", show_alert=True)
            return
            
        if action == "refresh":
            await query.answer()
            await update_status_panel(context, force=True)
            
        elif action == "skip":
            await query.answer("Skipping track...")
            state.now_playing = None
            await update_status_panel(context, force=True)
            
        elif action == "on":
            if state.is_on:
                await query.answer("Radio is already running!")
                return
            state.is_on = True
            await query.answer("Radio starting...")
            await context.bot.send_message(RADIO_CHAT_ID, "\U0001F680 Radio starting... Searching for music.")
            asyncio.create_task(_start_radio_logic(context))
            
        elif action == "off":
            if not state.is_on:
                await query.answer("Radio is already stopped!")
                return
            state.is_on = False
            if 'radio_loop_task' in context.bot_data:
                context.bot_data['radio_loop_task'].cancel()
                try:
                    await context.bot_data['radio_loop_task']
                except asyncio.CancelledError:
                    pass
                del context.bot_data['radio_loop_task']
            await update_status_panel(context, force=True)
            await query.answer("\U0001F517 Radio stopped!")
            
    elif command == "vote":
        if not await is_admin(query.from_user.id):
            await query.answer("Admin only command.", show_alert=True)
            return
            
        if action == "start":
            await query.answer()
            await start_vote(context)
            
    elif command == "cmd":
        await query.answer()
        await show_menu(update, context)

async def start_vote(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    
    if state.active_poll_id:
        await context.bot.send_message(RADIO_CHAT_ID, "\U0001F4DC There's already an active poll!")
        return
        
    if len(state.votable_genres) < 10:
        await context.bot.send_message(RADIO_CHAT_ID, "[WARN] Not enough genres available for voting.")
        return
        
    options = random.sample(state.votable_genres, 10)
    
    try:
        message = await context.bot.send_poll(
            chat_id=RADIO_CHAT_ID,
            question="\U0001F3B5 Choose the next music genre:",
            options=[g.title() for g in options],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=Constants.POLL_DURATION_SECONDS
        )
        
        state.active_poll_id = message.poll.id
        state.poll_message_id = message.message_id
        state.poll_options = [g.title() for g in options]
        state.poll_votes = [0] * len(options)
        
        context.application.job_queue.run_once(
            tally_vote, 
            Constants.POLL_DURATION_SECONDS + 2,
            data={'poll_message_id': message.message_id, 'chat_id': RADIO_CHAT_ID},
            name=f"vote_{message.poll.id}"
        )
        
        logger.info(f"Started poll {message.poll.id}, job scheduled for {Constants.POLL_DURATION_SECONDS + 2} seconds.")
        await save_state_from_botdata(context.bot_data)
        await update_status_panel(context, force=True)
        
    except Exception as e:
        logger.error(f"Failed to start vote: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Failed to start vote: {e}")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    is_admin_user = await is_admin(update.effective_user.id)
    
    status_icon = '\U0001F7E2 ON' if state.is_on else '\U0001F534 OFF'
    menu_text = [
        f"\U0001F3B5 *Groove AI Radio* \U0001F3B5",
        f"**Status**: {status_icon}",
        f"**Genre**: {escape_markdown_v2(state.genre.title())}",
        f"**Source**: {escape_markdown_v2(state.source.title())}",
        f"**Now Playing**: {escape_markdown_v2(state.now_playing.title if state.now_playing else 'None')}",
        "",
        f"\U0001F4BF *Commands*:",
        "/play <query> - Search and play a track",
        "/menu - Show this menu",
    ]
    
    if is_admin_user:
        menu_text.extend([
            "",
            f"\U0001F451 *Admin Commands*:",
            "/ron - Start radio",
            "/roff - Stop radio",
            "/skip - Skip current track",
            "/vote - Start genre vote",
            "/source <sc|yt> - Change source",
            "/refresh - Update status",
            "/stopbot - Stop the bot",
        ])
    
    start_skip_text = f'\u23ED\ufe0f Skip' if state.is_on else f'\u25B6\ufe0f Start'
    keyboard = [
        [InlineKeyboardButton("\U0001F3B5 Play Track", callback_data="cmd:play")],
        [InlineKeyboardButton("\U0001F4CB Menu", callback_data="cmd:menu")]
    ]
    
    if is_admin_user:
        keyboard.insert(0, [
            InlineKeyboardButton(start_skip_text, callback_data="radio:skip" if state.is_on else "radio:on"),
            InlineKeyboardButton("\u23F9\ufe0f Stop", callback_data="radio:off")
        ])
        keyboard.insert(1, [
            InlineKeyboardButton("\U0001F504 Refresh", callback_data="radio:refresh"),
            InlineKeyboardButton("\U0001F4DC Vote", callback_data="vote:start")
        ])
    
    full_text = "\n".join(menu_text)
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        query = update.callback_query
        if query:
            await query.edit_message_text(
                full_text,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2"
            )
        elif update.message:
            await update.message.reply_text(
                full_text,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2"
            )
    except Exception:
        fallback_text = re.sub(r'[*_`]', '', full_text)
        if 'query' in locals() and query:
            await query.edit_message_text(fallback_text, reply_markup=reply_markup)
        elif update.message:
            await update.message.reply_text(fallback_text, reply_markup=reply_markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)

# --- Health Check Endpoint ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log Errors caused by Updates."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

async def health_check(request):
    return web.Response(text="Bot is running", status=200)