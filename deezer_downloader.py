import os
import hashlib
import aiohttp
from typing import Optional

from base_downloader import BaseDownloader, DownloadResult
from config import TrackInfo, settings, Source
from logger import logger
from cache import CacheManager


class DeezerDownloader(BaseDownloader):
    """Загрузчик Deezer"""
    
    def __init__(self):
        super().__init__()
        self.session: Optional[aiohttp.ClientSession] = None
        self.api_base = "https://api.deezer.com"
        self.cache = CacheManager()
    
    async def _get_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session
    
    async def download(self, query: str) -> DownloadResult:
        """Загрузка превью с Deezer"""
        # Проверяем кэш
        cached_result = await self.cache.get(query, Source.DEEZER)
        if cached_result:
            logger.info(f"[Deezer] Использую кэш для: {query}")
            return cached_result
            
        logger.info(f"[Deezer] Ищу '{query}'")
        try:
            session = await self._get_session()
            
            async with session.get(
                f"{self.api_base}/search",
                params={"q": query, "limit": 1}
            ) as response:
                if response.status != 200:
                    return DownloadResult(
                        success=False,
                        error=f"Ошибка API: {response.status}"
                    )
                
                data = await response.json()
                if not data.get('data'):
                    return DownloadResult(
                        success=False,
                        error="Треки не найдены"
                    )
                
                track = data['data'][0]
                preview_url = track.get('preview')
                
                if not preview_url:
                    return DownloadResult(
                        success=False,
                        error="Нет превью"
                    )
                
                # Скачиваем превью
                async with session.get(preview_url) as audio_response:
                    if audio_response.status != 200:
                        return DownloadResult(
                            success=False,
                            error="Ошибка загрузки превью"
                        )
                    
                    audio_data = await audio_response.read()
                    
                    # Сохраняем файл
                    track_id = track['id']
                    file_hash = hashlib.md5(f"dz_{track_id}".encode()).hexdigest()[:8]
                    filename = f"dz_{file_hash}.mp3"
                    filepath = os.path.join(settings.DOWNLOADS_DIR, filename)
                    
                    with open(filepath, 'wb') as f:
                        f.write(audio_data)
                    
                    track_info = TrackInfo(
                        title=f"{track['title'][:95]} (preview)",
                        artist=track['artist']['name'][:100],
                        duration=30,
                        source=Source.DEEZER.value
                    )
                    
                    result = DownloadResult(
                        success=True,
                        file_path=filepath,
                        track_info=track_info
                    )
                    
                    # Сохраняем в кэш
                    await self.cache.set(query, Source.DEEZER, result)
                    return result
                    
        except Exception as e:
            logger.error(f"Ошибка Deezer: {e}")
            return DownloadResult(success=False, error=str(e))
    
    async def download_long(self, query: str) -> DownloadResult:
        """Поиск длинных треков на Deezer (заглушка, т.к. Deezer не отдает полные треки)"""
        logger.info(f"[Deezer] Поиск длинного контента не поддерживается, ищу обычный трек: '{query}'")
        return await self.download(query)
    
    async def __del__(self):
        """Очистка сессии"""
        if self.session and not self.session.closed:
            await self.session.close()