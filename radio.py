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

# --- Загрузка Cookies из переменных окружения ---
VK_COOKIES_CONTENT = os.getenv("VK_COOKIES_DATA")
if VK_COOKIES_CONTENT:
    VK_COOKIES_PATH = os.path.join(DOWNLOADS_DIR, "vk_cookies.txt")
    with open(VK_COOKIES_PATH, "w", encoding='utf-8') as f:
        f.write(VK_COOKIES_CONTENT)
    logger.info("VK cookies loaded successfully.")
else:
    VK_COOKIES_PATH = None
    logger.warning("VK_COOKIES_DATA environment variable not found.")

YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES_DATA")
if YOUTUBE_COOKIES_CONTENT:
    YOUTUBE_COOKIES_PATH = os.path.join(DOWNLOADS_DIR, "youtube_cookies.txt")
    with open(YOUTUBE_COOKIES_PATH, "w", encoding='utf-8') as f:
        f.write(YOUTUBE_COOKIES_CONTENT)
    logger.info("YouTube cookies loaded successfully.")
else:
    YOUTUBE_COOKIES_PATH = None
    logger.warning("YOUTUBE_COOKIES_DATA environment variable not found.")

class AudioDownloadManager:
    def __init__(self):
        self.base_ydl_opts = {
            # Самый надежный способ скачать только аудио
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
            # Эта опция может помочь избежать некоторых проблем с форматами
            'compat_options': {'check-formats': 'warn'}
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
                # Добавляем таймаут в 30 секунд на операцию скачивания
                info = await asyncio.wait_for(
                    asyncio.to_thread(ydl.extract_info, query, download=True),
                    timeout=30.0
                )
            
            entry = info.get('entries', [info])[0]

            audio_path = os.path.join(DOWNLOADS_DIR, f"{entry['id']}.mp3")
            track_info = TrackInfo(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', 'Unknown Artist'),
                duration=int(entry.get('duration', 0))
            )
            logger.info(f"Download successful: {track_info.title}")
            return audio_path, track_info
        except asyncio.TimeoutError:
            logger.error(f"Download timed out for query: {query}")
            return None, None
        except yt_dlp.utils.DownloadError as e:
            # Не выводим полный стектрейс для этой ошибки, т.к. она ожидаема
            logger.error(f"yt-dlp download error for query '{query}': {e}")
            return None, None
        except Exception as e:
            logger.error(f"Generic error in _execute_yt_dlp for query '{query}': {e}", exc_info=True)
            return None, None

    async def search_and_download_youtube(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Searching YouTube for: {query}")
        opts = self.base_ydl_opts.copy()
        if YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts['cookiefile'] = YOUTUBE_COOKIES_PATH
            logger.info("Using YouTube cookies for download.")
        else:
            logger.warning("YouTube cookies not found, proceeding without them.")
        return await self._execute_yt_dlp(f"ytsearch1:{query}", opts)

    async def search_and_download_vk(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        if not (VK_COOKIES_PATH and os.path.exists(VK_COOKIES_PATH)):
            logger.warning("VK search skipped: vk_cookies.txt not found or loaded.")
            return None, None
        logger.info(f"Searching VK for: {query}")
        opts = self.base_ydl_opts.copy()
        opts['cookiefile'] = VK_COOKIES_PATH
        logger.info("Using VK cookies for download.")
        return await self._execute_yt_dlp(f"vksearch1:{query}", opts)

    async def get_random_from_archive(self) -> Optional[Tuple[str, TrackInfo]]:
        logger.info("Getting random track from Internet Archive")
        query = 'collection:etree AND mediatype:audio AND format:"VBR MP3"'
        opts = self.base_ydl_opts.copy()
        return await self._execute_yt_dlp(f"iasearch1:{query}", opts)