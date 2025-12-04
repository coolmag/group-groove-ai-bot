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
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
            logger.info("[DeezerSimple] HTTP сессия создана")
    
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
                        tracks_with_preview = []
                        for track in data['data']:
                            if track.get('preview'):
                                tracks_with_preview.append(track)
                        
                        if tracks_with_preview:
                            return tracks_with_preview
                
                logger.warning(f"[DeezerSimple] Не найдено треков с превью для: {query}")
                return None
                
        except asyncio.TimeoutError:
            logger.error(f"[DeezerSimple] Таймаут при поиске: {query}")
            return None
        except Exception as e:
            logger.error(f"[DeezerSimple] Ошибка поиска {query}: {e}")
            return None
    
    async def download_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает превью трека."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск: '{query}'")
            
            tracks = await self._search_tracks(query, limit=5)
            if not tracks:
                return None
            
            # Берем первый трек с превью
            track = tracks[0]
            track_id = track['id']
            title = track['title'][:100]
            artist = track['artist']['name'][:100]
            preview_url = track['preview']
            
            logger.info(f"[DeezerSimple] Найден: {artist} - {title}")
            
            # Скачиваем превью
            file_hash = hashlib.md5(f"dz_{track_id}".encode()).hexdigest()[:8]
            filename = f"dz_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            try:
                await self.initialize()
                
                async with self.session.get(preview_url, timeout=30) as response:
                    if response.status == 200:
                        content = await response.read()
                        
                        # Проверяем размер (должен быть хотя бы 50KB для 30-секундного превью)
                        if len(content) < 50 * 1024:
                            logger.warning(f"[DeezerSimple] Слишком маленький файл: {len(content)} байт")
                            return None
                        
                        async with aiofiles.open(filepath, 'wb') as f:
                            await f.write(content)
                        
                        # Проверяем что файл создан
                        if os.path.exists(filepath):
                            file_size = os.path.getsize(filepath) / 1024  # KB
                            logger.info(f"[DeezerSimple] Скачано: {file_size:.1f} KB")
                            
                            track_info = TrackInfo(
                                title=f"{title} (preview 30s)",
                                artist=artist,
                                duration=30,
                                source="Deezer"
                            )
                            
                            return (filepath, track_info)
                        else:
                            logger.error(f"[DeezerSimple] Файл не создан: {filepath}")
                            return None
                    else:
                        logger.error(f"[DeezerSimple] Ошибка HTTP {response.status} при загрузке")
                        return None
                        
            except asyncio.TimeoutError:
                logger.error(f"[DeezerSimple] Таймаут при загрузке превью")
                return None
            except Exception as e:
                logger.error(f"[DeezerSimple] Ошибка загрузки: {e}")
                return None
    
    async def download_longest_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек (превью)."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск длинного трека: '{query}'")
            
            tracks = await self._search_tracks(query, limit=10)
            if not tracks:
                return None
            
            # Ищем самый длинный трек
            longest_track = max(tracks, key=lambda x: x.get('duration', 0))
            
            # Используем общий метод скачивания
            return await self.download_track(longest_track['title'])
    
    async def close(self):
        """Закрывает сессию."""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("[DeezerSimple] Сессия закрыта")
        logger.info("[DeezerSimple] Загрузчик остановлен")
