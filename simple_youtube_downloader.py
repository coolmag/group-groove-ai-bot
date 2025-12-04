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
    """Простой YouTube загрузчик с улучшенным кэшированием."""
    
    def __init__(self):
        self.youtube_downloader = AudioDownloadManager()
        self.cache_file = os.path.join("/tmp", "yt_cache.json") if os.path.exists("/tmp") else "yt_cache.json"
        self.cache = self._load_cache()
        logger.info(f"[SimpleYouTube] Инициализирован. Кэш: {self.cache_file}")
    
    def _load_cache(self) -> Dict:
        """Загружает и очищает кэш."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    
                    # Очищаем старые записи (старше 3 дней)
                    cleaned_cache = {}
                    now = datetime.now().timestamp()
                    three_days_ago = now - (3 * 24 * 3600)
                    
                    for key, data in cache_data.items():
                        if data.get('timestamp', 0) > three_days_ago:
                            cleaned_cache[key] = data
                    
                    # Сохраняем если очистили
                    if len(cleaned_cache) != len(cache_data):
                        self._save_cache(cleaned_cache)
                    
                    return cleaned_cache
        except Exception as e:
            logger.error(f"[SimpleYouTube] Ошибка загрузки кэша: {e}")
        
        return {}
    
    def _save_cache(self, cache_data: Dict = None):
        """Сохраняет кэш."""
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
            # Проверяем срок годности (максимум 7 дней)
            timestamp = cached.get('timestamp', 0)
            max_age = datetime.now().timestamp() - (7 * 24 * 3600)
            
            if timestamp > max_age:
                success_rate = cached.get('success_rate', 0)
                if success_rate > 0.5:  # Только если успешность > 50%
                    logger.info(f"[SimpleYouTube] Кэш найден для: {query}")
                    return cached
        
        return None
    
    def add_to_cache(self, query: str, source: Source, video_id: str, 
                    title: str, artist: str, duration: int, success: bool = True):
        """Добавляет или обновляет кэш."""
        cache_key = self._get_cache_key(query, source)
        
        if cache_key in self.cache:
            # Обновляем существующую запись
            old_data = self.cache[cache_key]
            old_success = old_data.get('success', False)
            # Простая логика обновления успешности
            new_success_rate = 0.7 if success else 0.3
        else:
            new_success_rate = 1.0 if success else 0.0
        
        self.cache[cache_key] = {
            'video_id': video_id,
            'title': title,
            'artist': artist,
            'duration': duration,
            'success': success,
            'success_rate': new_success_rate,
            'timestamp': datetime.now().timestamp(),
            'query': query,
            'source': source.value
        }
        
        # Ограничиваем размер кэша (максимум 50 записей)
        if len(self.cache) > 50:
            # Удаляем самые старые записи с низкой успешностью
            sorted_keys = sorted(
                self.cache.keys(),
                key=lambda k: (self.cache[k].get('success_rate', 0), 
                              self.cache[k].get('timestamp', 0))
            )
            
            for key in sorted_keys[:len(self.cache) - 50]:
                del self.cache[key]
        
        self._save_cache()
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек с использованием кэша."""
        async with download_lock:
            # Проверяем кэш
            cached = self.get_cached_data(query, source)
            
            if cached and cached.get('success_rate', 0) > 0.6:
                try:
                    logger.info(f"[SimpleYouTube] Использую кэш: {query}")
                    result = await self.youtube_downloader.download_track(
                        cached['video_id'], 
                        source
                    )
                    
                    if result:
                        # Увеличиваем рейтинг успешности
                        self.add_to_cache(query, source, cached['video_id'],
                                         cached['title'], cached['artist'],
                                         cached['duration'], success=True)
                        return result
                    else:
                        # Уменьшаем рейтинг
                        self.add_to_cache(query, source, cached['video_id'],
                                         cached['title'], cached['artist'],
                                         cached['duration'], success=False)
                except Exception as e:
                    logger.warning(f"[SimpleYouTube] Кэш не сработал: {e}")
            
            # Новый поиск
            logger.info(f"[SimpleYouTube] Новый поиск: '{query}'")
            try:
                result = await self.youtube_downloader.download_track(query, source)
                
                if result:
                    audio_path, track_info = result
                    video_id = getattr(self.youtube_downloader, 'last_video_id', None)
                    
                    if video_id and video_id != "unknown":
                        self.add_to_cache(query, source, video_id,
                                         track_info.title, track_info.artist,
                                         track_info.duration, success=True)
                
                return result
            except Exception as e:
                if "YouTube заблокировал запрос" in str(e):
                    raise  # Пробрасываем выше для обработки
                return None
    
    async def download_audiobook(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает аудиокнигу."""
        async with download_lock:
            logger.info(f"[SimpleYouTube] Поиск аудиокниги: '{query}'")
            
            # Пробуем специализированный поиск
            result = await self.youtube_downloader.download_audiobook(query, source)
            
            if not result:
                # Если не нашли, пробуем обычный поиск самого длинного
                logger.info(f"Специализированный поиск не дал результатов, пробую общий")
                if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                    search_query = f"ytsearch10:{query} аудиокнига"
                else:
                    search_query = f"{query} аудиокнига"
                
                result = await self.youtube_downloader.download_track(search_query, source)
            
            return result
    
    async def close(self):
        """Закрывает соединения."""
        await self.youtube_downloader.close()
        self._save_cache()
        logger.info("[SimpleYouTube] Загрузчик остановлен")