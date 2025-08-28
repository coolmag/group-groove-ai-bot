import logging
import os
import yt_dlp
from uuid import uuid4

from config import (
    PROXY_ENABLED, PROXY_URL, YOUTUBE_COOKIES_PATH, DOWNLOADS_DIR
)

logger = logging.getLogger(__name__)

class AudioDownloader:
    def __init__(self):
        if not os.path.exists(DOWNLOADS_DIR):
            os.makedirs(DOWNLOADS_DIR)

    def download_audio(self, query: str) -> str | None:
        """Находит видео по запросу, скачивает аудио и возвращает путь к файлу."""
        try:
            search_query = f"ytsearch1:{query}"
            filename = f"{str(uuid4())}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)

            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': filepath,
                'noplaylist': True,
                'quiet': True,
            }

            if PROXY_ENABLED and PROXY_URL:
                ydl_opts['proxy'] = PROXY_URL

            if YOUTUBE_COOKIES_PATH:
                ydl_opts['cookiefile'] = YOUTUBE_COOKIES_PATH

            logger.info(f"Starting download for query: '{query}'")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([search_query])
            
            if os.path.exists(filepath):
                logger.info(f"Successfully downloaded to {filepath}")
                return filepath
            else:
                logger.error("Download finished, but file not found.")
                return None

        except Exception as e:
            logger.error(f"An error occurred during download: {e}")
            return None