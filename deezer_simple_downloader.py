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
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def initialize(self):
        """Инициализация HTTP сессии."""
        self.session = aiohttp.ClientSession()
        logger.info("[DeezerSimple] Менеджер инициализирован")
        
    def setup_directories(self):
        """Создаёт директорию для загрузок."""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"[DeezerSimple] Директория загрузок: {DOWNLOADS_DIR}")
    
    def get_random_genre(self) -> str:
        """Возвращает случайный жанр для радио."""
        deezer_genres = ["rock", "pop", "hip hop", "electronic", "jazz", "lofi", "chill", "classical"]
        return random.choice(deezer_genres)
    
    async def _make_api_request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Выполняет запрос к Deezer Simple API."""
        if not self.session:
            await self.initialize()
            
        url = f"{self.API_BASE}/{endpoint}"
        params = params or {}
        
        try:
            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Ошибка API Deezer {endpoint}: {response.status}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при запросе к Deezer API: {endpoint}")
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
        
        search_data = await self._make_api_request(
            "search", {"q": query, "limit": limit}
        )
        
        if search_data and search_data.get('data'):
            tracks = search_data['data']
            # Фильтруем только те треки, у которых есть превью
            tracks_with_preview = [t for t in tracks if t.get('preview')]
            if tracks_with_preview:
                self._cache[cache_key] = tracks_with_preview
                return tracks_with_preview
        
        logger.warning(f"Трек не найден в Deezer или нет превью: '{query}'")
        return None
    
    async def _download_preview(self, preview_url: str, filepath: str) -> bool:
        """Скачивает превью-файл (30 секунд)."""
        if not self.session:
            await self.initialize()
            
        try:
            async with self.session.get(preview_url, timeout=30) as response:
                if response.status == 200:
                    content = await response.read()
                    if len(content) < 1024:  # Меньше 1KB - вероятно ошибка
                        logger.error(f"Слишком маленький файл: {len(content)} байт")
                        return False
                        
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(content)
                    return True
                else:
                    logger.error(f"Ошибка загрузки превью {preview_url}: {response.status}")
                    return False
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при загрузке превью: {preview_url}")
            return False
        except Exception as e:
            logger.error(f"Ошибка при скачивании превью: {e}")
            return False
    
    async def download_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Основной метод: ищет и скачивает превью трека."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск трека: '{query}'")
            
            # 1. Ищем треки
            tracks = await self._search_tracks(query, limit=5)
            if not tracks:
                logger.warning(f"Не найдено треков для запроса: '{query}'")
                return None
            
            # 2. Берём первый результат с превью
            track = tracks[0]
            track_id = track['id']
            title = track['title'][:100]
            artist = track['artist']['name'][:100]
            duration = 30  # Превью всегда 30 секунд
            
            logger.info(f"[DeezerSimple] Найден трек: {artist} - {title} (ID: {track_id})")
            
            # 3. Скачиваем превью
            preview_url = track['preview']
            file_hash = hashlib.md5(f"preview_{track_id}".encode()).hexdigest()[:8]
            filename = f"dz_preview_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            # Проверяем, не скачан ли уже файл
            if os.path.exists(filepath):
                logger.info(f"Файл уже существует: {filepath}")
            else:
                logger.info(f"Скачиваю превью: {preview_url}")
                success = await self._download_preview(preview_url, filepath)
                
                if not success:
                    logger.error(f"Не удалось скачать превью: {preview_url}")
                    return None
            
            # Проверяем размер файла
            if os.path.exists(filepath):
                file_size = os.path.getsize(filepath) / 1024  # КБ
                if file_size < 10:  # Если файл меньше 10 КБ - скорее всего ошибка
                    logger.error(f"Файл слишком мал ({file_size:.1f} КБ), удаляю")
                    try:
                        os.remove(filepath)
                    except:
                        pass
                    return None
                
                track_info = TrackInfo(
                    title=f"{title} (preview 30s)",
                    artist=artist,
                    duration=duration,
                    source="Deezer"
                )
                logger.info(f"[DeezerSimple] Превью готово: {filepath} ({file_size:.1f} КБ)")
                return (filepath, track_info)
            
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
            
            preview_url = longest_track['preview']
            track_id = longest_track['id']
            
            file_hash = hashlib.md5(f"long_preview_{track_id}".encode()).hexdigest()[:8]
            filename = f"dz_long_preview_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            if os.path.exists(filepath):
                logger.info(f"Файл уже существует: {filepath}")
            else:
                success = await self._download_preview(preview_url, filepath)
                if not success:
                    return None
            
            if os.path.exists(filepath):
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
        if self.session:
            await self.session.close()
            self.session = None
        self._cache.clear()
        logger.info("[DeezerSimple] Менеджер загрузки остановлен.")
