import os
import logging
import asyncio
import random
import uuid
from typing import Optional, Tuple
import yt_dlp

from config import DOWNLOADS_DIR, GENRES, Source, TrackInfo, PROXY_URL, PROXY_ENABLED, YOUTUBE_COOKIES_PATH
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        self.setup_directories()

    def setup_directories(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def get_random_genre(self) -> str:
        return random.choice(GENRES)

    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Initiating download for: '{query}' from {source.value}")

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

        # The actual download execution is now wrapped with a lock and has proper error handling
        return await methods[source](query)

    def get_ydl_opts(self, unique_id: str):
        """Generates yt-dlp options with a unique output template."""
        from config import TEMP_COOKIE_PATH  # Импортируем здесь, чтобы избежать циклических зависимостей

        opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(DOWNLOADS_DIR, f'{unique_id}.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': False,
            'socket_timeout': 30,
            'retries': 3,
            'extract_flat': False,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        }

        # НОВАЯ ЛОГИКА: Приоритет отдается cookies из переменной окружения
        if TEMP_COOKIE_PATH:
            opts['cookiefile'] = TEMP_COOKIE_PATH
            logger.info(f"Using temporary cookie file from environment variable: {TEMP_COOKIE_PATH}")
        elif YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts['cookiefile'] = YOUTUBE_COOKIES_PATH
            logger.info(f"Using YouTube cookies from file path: {YOUTUBE_COOKIES_PATH}")
        else:
            logger.warning("No YouTube cookies provided. Downloads may be blocked or restricted.")

        if PROXY_ENABLED and PROXY_URL:
            opts['proxy'] = PROXY_URL
            logger.info(f"Attempting to use proxy: {PROXY_URL}")

        return opts

    async def _execute_download(self, search_query: str) -> Optional[Tuple[str, TrackInfo]]:
        """
        Executes the download and post-processing, ensuring thread safety and
        correct file path retrieval.
        """
        unique_id = str(uuid.uuid4())
        
        async with download_lock:  # Ensure only one download happens at a time
            logger.info(f"Download lock acquired for '{search_query}' (ID: {unique_id})")
            try:
                ydl_opts = self.get_ydl_opts(unique_id)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Run the blocking ydl.extract_info in a separate thread
                    info = await asyncio.to_thread(ydl.extract_info, search_query, download=True)

                    if not info:
                        logger.warning(f"No info extracted for: {search_query}")
                        return None, None

                    entry = info.get('entries', [info])[0]
                    if not entry:
                        logger.warning(f"No valid entry found for: {search_query}")
                        return None, None
                    
                    # CRITICAL FIX: After post-processing, yt-dlp updates the 'filepath' key.
                    # We read this final path instead of guessing the filename.
                    final_filepath = entry.get('filepath')
                    if not final_filepath or not os.path.exists(final_filepath):
                         logger.error(f"Post-processing failed: MP3 file not found at expected path for '{search_query}'")
                         # Fallback to guessing, though it's not ideal
                         final_filepath = os.path.join(DOWNLOADS_DIR, f"{unique_id}.mp3")
                         if not os.path.exists(final_filepath):
                             return None, None

                    track_info = TrackInfo(
                        title=entry.get('title', 'Unknown'),
                        artist=entry.get('uploader', 'Unknown Artist'),
                        duration=int(entry.get('duration', 0)),
                        source=entry.get('extractor_key', 'Unknown')
                    )
                    
                    logger.info(f"Download successful for '{search_query}'. File: {final_filepath}")
                    return final_filepath, track_info

            except Exception as e:
                logger.error(f"Download execution failed for '{search_query}': {e}", exc_info=True)
                return None, None
            finally:
                logger.info(f"Download lock released for '{search_query}' (ID: {unique_id})")


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