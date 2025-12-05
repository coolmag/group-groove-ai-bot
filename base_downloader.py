import asyncio
from typing import Optional
from dataclasses import dataclass

from config import TrackInfo, settings
from logger import logger


@dataclass
class DownloadResult:
    """Результат загрузки"""
    success: bool
    file_path: Optional[str] = None
    track_info: Optional[TrackInfo] = None
    error: Optional[str] = None


class BaseDownloader:
    """Базовый класс для загрузчиков"""
    
    def __init__(self):
        self.name = self.__class__.__name__
        self.semaphore = asyncio.Semaphore(3)  # Ограничение одновременных загрузок
    
    async def download(self, query: str) -> DownloadResult:
        """Загрузить трек (абстрактный метод)"""
        raise NotImplementedError
    
    async def download_with_retry(self, query: str) -> Optional[DownloadResult]:
        """Загрузка с повторными попытками"""
        for attempt in range(settings.MAX_RETRIES):
            try:
                async with self.semaphore:
                    result = await self.download(query)
                
                if result.success:
                    logger.info(f"{self.name}: Успешно '{query}' (попытка {attempt + 1})")
                    return result
                
                logger.warning(f"{self.name}: Ошибка '{query}': {result.error}")
                
            except asyncio.TimeoutError:
                logger.error(f"{self.name}: Таймаут '{query}' (попытка {attempt + 1})")
                result = DownloadResult(
                    success=False,
                    error="Таймаут загрузки"
                )
            except Exception as e:
                logger.error(f"{self.name}: Исключение '{query}': {e}")
                result = DownloadResult(
                    success=False,
                    error=str(e)
                )
            
            if attempt < settings.MAX_RETRIES - 1:
                await asyncio.sleep(settings.RETRY_DELAY * (attempt + 1))
        
        return DownloadResult(
            success=False,
            error=f"Не удалось после {settings.MAX_RETRIES} попыток"
        )