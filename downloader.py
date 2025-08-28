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

    def download_audio(self, query: str) -> dict | None:
        """Находит видео, извлекает метаданные, скачивает аудио и возвращает информацию."""
        try:
            search_query = f"ytsearch1:{query}"
            
            # Сначала извлекаем информацию, чтобы получить метаданные
            info_opts = {
                'quiet': True,
                'noplaylist': True,
            }
            if PROXY_ENABLED and PROXY_URL:
                info_opts['proxy'] = PROXY_URL
            if YOUTUBE_COOKIES_PATH:
                info_opts['cookiefile'] = YOUTUBE_COOKIES_PATH

            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                entry = info['entries'][0]
                title = entry.get('title', query)
                artist = entry.get('artist') or entry.get('uploader', 'Unknown Artist')

            # Теперь скачиваем аудио с нужным именем файла
            filename = f"{str(uuid4())}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)

            download_opts = {
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
                download_opts['proxy'] = PROXY_URL
            if YOUTUBE_COOKIES_PATH:
                download_opts['cookiefile'] = YOUTUBE_COOKIES_PATH

            logger.info(f"Starting download for: '{title}'")
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([entry['webpage_url']])
            
            if os.path.exists(filepath):
                logger.info(f"Successfully downloaded to {filepath}")
                return {
                    'filepath': filepath,
                    'title': title,
                    'artist': artist,
                    'duration': duration,
                    'filename': f"{title}.mp3"
                }
            else:
                logger.error("Download finished, but file not found.")
                return None

        except Exception as e:
            logger.error(f"An error occurred during download: {e}")
            return None
