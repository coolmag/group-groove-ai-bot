import os
import logging
import asyncio
import random
import uuid
from typing import Optional, Tuple, List
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

    def _get_ydl_base_opts(self):
        """Возвращает базовые опции для yt-dlp, без специфики формата вывода."""
        from config import TEMP_COOKIE_PATH
        
        opts = {
            'noplaylist': True,
            'quiet': True,
            'no_warnings': False,
            'socket_timeout': 30,
            'retries': 3,
            'extract_flat': 'in_playlist', # Быстрое получение информации о результатах поиска
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
        }
        if TEMP_COOKIE_PATH:
            opts['cookiefile'] = TEMP_COOKIE_PATH
        elif YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts['cookiefile'] = YOUTUBE_COOKIES_PATH
        
        if PROXY_ENABLED and PROXY_URL:
            opts['proxy'] = PROXY_URL
        
        return opts

    async def _get_search_results(self, query: str, source: Source, count: int) -> List[str]:
        """Ищет видео и возвращает список URL-адресов кандидатов."""
        source_map = {
            Source.YOUTUBE: "ytsearch",
            Source.YOUTUBE_MUSIC: "ytmsearch",
            Source.SOUNDCLOUD: "scsearch",
            Source.JAMENDO: "jamendosearch",
            Source.ARCHIVE: "iasearch"
        }
        search_prefix = source_map.get(source)
        if not search_prefix:
            logger.error(f"Неподдерживаемый источник для поиска: {source}")
            return []

        search_query = f"{search_prefix}{count}:{query}"
        logger.info(f"Выполняю поиск кандидатов по запросу: '{search_query}'")
        
        ydl_opts = self._get_ydl_base_opts()
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, search_query, download=False)
                if not info or not info.get('entries'):
                    logger.warning(f"Поиск не дал результатов для: {query}")
                    return []
                
                # Возвращаем список URL-адресов
                return [entry['url'] for entry in info['entries'] if entry and 'url' in entry]
        except Exception as e:
            logger.error(f"Ошибка при поиске видео: {e}")
            return []

    async def _execute_single_download(self, video_url: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает одно видео по прямому URL."""
        unique_id = str(uuid.uuid4())
        
        ydl_opts = self._get_ydl_base_opts()
        # Обновляем опции для скачивания конкретного файла
        ydl_opts.update({
            'extract_flat': False, # Теперь нам нужна полная информация
            'outtmpl': os.path.join(DOWNLOADS_DIR, f'{unique_id}.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        })

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, video_url, download=True)
                
                if not info:
                    return None
                
                final_filepath = info.get('filepath')
                if not final_filepath or not os.path.exists(final_filepath):
                    logger.error(f"Файл не найден после постобработки для URL: {video_url}")
                    return None

                track_info = TrackInfo(
                    title=info.get('title', 'Unknown'),
                    artist=info.get('uploader', 'Unknown Artist'),
                    duration=int(info.get('duration', 0)),
                    source=info.get('extractor_key', 'Unknown')
                )
                logger.info(f"Успешно скачан трек: {track_info.title}")
                return final_filepath, track_info
        except Exception as e:
            logger.warning(f"Не удалось скачать кандидата {video_url}: {e}")
            return None

    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """
        Ищет несколько кандидатов и пытается скачать их по очереди,
        пока не будет получен первый успешный результат.
        """
        async with download_lock: # Блокируем весь процесс от поиска до скачивания
            candidate_urls = await self._get_search_results(query, source, count=5)
            
            if not candidate_urls:
                return None, None

            logger.info(f"Найдено {len(candidate_urls)} кандидатов. Начинаю скачивание по очереди...")
            
            for i, url in enumerate(candidate_urls):
                logger.info(f"Попытка {i+1}/{len(candidate_urls)}: {url}")
                result = await self._execute_single_download(url)
                if result:
                    return result # Возвращаем первый успешный результат

            logger.error(f"Не удалось скачать ни одного из {len(candidate_urls)} кандидатов для запроса '{query}'.")
            return None, None