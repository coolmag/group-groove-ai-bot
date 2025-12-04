import os
import logging
import asyncio
import sqlite3
import hashlib
from typing import Optional, Tuple, Dict
from datetime import datetime, timedelta

from config import DOWNLOADS_DIR, TrackInfo, Source
from downloader import AudioDownloadManager
from locks import download_lock

logger = logging.getLogger(__name__)

class SmartYouTubeDownloader:
    """Умный YouTube загрузчик с кэшированием метаданных."""
    
    CACHE_DB = "metadata_cache.db"
    CACHE_DURATION_DAYS = 7
    MAX_CACHE_SIZE = 300
    
    def __init__(self):
        self.setup_database()
        self.youtube_downloader = AudioDownloadManager()
        logger.info("[SmartYouTube] Умный загрузчик инициализирован")
    
    def setup_database(self):
        """Создаёт базу данных для кэша метаданных."""
        conn = sqlite3.connect(self.CACHE_DB)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT,
                video_id TEXT,
                source TEXT,
                title TEXT,
                artist TEXT,
                duration INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                use_count INTEGER DEFAULT 1,
                success_rate REAL DEFAULT 1.0
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_query_hash ON metadata_cache(query_hash, source)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_last_used ON metadata_cache(last_used)')
        
        conn.commit()
        conn.close()
    
    def _get_query_hash(self, query: str) -> str:
        """Создаёт хеш запроса."""
        return hashlib.md5(query.lower().encode()).hexdigest()[:16]
    
    def get_cached_metadata(self, query: str, source: Source) -> Optional[Dict]:
        """Ищет метаданные в кэше."""
        query_hash = self._get_query_hash(query)
        
        conn = sqlite3.connect(self.CACHE_DB)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT video_id, title, artist, duration, success_rate
            FROM metadata_cache 
            WHERE query_hash = ? AND source = ? AND 
                  datetime(created_at) >= datetime('now', ?)
            ORDER BY success_rate DESC, use_count DESC
            LIMIT 1
        ''', (query_hash, source.value, f'-{self.CACHE_DURATION_DAYS} days'))
        
        result = cursor.fetchone()
        
        if result:
            video_id, title, artist, duration, success_rate = result
            # Обновляем статистику использования
            cursor.execute('''
                UPDATE metadata_cache 
                SET last_used = CURRENT_TIMESTAMP, use_count = use_count + 1 
                WHERE query_hash = ? AND video_id = ?
            ''', (query_hash, video_id))
            conn.commit()
            
            logger.info(f"[SmartYouTube] Кэш найден для: {query} ({source.value})")
            return {
                'video_id': video_id,
                'title': title,
                'artist': artist,
                'duration': duration,
                'success_rate': success_rate
            }
        
        conn.close()
        return None
    
    def add_to_cache(self, query: str, video_id: str, source: Source, 
                    title: str, artist: str, duration: int, success: bool = True):
        """Добавляет метаданные в кэш."""
        query_hash = self._get_query_hash(query)
        
        conn = sqlite3.connect(self.CACHE_DB)
        cursor = conn.cursor()
        
        try:
            # Проверяем существующую запись
            cursor.execute('''
                SELECT id, success_rate, use_count FROM metadata_cache 
                WHERE query_hash = ? AND video_id = ? AND source = ?
            ''', (query_hash, video_id, source.value))
            
            existing = cursor.fetchone()
            
            if existing:
                # Обновляем существующую
                cache_id, old_rate, use_count = existing
                new_rate = ((old_rate * use_count) + (1.0 if success else 0.0)) / (use_count + 1)
                
                cursor.execute('''
                    UPDATE metadata_cache 
                    SET last_used = CURRENT_TIMESTAMP,
                        use_count = use_count + 1,
                        success_rate = ?
                    WHERE id = ?
                ''', (new_rate, cache_id))
            else:
                # Добавляем новую
                cursor.execute('''
                    INSERT INTO metadata_cache 
                    (query_hash, video_id, source, title, artist, duration, success_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (query_hash, video_id, source.value, title, artist, duration, 
                      1.0 if success else 0.0))
            
            # Очистка старых записей
            cursor.execute('SELECT COUNT(*) FROM metadata_cache')
            count = cursor.fetchone()[0]
            
            if count > self.MAX_CACHE_SIZE:
                cursor.execute('''
                    DELETE FROM metadata_cache 
                    WHERE id IN (
                        SELECT id FROM metadata_cache 
                        ORDER BY last_used ASC, success_rate ASC 
                        LIMIT ?
                    )
                ''', (count - self.MAX_CACHE_SIZE,))
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"[SmartYouTube] Ошибка кэша: {e}")
        finally:
            conn.close()
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек с использованием кэша."""
        async with download_lock:
            # 1. Проверяем кэш метаданных
            cached = self.get_cached_metadata(query, source)
            
            if cached and cached['success_rate'] > 0.6:
                try:
                    logger.info(f"[SmartYouTube] Пробую кэшированный video_id: {cached['video_id']}")
                    result = await self.youtube_downloader.download_track(cached['video_id'], source)
                    
                    if result:
                        # Успешно - увеличиваем рейтинг
                        self.add_to_cache(query, cached['video_id'], source,
                                         cached['title'], cached['artist'], 
                                         cached['duration'], success=True)
                        return result
                    else:
                        # Не удалось - уменьшаем рейтинг
                        self.add_to_cache(query, cached['video_id'], source,
                                         cached['title'], cached['artist'],
                                         cached['duration'], success=False)
                except Exception as e:
                    logger.warning(f"[SmartYouTube] Кэш не сработал: {e}")
            
            # 2. Обычный поиск
            logger.info(f"[SmartYouTube] Новый поиск: '{query}'")
            result = await self.youtube_downloader.download_track(query, source)
            
            if result:
                audio_path, track_info = result
                video_id = self.youtube_downloader.last_video_id or "unknown"
                
                # Добавляем в кэш
                self.add_to_cache(query, video_id, source,
                                 track_info.title, track_info.artist,
                                 track_info.duration, success=True)
            
            return result
    
    async def download_longest_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек."""
        async with download_lock:
            # Для длинных треков тоже используем кэш
            cached = self.get_cached_metadata(f"long_{query}", source)
            
            if cached and cached['success_rate'] > 0.6:
                try:
                    result = await self.youtube_downloader.download_longest_track(cached['video_id'], source)
                    if result:
                        return result
                except:
                    pass
            
            result = await self.youtube_downloader.download_longest_track(query, source)
            
            if result:
                audio_path, track_info = result
                video_id = self.youtube_downloader.last_video_id or "unknown"
                self.add_to_cache(f"long_{query}", video_id, source,
                                 track_info.title, track_info.artist,
                                 track_info.duration, success=True)
            
            return result
    
    async def close(self):
        """Закрывает соединения."""
        await self.youtube_downloader.close()