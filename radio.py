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
            return None
        try:
            temp_dir = Path("/tmp")
            temp_dir.mkdir(exist_ok=True)
            cookie_file = temp_dir / f"{source_name}_cookies.txt"
            cookie_file.write_text(cookie_data)
            return str(cookie_file)
        except Exception as e:
            logger.error(f"Failed to write temporary cookie file for {source_name}: {e}")
            return None

    async def _search_youtube(self, query: str) -> List[dict]:
        search_templates = [f"{query} music", f"best of {query}", f"{query} playlist", f"{query} mix"]
        search_query = random.choice(search_templates)
        ydl_opts = {
            'default_search': f"ytsearch{config.Constants.SEARCH_LIMIT}",
            'cookiefile': self._get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")
        }
        return await self._execute_yt_dlp_search(search_query, ydl_opts)

    async def _search_soundcloud(self, query: str) -> List[dict]:
        ydl_opts = {'default_search': f"scsearch{config.Constants.SEARCH_LIMIT}"}
        return await self._execute_yt_dlp_search(query, ydl_opts)

    async def _search_vk(self, query: str) -> List[dict]:
        cookie_path = self._get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
        if not cookie_path:
            logger.error("VK source selected but no VK_COOKIES_DATA was provided or writable.")
            return []
        ydl_opts = {
            'default_search': f"vksearch{config.Constants.SEARCH_LIMIT}",
            'cookiefile': cookie_path
        }
        return await self._execute_yt_dlp_search(query, ydl_opts)

    async def _search_archive(self, query: str) -> List[dict]:
        search_url = f"https://archive.org/advancedsearch.php?q={quote(query)} AND mediatype:(audio)&fl[]=identifier,title,duration&sort[]=downloads desc&rows=10&output=json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url) as response:
                    response.raise_for_status()
                    data = await response.json()
                    docs = data.get("response", {}).get("docs", [])
                    tracks = [
                        {
                            "url": f"https://archive.org/details/{doc['identifier']}",
                            "title": doc.get("title", "Unknown Title"),
                            "duration": float(doc.get("duration", 0))
                        }
                        for doc in docs if doc.get('identifier')
                    ]
                    logger.info(f"Found {len(tracks)} tracks on Internet Archive for query: '{query}'")
                    return tracks
        except Exception as e:
            logger.error(f"Internet Archive search failed for query '{query}': {e}")
            return []

    async def _execute_yt_dlp_search(self, query: str, ydl_opts: dict) -> List[dict]:
        base_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
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

    async def search_tracks(self, source: str, query: str) -> List[dict]:
        search_function = self._source_map.get(source)
        if not search_function:
            logger.error(f"Unknown source requested: {source}")
            return []
        return await search_function(query)

    async def download_track(self, url: str) -> Optional[Dict[str, any]]:
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
                cookie_path = self._get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")
            elif "vk.com" in url:
                cookie_path = self._get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
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

            return {
                "filepath": filepath,
                "title": info.get("title", "Unknown Track"),
                "duration": int(info.get("duration", 0)),
                "performer": info.get("uploader", "Unknown Artist")
            }

        except Exception as e:
            logger.error(f"Error processing track {url}: {e}")
            if filepath and filepath.exists():
                filepath.unlink(missing_ok=True)
            return None

# --- Radio Logic ---
music_source_manager = MusicSourceManager()

async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")

    for _ in range(config.Constants.MAX_RETRIES):
        tracks = await music_source_manager.search_tracks(state.source, state.genre)
        if tracks:
            filtered_urls = [t["url"] for t in tracks if t.get("duration") and config.Constants.MIN_DURATION <= t["duration"] <= config.Constants.MAX_DURATION and t["url"] not in state.played_radio_urls]
            if filtered_urls:
                random.shuffle(filtered_urls)
                state.radio_playlist.extend(filtered_urls)
                logger.info(f"Added {len(filtered_urls)} new tracks to the playlist.")
                await save_state_from_botdata(context.bot_data)
                return
        logger.warning(f"No valid new tracks found on {state.source} for genre {state.genre}. Retrying...")
        await asyncio.sleep(2)

    logger.error(f"Failed to refill playlist after multiple attempts.")
    set_escaped_error(state, f"Failed to find tracks for '{state.genre}'.")

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: config.State = context.bot_data['state']
    logger.info("Radio loop started.")
    
    while state.is_on:
        if not state.radio_playlist:
            logger.warning("Playlist is empty, attempting to refill...")
            await refill_playlist(context)
            if not state.radio_playlist:
                set_escaped_error(state, "Playlist empty, could not refill.")
                state.is_on = False
                await context.bot.send_message(config.RADIO_CHAT_ID, "[ERR] Playlist is empty and could not be refilled. Radio stopped.")
                break
            continue

        url = state.radio_playlist.popleft()
        state.played_radio_urls.append(url)
        if len(state.played_radio_urls) > config.Constants.PLAYED_URLS_MEMORY:
            state.played_radio_urls.popleft()

        logger.info(f"Downloading track: {url}")
        track_info = await music_source_manager.download_track(url)

        if track_info:
            try:
                state.now_playing = config.NowPlaying(title=track_info["title"], duration=track_info["duration"], url=url)
                with open(track_info["filepath"], 'rb') as audio_file:
                    await context.bot.send_audio(chat_id=config.RADIO_CHAT_ID, audio=audio_file, title=track_info["title"], duration=track_info["duration"], performer=track_info["performer"])
                logger.info(f"Sent track: {track_info['title']}")
                sleep_duration = track_info["duration"] + config.Constants.PAUSE_BETWEEN_TRACKS
                await asyncio.sleep(sleep_duration)
            except Exception as e:
                logger.error(f"Failed to send audio: {e}")
            finally:
                if track_info and os.path.exists(track_info["filepath"]):
                    os.remove(track_info["filepath"])
                state.now_playing = None
        else:
            logger.warning(f"Failed to download track: {url}. Skipping.")

        if len(state.radio_playlist) < config.Constants.REFILL_THRESHOLD:
            asyncio.create_task(refill_playlist(context))

    logger.info("Radio loop finished.")