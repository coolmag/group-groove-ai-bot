import json
import hashlib
import asyncio
import os
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

import aiosqlite

from base_downloader import DownloadResult
from config import settings, Source
from logger import logger


class CacheManager:
    """Менеджер кэша"""
    
    def __init__(self):
        self.db_path = "cache.db"
        self.init_lock = asyncio.Lock()
        self.initialized = False
    
    async def _init_db(self):
        """Инициализация БД"""
        async with self.init_lock:
            if not self.initialized:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS cache (
                            id TEXT PRIMARY KEY,
                            query TEXT,
                            source TEXT,
                            result_json TEXT,
                            last_access TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    await db.commit()
                self.initialized = True
    
    def _get_cache_id(self, query: str, source: Source) -> str:
        """Генерация ID кэша"""
        key = f"{source.value}:{query.lower().strip()}"
        return hashlib.md5(key.encode()).hexdigest()[:16]
    
    async def get(self, query: str, source: Source) -> Optional[DownloadResult]:
        """Получить из кэша"""
        await self._init_db()
        
        cache_id = self._get_cache_id(query, source)
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT result_json FROM cache WHERE id = ?",
                    (cache_id,)
                )
                row = await cursor.fetchone()
                
                if row:
                    result_data = json.loads(row['result_json'])
                    
                    # Проверяем срок годности (7 дней)
                    cursor = await db.execute(
                        "SELECT julianday('now') - julianday(last_access) as days FROM cache WHERE id = ?",
                        (cache_id,)
                    )
                    age_row = await cursor.fetchone()
                    
                    if age_row and age_row['days'] > 7:
                        await db.execute("DELETE FROM cache WHERE id = ?", (cache_id,))
                        await db.commit()
                        return None
                    
                    # Обновляем время доступа
                    await db.execute(
                        "UPDATE cache SET last_access = CURRENT_TIMESTAMP WHERE id = ?",
                        (cache_id,)
                    )
                    await db.commit()
                    
                    return DownloadResult(**result_data)
        
        except Exception as e:
            logger.warning(f"Ошибка кэша (get): {e}")
        
        return None
    
    async def set(self, query: str, source: Source, result: DownloadResult):
        """Сохранить в кэш"""
        if not result.success:
            return
        
        await self._init_db()
        
        cache_id = self._get_cache_id(query, source)
        result_json = json.dumps({
            'success': result.success,
            'file_path': result.file_path,
            'track_info': {
                'title': result.track_info.title,
                'artist': result.track_info.artist,
                'duration': result.track_info.duration,
                'source': result.track_info.source,
            } if result.track_info else None,
            'error': result.error
        })
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO cache (id, query, source, result_json) VALUES (?, ?, ?, ?)",
                    (cache_id, query, source.value, result_json)
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Ошибка кэша (set): {e}")