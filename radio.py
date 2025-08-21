# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import random
from typing import List, Optional
from pathlib import Path

import yt_dlp
from telegram.ext import ContextTypes

import config
from utils import set_escaped_error, save_state_from_botdata

logger = logging.getLogger(__name__)

def _get_cookie_file_from_data(cookie_data: Optional[str], source_name: str) -> Optional[str]:
    """Writes cookie data from an env var to a temp file and returns the path."""
    if not cookie_data:
        return None
    
    try:
        temp_dir = Path("/tmp")
        temp_dir.mkdir(exist_ok=True)
        cookie_file = temp_dir / f"{source_name}_cookies.txt"
        cookie_file.write_text(cookie_data)
        logger.info(f"Successfully wrote {source_name} cookies to temporary file: {cookie_file}")
        return str(cookie_file)
    except Exception as e:
        logger.error(f"Failed to write temporary cookie file for {source_name}: {e}")
        return None

async def _search_tracks(source: str, genre: str) -> List[dict]:
    logger.info(f"Searching {source} for genre: '{genre}'")
    
    search_query = genre
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'extract_flat': 'in_playlist',
        'ignoreerrors': True,
    }

    if source == "youtube":
        search_templates = [f"{genre} music", f"best of {genre}", f"{genre} playlist", f"{genre} mix"]
        search_query = random.choice(search_templates)
        ydl_opts['default_search'] = f"ytsearch{config.Constants.SEARCH_LIMIT}"
        cookie_path = _get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")
        if cookie_path:
            ydl_opts['cookiefile'] = cookie_path
    
    elif source == "soundcloud":
        ydl_opts['default_search'] = f"scsearch{config.Constants.SEARCH_LIMIT}"

    elif source == "vk":
        cookie_path = _get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
        if not cookie_path:
            logger.error("VK source selected but no VK_COOKIES_DATA was provided or writable.")
            return []
        ydl_opts['cookiefile'] = cookie_path
        ydl_opts['default_search'] = f"vksearch{config.Constants.SEARCH_LIMIT}"

    else:
        logger.error(f"Unknown source provided: {source}")
        return []

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, search_query, download=False)
        
        if not info or not info.get("entries"):
            logger.warning(f"No entries found on {source} for query: '{search_query}'")
            return []

        tracks = [{"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)} for e in info.get("entries", []) if e and e.get("url")]
        logger.info(f"Found {len(tracks)} tracks on {source} for query: '{search_query}'")
        return tracks
    except Exception as e:
        logger.error(f"{source.capitalize()} search failed for query '{search_query}': {e}")
        return []

async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")

    if len(state.played_radio_urls) > config.Constants.PLAYED_URLS_MEMORY * 0.8:
        state.played_radio_urls.clear()
        logger.debug("Cleared played URLs history.")

    for attempt in range(config.Constants.MAX_RETRIES):
        tracks = await _search_tracks(state.source, state.genre)
        
        if not tracks:
            logger.warning(f"No tracks found on {state.source} for genre {state.genre}, attempt {attempt + 1}")
            await asyncio.sleep(config.Constants.RETRY_INTERVAL)
            continue

        filtered_tracks = [t for t in tracks if t.get("duration") and config.Constants.MIN_DURATION <= t["duration"] <= config.Constants.MAX_DURATION and t["url"] not in state.played_radio_urls]
        
        if not filtered_tracks:
            logger.warning(f"No valid tracks found after filtering on {state.source}. Attempt {attempt + 1}")
            state.played_radio_urls.clear()
            await asyncio.sleep(config.Constants.RETRY_INTERVAL)
            continue

        urls = [t["url"] for t in filtered_tracks]
        random.shuffle(urls)
        state.radio_playlist.extend(urls)
        logger.info(f"Added {len(urls)} new tracks to the playlist.")
        await save_state_from_botdata(context.bot_data)
        return

    logger.error(f"Failed to refill playlist after {config.Constants.MAX_RETRIES} attempts.")
    set_escaped_error(state, f"Failed to find tracks after {config.Constants.MAX_RETRIES} attempts.")
    await context.bot.send_message(config.RADIO_CHAT_ID, f"[ERR] Failed to find any tracks for '{state.genre}' after multiple attempts.")
    await save_state_from_botdata(context.bot_data)

async def _refill_playlist_if_needed(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    if len(state.radio_playlist) < config.Constants.REFILL_THRESHOLD:
        if context.bot_data['refill_lock'].locked():
            return
        
        async with context.bot_data['refill_lock']:
            logger.info("Playlist running low, starting background refill...")
            await refill_playlist(context)
            logger.info("Background playlist refill complete.")

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str) -> int:
    state: config.State = context.bot_data['state']
    filepath = None
    try:
        config.DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': str(config.DOWNLOAD_DIR / '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'ignoreerrors': True,
        }
        
        cookie_path = None
        if "youtube.com" in url or "youtu.be" in url:
            cookie_path = _get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")
        elif "vk.com" in url:
            cookie_path = _get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
        
        if cookie_path:
            ydl_opts['cookiefile'] = cookie_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)

        if not info:
            raise ValueError("yt-dlp did not return any info.")

        original_ext = info.get('ext')
        if not original_ext:
            raise FileNotFoundError("Could not determine file extension.")

        filepath = config.DOWNLOAD_DIR / f"{info['id']}.{original_ext}"

        if not filepath.exists() or filepath.stat().st_size == 0:
            raise FileNotFoundError(f"Downloaded file is missing or empty: {filepath}")

        track_duration = int(info.get("duration", 0))
        if not (config.Constants.MIN_DURATION <= track_duration <= config.Constants.MAX_DURATION):
            return 0

        if filepath.stat().st_size > config.Constants.MAX_FILE_SIZE:
            return 0

        state.now_playing = config.NowPlaying(title=info.get("title", "Unknown Track"), duration=track_duration, url=url)
        
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(chat_id=config.RADIO_CHAT_ID, audio=f, title=state.now_playing.title, duration=state.now_playing.duration, performer=info.get("uploader", "Unknown Artist"))
        logger.info(f"Sent track: {state.now_playing.title}")
        return track_duration
        
    except Exception as e:
        logger.error(f"Error processing track {url}: {e}")
        set_escaped_error(state, f"Track processing error: {e}")
        return 0
    finally:
        state.now_playing = None
        if filepath and filepath.exists():
            try:
                filepath.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete track {filepath}: {e}")

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    logger.info("Starting radio loop")
    
    while state.is_on:
        try:
            if not state.radio_playlist:
                logger.warning("Playlist is empty, attempting to refill...")
                await _refill_playlist_if_needed(context)
                if not state.radio_playlist:
                    logger.error("Failed to refill playlist, stopping radio.")
                    set_escaped_error(state, "Playlist is empty and could not be refilled.")
                    state.is_on = False
                    await context.bot.send_message(config.RADIO_CHAT_ID, "[ERR] Playlist is empty and could not be refilled. Radio stopped.")
                    break 
                continue
            
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            
            if len(state.played_radio_urls) > config.Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            
            logger.info(f"Playing track: {url}")
            track_duration = await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            
            if track_duration == 0:
                logger.warning(f"Track failed to play: {url}. Trying next track.")
                await asyncio.sleep(1)
                continue

            sleep_time = track_duration + config.Constants.PAUSE_BETWEEN_TRACKS
            
            logger.debug(f"Waiting for {sleep_time} seconds until next track")
            await asyncio.sleep(sleep_time)

            if len(state.radio_playlist) < config.Constants.REFILL_THRESHOLD:
                asyncio.create_task(_refill_playlist_if_needed(context))
            
        except asyncio.CancelledError:
            logger.info("Radio loop cancelled")
            break
        except Exception as e:
            logger.error(f"Critical error in radio loop: {e}", exc_info=True)
            set_escaped_error(state, f"Radio loop error: {e}")
            await context.bot.send_message(config.RADIO_CHAT_ID, f"[ERR] Critical radio error: {e}. Restarting loop in 10s.")
            await asyncio.sleep(10)
