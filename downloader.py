import logging
import os
import yt_dlp
from uuid import uuid4

from config import (
    PROXY_ENABLED, PROXY_URL, YOUTUBE_COOKIES_PATH, DOWNLOADS_DIR
)

logger = logging.getLogger(__name__)

class SmartDownloader:
    def __init__(self):
        if not os.path.exists(DOWNLOADS_DIR):
            os.makedirs(DOWNLOADS_DIR)

    def _get_ydl_opts(self, filepath):
        opts = {
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
            opts['proxy'] = PROXY_URL
        if YOUTUBE_COOKIES_PATH:
            opts['cookiefile'] = YOUTUBE_COOKIES_PATH
        return opts

    def download_media(self, query: str, search_type: str = 'music') -> dict | None:
        try:
            # 1. Ищем 5 результатов без скачивания
            search_opts = {
                'quiet': True,
                'noplaylist': True,
            }
            if PROXY_ENABLED and PROXY_URL:
                search_opts['proxy'] = PROXY_URL
            if YOUTUBE_COOKIES_PATH:
                search_opts['cookiefile'] = YOUTUBE_COOKIES_PATH

            with yt_dlp.YoutubeDL(search_opts) as ydl:
                results = ydl.extract_info(f"ytsearch5:{query}", download=False)['entries']

            if not results:
                logger.warning(f"No search results for query: {query}")
                return None

            # 2. Выбираем подходящее видео
            target_entry = None
            if search_type == 'music':
                # Для музыки ищем первое видео короче 15 минут (900 секунд)
                for entry in results:
                    if entry.get('duration', 0) < 900:
                        target_entry = entry
                        break
            elif search_type == 'audiobook':
                # Для аудиокниг ищем самое длинное видео
                target_entry = max(results, key=lambda e: e.get('duration', 0))

            if not target_entry:
                logger.warning(f"No suitable video found for query: {query} with type: {search_type}")
                return None

            # 3. Скачиваем выбранное видео
            title = target_entry.get('title', query)
            artist = target_entry.get('artist') or target_entry.get('uploader', 'Unknown')
            duration = target_entry.get('duration', 0)
            
            filename = f"{str(uuid4())}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            download_opts = self._get_ydl_opts(filepath)

            logger.info(f"Starting download for: '{title}'")
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([target_entry['webpage_url']])

            if os.path.exists(filepath):
                logger.info(f"Successfully downloaded to {filepath}")
                return {
                    'filepath': filepath,
                    'title': title,
                    'artist': artist,
                    'duration': duration,
                    'filename': f"{title}.mp3"
                }
            return None

        except Exception as e:
            logger.error(f"An error occurred during download: {e}")
            return None