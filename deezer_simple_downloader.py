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
    """Улучшенный менеджер Deezer с таймаутами."""
    
    API_BASE = "https://api.deezer.com"
    
    def __init__(self):
        self.session = None
        logger.info("[DeezerSimple] Загрузчик инициализирован")
    
    async def initialize(self):
        """Инициализация сессии с таймаутами."""
        if not self.session:
            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=10,
                sock_read=20
            )
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )
            logger.info("[DeezerSimple] HTTP сессия создана")
    
    def get_random_genre(self) -> str:
        genres = ["rock", "pop", "hip hop", "electronic", "jazz", "lofi", "chill", "classical"]
        return random.choice(genres)
    
    async def _search_tracks(self, query: str, limit: int = 5) -> Optional[List[dict]]:
        """Ищет треки в Deezer с таймаутом."""
        await self.initialize()
        
        try:
            async with self.session.get(
                f"{self.API_BASE}/search",
                params={"q": query, "limit": limit},
                timeout=15
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('data'):
                        tracks_with_preview = [t for t in data['data'] if t.get('preview')]
                        if tracks_with_preview:
                            return tracks_with_preview
                
                return None
                
        except asyncio.TimeoutError:
            logger.error(f"[DeezerSimple] Таймаут поиска: {query}")
            return None
        except Exception as e:
            logger.error(f"[DeezerSimple] Ошибка поиска: {e}")
            return None
    
    async def download_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает превью трека."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск: '{query}'")
            
            tracks = await self._search_tracks(query, limit=5)
            if not tracks:
                return None
            
            track = tracks[0]
            track_id = track['id']
            title = track['title'][:100]
            artist = track['artist']['name'][:100]
            preview_url = track['preview']
            
            logger.info(f"[DeezerSimple] Найден: {artist} - {title}")
            
            file_hash = hashlib.md5(f"dz_{track_id}".encode()).hexdigest()[:8]
            filename = f"dz_{file_hash}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)
            
            try:
                await self.initialize()
                
                async with self.session.get(preview_url, timeout=20) as response:
                    if response.status == 200:
                        content = await response.read()
                        
                        if len(content) < 50 * 1024:
                            logger.warning(f"[DeezerSimple] Слишком маленький файл")
                            return None
                        
                        async with aiofiles.open(filepath, 'wb') as f:
                            await f.write(content)
                        
                        if os.path.exists(filepath):
                            file_size = os.path.getsize(filepath) / 1024
                            logger.info(f"[DeezerSimple] Скачано: {file_size:.1f} KB")
                            
                            track_info = TrackInfo(
                                title=f"{title} (preview 30s)",
                                artist=artist,
                                duration=30,
                                source="Deezer"
                            )
                            
                            return (filepath, track_info)
                        else:
                            return None
                    else:
                        logger.error(f"[DeezerSimple] Ошибка HTTP {response.status}")
                        return None
                        
            except asyncio.TimeoutError:
                logger.error(f"[DeezerSimple] Таймаут загрузки")
                return None
            except Exception as e:
                logger.error(f"[DeezerSimple] Ошибка загрузки: {e}")
                return None
    
    async def download_longest_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек."""
        async with download_lock:
            logger.info(f"[DeezerSimple] Поиск длинного трека: '{query}'")
            
            tracks = await self._search_tracks(query, limit=10)
            if not tracks:
                return None
            
            longest_track = max(tracks, key=lambda x: x.get('duration', 0))
            return await self.download_track(longest_track['title'])
    
    async def close(self):
        """Закрывает сессию."""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("[DeezerSimple] Сессия закрыта")