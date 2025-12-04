import os
import logging
import asyncio
import random
import aiohttp
import aiofiles
from typing import Optional, Tuple, List
import hashlib

from config import DOWNLOADS_DIR, TrackInfo
from locks import download_lock

logger = logging.getLogger(__name__)

class DeezerSimpleDownloadManager:
    """Менеджер загрузки через публичный Deezer Simple API (только превью 30 сек)."""
    
    API_BASE = "https://api.deezer.com"
    
    def __init__(self):
        self.setup_directories()
        self._cache = {}
        
    def setup_directories(self):
        """Создаёт директорию для загрузок."""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"[DeezerSimple] Директория загрузок: {DOWNLOADS_DIR}")
    
    def get_random_genre(self) -> str:
        """Возвращает случайный жанр для радио."""
        deezer_genres = ["rock", "pop", "hip hop", "electronic", "jazz", "lofi", "chill", "classical"]
        return random.choice(deezer_genres)
    
    async def _make_api_request(self, session: aiohttp.ClientSession, endpoint: str, params: dict = None) -> Optional[dict]:
        """Выполняет запрос к Deezer Simple API."""
        url = f"{self.API_BASE}/{endpoint}"
        params = params or {}
        
        try:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Ошибка API Deezer {endpoint}: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Сетевая ошибка при запросе к Deezer: {e}")
            return None
    
    async def _search_tracks(self, query: str, limit: int = 5) -> Optional[List[dict]]:
        """Ищет треки по запросу."""
        cache_key = f"search:{query}:{limit}"
        if cache_key in self._cache:
            logger.debug(f"Использую кэшированные результаты для: {query}")
            return self._cache[cache_key]
        
        async with aiohttp.ClientSession() as session:
            search_data = await self._make_api_request(
                session, "search", {"q": query, "limit": limit}
            )
            
            if search_data and search_data.get('data'):
                tracks = search_data['data']
                self._cache[cache_key] = tracks
                return tracks
        
        logger.warning(f"Трек не найден в Deezer: '{query}'")
        return None
    
    async def _download_preview(self, session: aiohttp.ClientSession, preview_url: str, filepath: str) -> bool:
        """Скачивает превью-файл (30 секунд)."""
        try:
            async with session.get(preview_url) as response:
                if response.status == 200:
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(await response.read())
                    return True
                else:
                    logger.error(f"Ошибка загрузки превью {preview_url}: {response.status}")
                    return False
        except Exception as e:
            logger.error(f"Ошибка при скачивании превью: {e}")
            return False
    
    async def download_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Основной метод: ищет и скачивает превью трека."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск трека: '{query}'")
            
            # 1. Ищем треки
            tracks = await self._search_tracks(query, limit=3)
            if not tracks:
                return None
            
            # 2. Берём первый результат
            track = tracks[0]
            track_id = track['id']
            title = track['title'][:100]
            artist = track['artist']['name'][:100]
            duration = 30  # Превью всегда 30 секунд
            
            logger.info(f"[DeezerSimple] Найден трек: {artist} - {title}")
            
            # 3. Проверяем наличие превью
            preview_url = track.get('preview')
            if not preview_url:
                logger.error(f"У трека {track_id} нет превью-ссылки")
                return None
            
            # 4. Скачиваем превью
            file_hash = hashlib.md5(f"preview_{track_id}".encode()).hexdigest()[:8]
            filename = f"dz_preview_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            async with aiohttp.ClientSession() as session:
                success = await self._download_preview(session, preview_url, filepath)
                
                if success and os.path.exists(filepath):
                    # Проверяем размер файла (превью должно быть небольшим)
                    file_size = os.path.getsize(filepath) / 1024  # КБ
                    if file_size < 10:  # Если файл меньше 10 КБ - скорее всего ошибка
                        logger.error(f"Скачанный файл слишком мал ({file_size:.1f} КБ), вероятно ошибка")
                        os.remove(filepath)
                        return None
                    
                    track_info = TrackInfo(
                        title=f"{title} (preview 30s)",
                        artist=artist,
                        duration=duration,
                        source="Deezer"
                    )
                    logger.info(f"[DeezerSimple] Превью скачано: {filepath} ({file_size:.1f} КБ)")
                    return (filepath, track_info)
                else:
                    logger.error(f"[DeezerSimple] Не удалось скачать превью: {preview_url}")
                    return None
    
    async def download_longest_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Для аудиокниг: ищет самый длинный трек (тоже будет превью)."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск длинного трека: '{query}'")
            
            tracks = await self._search_tracks(query, limit=10)
            if not tracks:
                return None
            
            # Ищем трек с максимальной длительностью (по данным API, не превью)
            longest_track = max(tracks, key=lambda x: x.get('duration', 0))
            
            preview_url = longest_track.get('preview')
            if not preview_url:
                return None
            
            file_hash = hashlib.md5(f"long_preview_{longest_track['id']}".encode()).hexdigest()[:8]
            filename = f"dz_long_preview_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            async with aiohttp.ClientSession() as session:
                success = await self._download_preview(session, preview_url, filepath)
                
                if success and os.path.exists(filepath):
                    track_info = TrackInfo(
                        title=f"{longest_track['title'][:97]}... (preview)",
                        artist=longest_track['artist']['name'][:100],
                        duration=30,  # Превью всегда 30 секунд
                        source="Deezer"
                    )
                    return (filepath, track_info)
            
            return None
    
    async def close(self):
        """Очистка ресурсов."""
        self._cache.clear()
        logger.info("[DeezerSimple] Менеджер загрузки остановлен.")