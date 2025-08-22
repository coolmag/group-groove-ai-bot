import os
import logging
import asyncio
import random
from typing import Optional, Tuple
import yt_dlp

from config import DOWNLOADS_DIR, GENRES, Source, TrackInfo
from locks import download_lock

# Настройка логирования
logger = logging.getLogger(__name__)

# Получение cookies из переменных окружения
VK_COOKIES_CONTENT = os.getenv("VK_COOKIES")
VK_COOKIES_PATH = os.path.join(DOWNLOADS_DIR, "vk_cookies.txt")
if VK_COOKIES_CONTENT:
    with open(VK_COOKIES_PATH, "w") as f:
        f.write(VK_COOKIES_CONTENT)

YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES")
YOUTUBE_COOKIES_PATH = os.path.join(DOWNLOADS_DIR, "youtube_cookies.txt")
if YOUTUBE_COOKIES_CONTENT:
    with open(YOUTUBE_COOKIES_PATH, "w") as f:
        f.write(YOUTUBE_COOKIES_CONTENT)

class AudioDownloadManager:
    def __init__(self):
        self.base_ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }

    def get_random_genre(self) -> str:
        return random.choice(GENRES)

    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Starting download for query: '{query}' from source: {source.value}")
        async with download_lock:
            try:
                if source == Source.YOUTUBE:
                    return await self.search_and_download_youtube(query)
                elif source == Source.VK:
                    return await self.search_and_download_vk(query)
                elif source == Source.ARCHIVE:
                    return await self.get_random_from_archive()
            except Exception as e:
                logger.error(f"Failed to download from {source.value} with query '{query}': {e}", exc_info=True)
            return None, None

    async def _execute_yt_dlp(self, query: str, ydl_opts: dict) -> Optional[Tuple[str, TrackInfo]]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=True)
            
            if not info.get('entries'):
                # Single video result
                entry = info
            else:
                # Search result, pick the first one
                entry = info['entries'][0]

            audio_path = os.path.join(DOWNLOADS_DIR, f"{entry['id']}.mp3")
            track_info = TrackInfo(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', 'Unknown Artist'),
                duration=int(entry.get('duration', 0))
            )
            logger.info(f"Download successful: {track_info.title}")
            return audio_path, track_info
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp download error for query '{query}': {e}")
            return None, None
        except Exception as e:
            logger.error(f"Generic error in _execute_yt_dlp for query '{query}': {e}", exc_info=True)
            return None, None

    async def search_and_download_youtube(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Searching YouTube for: {query}")
        opts = self.base_ydl_opts.copy()
        opts['cookiefile'] = YOUTUBE_COOKIES_PATH if os.path.exists(YOUTUBE_COOKIES_PATH) else None
        return await self._execute_yt_dlp(f"ytsearch1:{query}", opts)

    async def search_and_download_vk(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        if not os.path.exists(VK_COOKIES_PATH):
            logger.warning("VK search skipped: vk_cookies.txt not found.")
            return None, None
        logger.info(f"Searching VK for: {query}")
        opts = self.base_ydl_opts.copy()
        opts['cookiefile'] = VK_COOKIES_PATH
        return await self._execute_yt_dlp(f"vksearch1:{query}", opts)

    async def get_random_from_archive(self) -> Optional[Tuple[str, TrackInfo]]:
        logger.info("Getting random track from Internet Archive")
        # Пример запроса к Internet Archive (можно усложнить)
        query = 'collection:etree AND mediatype:audio AND format:"VBR MP3"'
        opts = self.base_ydl_opts.copy()
        return await self._execute_yt_dlp(f"iasearch:{query}", opts)
