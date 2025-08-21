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

import config

logger = logging.getLogger(__name__) 

class AudioDownloadManager:
    """Manages searching and downloading audio from various sources."""

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
            cleaned_data = "\n".join(line.strip() for line in cookie_data.strip().splitlines() if line.strip())
            if not cleaned_data.strip().startswith("# Netscape HTTP Cookie File"):
                logger.error(f"Cookie data for {source_name} does not appear to be in Netscape format.")
                return None
            cookie_file.write_text(cleaned_data)
            return str(cookie_file)
        except Exception as e:
            logger.error(f"Failed to write temporary cookie file for {source_name}: {e}")
            return None

    async def _execute_yt_dlp_search(self, query: str, ydl_opts: dict) -> List[Dict]:
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

    async def _search_youtube(self, query: str) -> List[Dict]:
        search_query = f"ytsearch{config.Constants.SEARCH_LIMIT}:{query}"
        ydl_opts = {'cookiefile': self._get_cookie_file_from_data(config.YOUTUBE_COOKIES_DATA, "youtube")}
        return await self._execute_yt_dlp_search(search_query, ydl_opts)

    async def _search_soundcloud(self, query: str) -> List[Dict]:
        search_query = f"scsearch{config.Constants.SEARCH_LIMIT}:{query}"
        return await self._execute_yt_dlp_search(search_query, {})

    async def _search_vk(self, query: str) -> List[Dict]:
        cookie_path = self._get_cookie_file_from_data(config.VK_COOKIES_DATA, "vk")
        if not cookie_path:
            return []
        search_query = f"vksearch{config.Constants.SEARCH_LIMIT}:{query}"
        ydl_opts = {'cookiefile': cookie_path}
        return await self._execute_yt_dlp_search(search_query, ydl_opts)

    async def _search_archive(self, query: str) -> List[Dict]:
        search_url = f"https://archive.org/advancedsearch.php?q={quote(query)} AND mediatype:(audio)&fl[]=identifier,title,duration&sort[]=downloads desc&rows=10&output=json"
        try:
            async with aiohttp.ClientSession() as session, session.get(search_url) as response:
                response.raise_for_status()
                data = await response.json()
                docs = data.get("response", {}).get("docs", [])
                return [{"url": f"https://archive.org/details/{doc['identifier']}", "title": doc.get("title", "Unknown"), "duration": float(doc.get("duration", 0))} for doc in docs if doc.get('identifier')]
        except Exception as e:
            logger.error(f"Internet Archive search failed: {e}")
            return []

    async def search_tracks(self, source: str, query: str) -> List[Dict]:
        search_function = self._source_map.get(source)
        if not search_function:
            logger.error(f"Unknown source requested: {source}")
            return []
        return await search_function(query)

    async def download_track(self, url: str) -> Optional[Dict[str, any]]:
        filepath = None
        try:
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': str(config.DOWNLOAD_DIR / '%(id)s.%(ext)s'),
                'noplaylist': True,
                'quiet': True,
                'ignoreerrors': True,
                'socket_timeout': config.Constants.DOWNLOAD_TIMEOUT,
                'retries': config.Constants.MAX_RETRIES,
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