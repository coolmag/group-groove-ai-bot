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
    """Менеджер загрузки через Deezer Simple API (только превью 30 сек)."""
    
    API_BASE = "https://api.deezer.com"
    
    def __init__(self):
        self.session = None
        logger.info("[DeezerSimple] Загрузчик инициализирован")
    
    async def initialize(self):
        """Инициализация сессии."""
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    
    def get_random_genre(self) -> str:
        """Возвращает случайный жанр для радио."""
        genres = ["rock", "pop", "hip hop", "electronic", "jazz", "lofi", "chill", "classical"]
        return random.choice(genres)
    
    async def _search_tracks(self, query: str, limit: int = 5) -> Optional[List[dict]]:
        """Ищет треки в Deezer."""
        await self.initialize()
        
        try:
            async with self.session.get(
                f"{self.API_BASE}/search",
                params={"q": query, "limit": limit}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data'):
                        # Фильтруем только треки с превью
                        return [t for t in data['data'] if t.get('preview')]
        except Exception as e:
            logger.error(f"[DeezerSimple] Ошибка поиска: {e}")
        
        return None
    
    async def download_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает превью трека."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск: '{query}'")
            
            tracks = await self._search_tracks(query, limit=3)
            if not tracks:
                return None
            
            track = tracks[0]
            track_id = track['id']
            title = track['title'][:100]
            artist = track['artist']['name'][:100]
            preview_url = track['preview']
            
            # Скачиваем превью
            file_hash = hashlib.md5(f"dz_{track_id}".encode()).hexdigest()[:8]
            filename = f"dz_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            try:
                async with self.session.get(preview_url) as response:
                    if response.status == 200:
                        content = await response.read()
                        async with aiofiles.open(filepath, 'wb') as f:
                            await f.write(content)
                        
                        track_info = TrackInfo(
                            title=f"{title} (preview 30s)",
                            artist=artist,
                            duration=30,
                            source="Deezer"
                        )
                        
                        logger.info(f"[DeezerSimple] Скачано: {artist} - {title}")
                        return (filepath, track_info)
            except Exception as e:
                logger.error(f"[DeezerSimple] Ошибка загрузки: {e}")
            
            return None
    
    async def download_longest_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек (превью)."""
        async with download_lock:
            tracks = await self._search_tracks(query, limit=10)
            if not tracks:
                return None
            
            longest = max(tracks, key=lambda x: x.get('duration', 0))
            return await self.download_track(longest['title'])
    
    async def close(self):
        """Закрывает сессию."""
        if self.session:
            await self.session.close()
