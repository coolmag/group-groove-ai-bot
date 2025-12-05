import os
import tempfile
import atexit
import re
import asyncio
from typing import Optional, Dict, Any

import yt_dlp

from base_downloader import BaseDownloader, DownloadResult
from config import TrackInfo, settings, Source
from logger import logger
from cache import CacheManager


class YouTubeDownloader(BaseDownloader):
    """Загрузчик YouTube"""
    
    def __init__(self):
        super().__init__()
        self.cache = CacheManager()
        self.cookies_file = None
        self._setup_cookies()
    
    def _setup_cookies(self):
        """Настройка cookies"""
        if not settings.COOKIES_TEXT:
            logger.warning("⚠️ COOKIES_TEXT не задан")
            return
        
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', delete=False, encoding='utf-8'
            ) as f:
                f.write(settings.COOKIES_TEXT)
                self.cookies_file = f.name
            
            atexit.register(self._cleanup_cookies)
            logger.info(f"Cookies файл создан: {self.cookies_file}")
        except Exception as e:
            logger.error(f"Ошибка cookies: {e}")
            self.cookies_file = None
    
    def _cleanup_cookies(self):
        """Очистка cookies"""
        if self.cookies_file and os.path.exists(self.cookies_file):
            try:
                os.unlink(self.cookies_file)
            except:
                pass
    
    def _get_ydl_options(self) -> Dict[str, Any]:
        """Настройки yt-dlp"""
        options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(settings.DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': False,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'retries': 3,
            'noplaylist': True,
        }
        
        if self.cookies_file:
            options['cookiefile'] = self.cookies_file
        
        return options
    
    async def download(self, query: str) -> DownloadResult:
        """Загрузка с YouTube"""
        # Проверяем кэш
        cached = await self.cache.get(query, Source.YOUTUBE)
        if cached:
            logger.info(f"Использую кэш для: {query}")
            return cached
        
        logger.info(f"Скачиваю с YouTube: '{query}'")
        
        try:
            options = self._get_ydl_options()
            
            # Проверяем video_id
            video_id = None
            if re.match(r'^[a-zA-Z0-9_-]{11}$', query):
                video_id = query
                search_query = video_id
            else:
                search_query = f"ytsearch1:{query}"
            
            def _download():
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(search_query, download=True)
                    
                    if 'entries' in info:
                        video = info['entries'][0]
                    else:
                        video = info
                    
                    return video
            
            video_info = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _download),
                timeout=settings.DOWNLOAD_TIMEOUT
            )
            
            video_id = video_info.get('id', video_id)
            if not video_id:
                return DownloadResult(success=False, error="Нет video_id")
            
            # Ищем файл
            expected_path = os.path.join(settings.DOWNLOADS_DIR, f"{video_id}.mp3")
            if not os.path.exists(expected_path):
                import glob
                pattern = os.path.join(settings.DOWNLOADS_DIR, f"{video_id}.*")
                files = glob.glob(pattern)
                if files:
                    expected_path = files[0]
                else:
                    return DownloadResult(success=False, error="Файл не создан")
            
            # Информация о треке
            title = video_info.get('title', 'Unknown')[:100]
            artist = 'Unknown'
            
            for field in ['artist', 'uploader', 'channel']:
                if video_info.get(field):
                    artist = str(video_info[field])[:100]
                    break
            
            duration = int(video_info.get('duration', 180))
            
            track_info = TrackInfo(
                title=title,
                artist=artist,
                duration=duration,
                source=Source.YOUTUBE.value
            )
            
            result = DownloadResult(
                success=True,
                file_path=expected_path,
                track_info=track_info
            )
            
            # Сохраняем в кэш
            await self.cache.set(query, Source.YOUTUBE, result)
            
            return result
            
        except asyncio.TimeoutError:
            return DownloadResult(success=False, error="Таймаут загрузки")
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)[:200]
            if any(pattern in error_msg for pattern in 
                   ["429", "Too Many Requests", "blocked", "captcha"]):
                return DownloadResult(
                    success=False,
                    error="YouTube заблокировал запрос. Проверьте cookies."
                )
            return DownloadResult(success=False, error=error_msg)
        except Exception as e:
            logger.error(f"Ошибка YouTube: {e}")
            return DownloadResult(success=False, error=str(e))
    
    async def download_long(self, query: str) -> DownloadResult:
        """Поиск длинного контента (аудиокниг)"""
        logger.info(f"Поиск длинного контента: '{query}'")
        
        try:
            options = self._get_ydl_options()
            options['extract_flat'] = True
            
            def _search():
                with yt_dlp.YoutubeDL(options) as ydl:
                    return ydl.extract_info(f"ytsearch10:{query}", download=False)
            
            info = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _search),
                timeout=30
            )
            
            if not info or 'entries' not in info:
                return DownloadResult(success=False, error="Нет результатов")
            
            # Ищем самый длинный трек
            entries = [e for e in info['entries'] if e]
            if not entries:
                return DownloadResult(success=False, error="Нет записей")
            
            long_entries = [e for e in entries if e.get('duration', 0) > 1800]
            if long_entries:
                chosen = max(long_entries, key=lambda x: x.get('duration', 0))
            else:
                chosen = max(entries, key=lambda x: x.get('duration', 0))
            
            # Скачиваем выбранный
            video_id = chosen['id']
            return await self.download(video_id)
            
        except Exception as e:
            logger.error(f"Ошибка поиска длинного: {e}")
            return DownloadResult(success=False, error=str(e))