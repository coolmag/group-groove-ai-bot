# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import random
from typing import List, Optional, Dict
from pathlib import Path
import yt_dlp
import aiohttp
from urllib.parse import quote

from telegram.ext import ContextTypes

import config
from utils import set_escaped_error, save_state_from_botdata

logger = logging.getLogger(__name__)

class MusicSourceManager:
    """A class to manage all music search and download operations from various sources."""

    def __init__(self):
        self._source_map = {
            "youtube": self._search_youtube,
            "soundcloud": self._search_soundcloud,
            "vk": self._search_vk,
            "archive": self._search_archive
        }

    def _get_cookie_file_from_data(self, cookie_data: Optional[str], source_name: str) -> Optional[str]:
        if not cookie_data:
            logger.warning(f"Cookie data for {source_name} is missing.")
            return None
        
        try:
            # This is a robust way to handle multiline env vars
            # It cleans up whitespace and ensures it's a valid string
            cleaned_data = "\n".join(line.strip() for line in cookie_data.strip().splitlines() if line.strip())
            
            # Basic validation
            if not cleaned_data.strip().startswith("# Netscape HTTP Cookie File"):
                logger.error(f"Cookie data for {source_name} does not start with the correct header.")
                return None

            temp_dir = Path("/tmp")
            temp_dir.mkdir(exist_ok=True)
            cookie_file = temp_dir / f"{source_name}_cookies.txt"
            cookie_file.write_text(cleaned_data)
            return str(cookie_file)
        except Exception as e:
            logger.error(f"Failed to write temporary cookie file for {source_name}: {e}")
            return None

    async def _execute_yt_dlp_search(self, query: str, ydl_opts: dict) -> List[dict]:
        base_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
        }
        ydl_opts.update(base_opts)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
            if not info or not info.get("entries"):
                return []
            return [{"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)} for e in info.get("entries", []) if e and e.get("url")]
        except Exception as e:
            logger.error(f"yt-dlp search failed for query '{query}': {e}")
            return []

    async def _search_youtube(self, query: str) -> List[dict]:
        search_query = f"ytsearch{config.Constants.SEARCH_LIMIT}:{query}"
        ydl_opts = {
            'cookiefile': self._get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")
        }
        return await self._execute_yt_dlp_search(search_query, ydl_opts)

    async def _search_soundcloud(self, query: str) -> List[dict]:
        search_query = f"scsearch{config.Constants.SEARCH_LIMIT}:{query}"
        return await self._execute_yt_dlp_search(search_query, {})

    async def _search_vk(self, query: str) -> List[dict]:
        cookie_path = self._get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
        if not cookie_path:
            return []
        # Correct syntax for vk search is to pass it directly to extract_info
        search_query = f"vksearch{config.Constants.SEARCH_LIMIT}:{query}"
        ydl_opts = {
            'cookiefile': cookie_path,
        }
        return await self._execute_yt_dlp_search(search_query, ydl_opts)

    async def _search_archive(self, query: str) -> List[dict]:
        search_url = f"https://archive.org/advancedsearch.php?q={quote(query)} AND mediatype:(audio)&fl[]=identifier,title,duration&sort[]=downloads desc&rows=10&output=json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url) as response:
                    response.raise_for_status()
                    data = await response.json()
                    docs = data.get("response", {}).get("docs", [])
                    return [{"url": f"https://archive.org/details/{doc['identifier']}", "title": doc.get("title", "Unknown Title"), "duration": float(doc.get("duration", 0))} for doc in docs if doc.get('identifier')]
        except Exception as e:
            logger.error(f"Internet Archive search failed: {e}")
            return []

    async def search_tracks(self, source: str, query: str) -> List[dict]:
        search_function = self._source_map.get(source)
        return await search_function(query) if search_function else []

    async def download_track(self, url: str) -> Optional[Dict[str, any]]:
        filepath = None
        try:
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': str(config.DOWNLOAD_DIR / '%(id)s.%(ext)s'),
                'noplaylist': True,
                'quiet': True,
                'ignoreerrors': True,
            }
            cookie_path = None
            if "youtube.com" in url or "youtu.be" in url:
                cookie_path = self._get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")
            elif "vk.com" in url:
                cookie_path = self._get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
            if cookie_path:
                ydl_opts['cookiefile'] = cookie_path

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            if not info:
                raise ValueError("yt-dlp did not return any info.")

            filepath = config.DOWNLOAD_DIR / f"{info['id']}.{info['ext']}"
            if not filepath.exists() or filepath.stat().st_size == 0:
                raise FileNotFoundError(f"Downloaded file is missing or empty: {filepath}")

            return {"filepath": filepath, "title": info.get("title", "Unknown"), "duration": int(info.get("duration", 0)), "performer": info.get("uploader", "Unknown")}
        except Exception as e:
            logger.error(f"Error processing track {url}: {e}")
            if filepath and filepath.exists():
                filepath.unlink(missing_ok=True)
            return None

music_source_manager = MusicSourceManager()

async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    tracks = await music_source_manager.search_tracks(state.source, state.genre)
    if tracks:
        filtered_urls = [t["url"] for t in tracks if t.get("duration") and config.Constants.MIN_DURATION <= t["duration"] <= config.Constants.MAX_DURATION and t["url"] not in state.played_radio_urls]
        if filtered_urls:
            random.shuffle(filtered_urls)
            state.radio_playlist.extend(filtered_urls)
            logger.info(f"Added {len(filtered_urls)} new tracks to the playlist.")
            await save_state_from_botdata(context.bot_data)
            return
    logger.error(f"Failed to refill playlist for '{state.genre}'.")
    set_escaped_error(state, f"Failed to find tracks for '{state.genre}'.")

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    logger.info("Radio loop started.")
    while state.is_on:
        if not state.radio_playlist:
            await refill_playlist(context)
            if not state.radio_playlist:
                set_escaped_error(state, "Playlist empty, could not refill.")
                state.is_on = False
                await context.bot.send_message(config.RADIO_CHAT_ID, "[ERR] Playlist is empty. Radio stopped.")
                break
            continue
        url = state.radio_playlist.popleft()
        state.played_radio_urls.append(url)
        if len(state.played_radio_urls) > config.Constants.PLAYED_URLS_MEMORY:
            state.played_radio_urls.popleft()
        track_info = await music_source_manager.download_track(url)
        if track_info:
            try:
                state.now_playing = config.NowPlaying(title=track_info["title"], duration=track_info["duration"], url=url)
                await radio.update_status_panel(context, force=True)
                with open(track_info["filepath"], 'rb') as audio_file:
                    await context.bot.send_audio(chat_id=config.RADIO_CHAT_ID, audio=audio_file, title=track_info["title"], duration=track_info["duration"], performer=track_info["performer"])
                sleep_duration = track_info["duration"] + config.Constants.PAUSE_BETWEEN_TRACKS
                await asyncio.sleep(sleep_duration)
            finally:
                if os.path.exists(track_info["filepath"]):
                    os.remove(track_info["filepath"])
                state.now_playing = None
        if len(state.radio_playlist) < config.Constants.REFILL_THRESHOLD and not context.bot_data['refill_lock'].locked():
            async with context.bot_data['refill_lock']:
                asyncio.create_task(refill_playlist(context))
    logger.info("Radio loop finished.")
