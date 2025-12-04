import os
import logging
import asyncio
import json
import hashlib
from typing import Optional, Tuple, Dict
from datetime import datetime

from config import DOWNLOADS_DIR, TrackInfo, Source
from downloader import AudioDownloadManager
from locks import download_lock

logger = logging.getLogger(__name__)

class SimpleYouTubeDownloader:
    """Простой YouTube загрузчик с файловым кэшем."""
    
    def __init__(self):
        self.youtube_downloader = AudioDownloadManager()
        self.cache_file = os.path.join("/tmp", "yt_cache.json") if os.path.exists("/tmp") else "yt_cache.json"
        self.cache = self._load_cache()
        logger.info(f"[SimpleYouTube] Загрузчик инициализирован. Кэш: {self.cache_file}")
    
    def _load_cache(self) -> Dict:
        """Загружает кэш из файла."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    
                    # Очищаем устаревшие записи (старше 7 дней)
                    cleaned_cache = {}
                    now = datetime.now().timestamp()
                    week_ago = now - (7 * 24 * 3600)
                    
                    for key, data in cache_data.items():
                        if data.get('timestamp', 0) > week_ago:
                            cleaned_cache[key] = data
                    
                    # Если очистили старые записи, сохраняем обновленный кэш
                    if len(cleaned_cache) != len(cache_data):
                        self._save_cache(cleaned_cache)
                    
                    return cleaned_cache
        except Exception as e:
            logger.error(f"[SimpleYouTube] Ошибка загрузки кэша: {e}")
        
        return {}
    
    def _save_cache(self, cache_data: Dict = None):
        """Сохраняет кэш в файл."""
        try:
            data_to_save = cache_data or self.cache
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[SimpleYouTube] Ошибка сохранения кэша: {e}")
    
    def _get_cache_key(self, query: str, source: Source) -> str:
        """Создаёт ключ для кэша."""
        key_string = f"{source.value}:{query.lower().strip()}"
        return hashlib.md5(key_string.encode()).hexdigest()[:16]
    
    def get_cached_data(self, query: str, source: Source) -> Optional[Dict]:
        """Ищет данные в кэше."""
        cache_key = self._get_cache_key(query, source)
        cached = self.cache.get(cache_key)
        
        if cached:
            # Проверяем срок годности (максимум 30 дней)
            timestamp = cached.get('timestamp', 0)
            max_age = datetime.now().timestamp() - (30 * 24 * 3600)
            
            if timestamp > max_age:
                logger.info(f"[SimpleYouTube] Найден кэш для: {query}")
                return cached
        
        return None
    
    def add_to_cache(self, query: str, source: Source, video_id: str, 
                    title: str, artist: str, duration: int, success: bool = True):
        """Добавляет данные в кэш."""
        cache_key = self._get_cache_key(query, source)
        
        self.cache[cache_key] = {
            'video_id': video_id,
            'title': title,
            'artist': artist,
            'duration': duration,
            'success': success,
            'timestamp': datetime.now().timestamp(),
            'query': query,
            'source': source.value
        }
        
        # Ограничиваем размер кэша (максимум 100 записей)
        if len(self.cache) > 100:
            # Удаляем самые старые записи
            oldest_keys = sorted(
                self.cache.keys(),
                key=lambda k: self.cache[k].get('timestamp', 0)
            )[:len(self.cache) - 100]
            
            for key in oldest_keys:
                del self.cache[key]
        
        # Сохраняем кэш
        self._save_cache()
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек с использованием кэша."""
        async with download_lock:
            # 1. Проверяем кэш
            cached = self.get_cached_data(query, source)
            
            if cached and cached.get('success', False):
                try:
                    logger.info(f"[SimpleYouTube] Использую кэш для: {query}")
                    result = await self.youtube_downloader.download_track(
                        cached['video_id'], 
                        source
                    )
                    
                    if result:
                        return result
                    else:
                        # Помечаем как неудачный
                        self.add_to_cache(query, source, cached['video_id'],
                                         cached['title'], cached['artist'],
                                         cached['duration'], success=False)
                except Exception as e:
                    logger.warning(f"[SimpleYouTube] Кэш не сработал: {e}")
            
            # 2. Обычный поиск
            logger.info(f"[SimpleYouTube] Новый поиск: '{query}'")
            result = await self.youtube_downloader.download_track(query, source)
            
            if result:
                audio_path, track_info = result
                video_id = getattr(self.youtube_downloader, 'last_video_id', None)
                
                if video_id:
                    # Добавляем в кэш
                    self.add_to_cache(query, source, video_id,
                                     track_info.title, track_info.artist,
                                     track_info.duration, success=True)
            
            return result
    
    async def download_longest_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек."""
        async with download_lock:
            cached = self.get_cached_data(f"long_{query}", source)
            
            if cached and cached.get('success', False):
                try:
                    result = await self.youtube_downloader.download_longest_track(
                        cached['video_id'], 
                        source
                    )
                    if result:
                        return result
                except:
                    pass
            
            result = await self.youtube_downloader.download_longest_track(query, source)
            
            if result:
                audio_path, track_info = result
                video_id = getattr(self.youtube_downloader, 'last_video_id', None)
                
                if video_id:
                    self.add_to_cache(f"long_{query}", source, video_id,
                                     track_info.title, track_info.artist,
                                     track_info.duration, success=True)
            
            return result
    
    async def close(self):
        """Закрывает соединения."""
        await self.youtube_downloader.close()
        # Сохраняем кэш при закрытии
        self._save_cache()
        logger.info("[SimpleYouTube] Загрузчик остановлен")
