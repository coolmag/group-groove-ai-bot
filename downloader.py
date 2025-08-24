import os
import logging
import asyncio
import random
from typing import Optional, Tuple
import yt_dlp

from config import DOWNLOADS_DIR, GENRES, Source, TrackInfo, PROXY_URL, PROXY_ENABLED, YOUTUBE_COOKIES_PATH

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        self.setup_directories()
        
    def setup_directories(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        
    def get_random_genre(self) -> str:
        return random.choice(GENRES)

    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Downloading: '{query}' from {source.value}")
        
        methods = {
            Source.YOUTUBE: self.download_from_youtube,
            Source.YOUTUBE_MUSIC: self.download_from_youtube_music,
            Source.SOUNDCLOUD: self.download_from_soundcloud,
            Source.JAMENDO: self.download_from_jamendo,
            Source.ARCHIVE: self.download_from_archive,
        }
        
        if source not in methods:
            logger.error(f"Unsupported source: {source}")
            return None, None
            
        return await methods[source](query)

    def get_ydl_opts(self):
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': False,
            'socket_timeout': 30,
            'retries': 3,
            'extract_flat': False,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        if YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts['cookiefile'] = YOUTUBE_COOKIES_PATH
            logger.info("Using YouTube cookies for authentication.")
        
        if PROXY_ENABLED and PROXY_URL:
            opts['proxy'] = PROXY_URL
            logger.info(f"Attempting to use proxy: {PROXY_URL}")
        
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        
        return opts

    async def _execute_download(self, search_query: str) -> Optional[Tuple[str, TrackInfo]]:
        try:
            ydl_opts = self.get_ydl_opts()
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, search_query, download=True)
                
                if not info:
                    logger.warning(f"No info extracted for: {search_query}")
                    return None, None

                entry = info.get('entries', [info])[0]
                if not entry:
                    logger.warning(f"No valid entry found for: {search_query}")
                    return None, None

                filename = ydl.prepare_filename(entry)
                mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'
                
                track_info = TrackInfo(
                    title=entry.get('title', 'Unknown'),
                    artist=entry.get('uploader', 'Unknown Artist'),
                    duration=entry.get('duration', 0),
                    source=entry.get('extractor_key', 'Unknown')
                )
                
                return mp3_filename, track_info

        except Exception as e:
            logger.error(f"Download execution failed for '{search_query}': {e}")
            return None, None

    async def download_from_youtube(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        return await self._execute_download(f"ytsearch1:{query}")

    async def download_from_youtube_music(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        return await self._execute_download(f"ytmsearch1:{query}")

    async def download_from_soundcloud(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        return await self._execute_download(f"scsearch1:{query}")

    async def download_from_jamendo(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        return await self._execute_download(f"jamendosearch1:{query}")

    async def download_from_archive(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        return await self._execute_download(f"iasearch1:{query}")