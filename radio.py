# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import random
import shutil
from typing import List, Optional

import yt_dlp
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from config import Constants, State, NowPlaying, YOUTUBE_COOKIES, RADIO_CHAT_ID, DOWNLOAD_DIR
from utils import set_escaped_error, escape_markdown_v2

logger = logging.getLogger(__name__)


async def get_tracks_soundcloud(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"scsearch{Constants.SEARCH_LIMIT}:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, f"scsearch{Constants.SEARCH_LIMIT}:{genre}", download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", []) if e
        ]
        logger.info(f"Found {len(tracks)} SoundCloud tracks for '{genre}'")
        return tracks
    except Exception as e:
        logger.error(f"SoundCloud search failed for '{genre}': {e}")
        return []

async def get_tracks_youtube(genre: str) -> List[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"ytsearch{Constants.SEARCH_LIMIT}:{genre}",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist'
    }
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, f"ytsearch{Constants.SEARCH_LIMIT}:{genre}", download=False)
        tracks = [
            {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
            for e in info.get("entries", []) if e
        ]
        logger.info(f"Found {len(tracks)} YouTube tracks for '{genre}'")
        return tracks
    except Exception as e:
        logger.error(f"YouTube search failed for '{genre}': {e}")
        return []


async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    
    if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY * 0.5:
        state.played_radio_urls.clear()
        logger.debug("Cleared played URLs to manage memory")

    async def attempt_refill(source: str, genre: str) -> List[dict]:
        return await get_tracks_soundcloud(genre) if source == "soundcloud" else await get_tracks_youtube(genre)

    original_genre, original_source = state.genre, state.source
    for attempt in range(Constants.MAX_RETRIES):
        try:
            tracks = await attempt_refill(state.source, state.genre)
            if not tracks:
                logger.warning(f"No tracks found on {state.source} for genre {state.genre}, attempt {attempt + 1}")
                set_escaped_error(state, f"No tracks found on {state.source} for genre {state.genre}")
                await context.bot.send_message(RADIO_CHAT_ID, f"[WARN] No tracks found on {state.source} for genre {state.genre}. Retrying ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                
                if state.source == "soundcloud" and attempt == 0:
                    state.source = "youtube"
                elif attempt == Constants.MAX_RETRIES - 1:
                    state.genre = Constants.DEFAULT_GENRE
                    state.source = Constants.DEFAULT_SOURCE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            filtered_tracks = [
                t for t in tracks
                if t.get("duration") and Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION
                and t["url"] not in state.played_radio_urls
            ]
            
            if not filtered_tracks:
                logger.warning(f"No valid tracks after filtering on {state.source}")
                set_escaped_error(state, f"No valid tracks after filtering on {state.source}")
                await context.bot.send_message(RADIO_CHAT_ID, f"[WARN] No valid tracks after filtering on {state.source}. Retrying ({attempt + 1}/{Constants.MAX_RETRIES}).")
                state.retry_count += 1
                state.played_radio_urls.clear()
                
                if state.source == "soundcloud" and attempt == 0:
                    state.source = "youtube"
                elif attempt == Constants.MAX_RETRIES - 1:
                    state.genre = Constants.DEFAULT_GENRE
                    state.source = Constants.DEFAULT_SOURCE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            urls = [t["url"] for t in filtered_tracks]
            random.shuffle(urls)
            state.radio_playlist.extend(urls)
            state.retry_count = 0
            state.genre = original_genre
            state.source = original_source
            logger.info(f"Added {len(urls)} tracks to playlist")
            await save_state_from_botdata(context.bot_data)
            return
            
        except Exception as e:
            logger.error(f"Playlist refill failed, attempt {attempt + 1}: {e}")
            set_escaped_error(state, f"Playlist refill error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Playlist refill error: {e}")
            state.retry_count += 1
            await asyncio.sleep(Constants.RETRY_INTERVAL)

    logger.error(f"Failed to refill playlist after {Constants.MAX_RETRIES} attempts")
    state.source = Constants.DEFAULT_SOURCE
    state.genre = Constants.DEFAULT_GENRE
    set_escaped_error(state, f"Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to {state.source}/{state.genre}.")
    await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to {state.source}/{state.genre}.")
    await save_state_from_botdata(context.bot_data)

async def _refill_playlist_if_needed(context: ContextTypes.DEFAULT_TYPE):
    """Checks if the playlist is running low and refills it in the background."""
    state: State = context.bot_data['state']
    if len(state.radio_playlist) < Constants.REFILL_THRESHOLD:
        if context.bot_data.get('refill_lock', asyncio.Lock()).locked():
            logger.info("Refill is already in progress.")
            return
        
        async with context.bot_data.get('refill_lock'):
            logger.info("Playlist running low, starting background refill...")
            await refill_playlist(context)
            logger.info("Background playlist refill complete.")

async def check_track_validity(url: str) -> Optional[dict]:
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'simulate': True
    }
    if "youtube.com" in url or "youtu.be" in url:
        if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
        return {
            "url": url,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0)
        }
    except Exception as e:
        logger.error(f"Track validity check failed for {url}: {e}")
        return None

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str) -> int:
    state: State = context.bot_data['state']
    track_info = await check_track_validity(url)
    if not track_info:
        set_escaped_error(state, "Invalid track URL")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Invalid track URL.")
        state.now_playing = None
        # await update_status_panel(context, force=True) # Handled in main loop
        return 0
    
    duration = track_info.get("duration", 0)
    if not (Constants.MIN_DURATION <= duration <= Constants.MAX_DURATION):
        set_escaped_error(state, f"Duration out of range ({duration}s)")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Track duration out of range ({duration}s).")
        state.now_playing = None
        # await update_status_panel(context, force=True) # Handled in main loop
        return 0

    DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        set_escaped_error(state, "Download directory not writable")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Download directory not writable.")
        state.now_playing = None
        # await update_status_panel(context, force=True) # Handled in main loop
        return 0

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'ffmpeg_location': shutil.which("ffmpeg"),
        'ffprobe_location': shutil.which("ffprobe")
    }
    
    if "youtube.com" in url or "youtu.be" in url:
        if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES

    filepath = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        
        filepath = Path(ydl.prepare_filename(info))


        if not filepath or not filepath.exists():
            set_escaped_error(state, "Failed to download track")
            await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Failed to download track.")
            state.now_playing = None
            # await update_status_panel(context, force=True) # Handled in main loop
            return 0

        if filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            set_escaped_error(state, "Track exceeds max file size")
            await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Track too large to send.")
            state.now_playing = None
            # await update_status_panel(context, force=True) # Handled in main loop
            return 0

        track_duration = int(info.get("duration", 0))
        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown Track"),
            duration=track_duration,
            url=url
        )
        # await update_status_panel(context, force=True) # Handled in main loop
        
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                chat_id=RADIO_CHAT_ID,
                audio=f,
                title=state.now_playing.title,
                duration=state.now_playing.duration,
                performer=info.get("uploader", "Unknown Artist")
            )
        logger.info(f"Sent track: {state.now_playing.title}")
        return track_duration
        
    except asyncio.TimeoutError:
        set_escaped_error(state, "Track download timeout")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Track download timed out.")
        return 0
    except TelegramError as e:
        set_escaped_error(state, f"Telegram error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Telegram error: {e}")
        return 0
    except Exception as e:
        set_escaped_error(state, f"Track processing error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Track processing error: {e}")
        return 0
    finally:
        state.now_playing = None
        # await update_status_panel(context, force=True) # Handled in main loop
        if filepath and filepath.exists():
            try:
                filepath.unlink(missing_ok=True)
            except Exception:
                pass

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info("Starting radio loop")
    
    while True:
        try:
            if not state.is_on:
                logger.info("Radio is off, sleeping")
                await asyncio.sleep(10)
                continue

            asyncio.create_task(_refill_playlist_if_needed(context))
                
            if not state.radio_playlist:
                logger.warning("Playlist is empty, waiting for refill...")
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue
            
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            
            logger.info(f"Playing track: {url}")
            track_duration = await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            
            sleep_time = (track_duration or 0) + Constants.PAUSE_BETWEEN_TRACKS
            
            logger.debug(f"Waiting for {sleep_time} seconds until next track")
            await asyncio.sleep(sleep_time)
            
        except asyncio.CancelledError:
            logger.info("Radio loop cancelled")
            return
        except Exception as e:
            logger.error(f"Radio loop error: {e}")
            set_escaped_error(state, f"Radio loop error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Radio error: {e}")
            await asyncio.sleep(10)
