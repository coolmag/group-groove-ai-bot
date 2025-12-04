import os
import logging
import asyncio
import random
import uuid
import time
from typing import Optional, Tuple, List
import yt_dlp

from config import (
    DOWNLOADS_DIR, GENRES, Source, TrackInfo, 
    PROXY_URL, PROXY_ENABLED, TEMP_COOKIE_PATH, 
    YOUTUBE_COOKIES_PATH, SOUNDCLOUD_COOKIES_PATH,
    DOWNLOAD_TIMEOUT, MAX_AUDIO_SIZE_MB
)
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        # ... существующий код ...
        self.last_video_id = None  # Добавьте эту строку
    
    async def download_track(self, query: str, source: str) -> Optional[Tuple[str, TrackInfo]]:
        # ... существующий код в методе ...
        
        try:
            # После успешного скачивания сохраняем video_id
            if 'info_dict' in ydl_result:
                self.last_video_id = ydl_result['info_dict'].get('id')
            elif ydl_result.get('url'):
                # Пробуем извлечь из URL
                import re
                match = re.search(r'v=([a-zA-Z0-9_-]+)', ydl_result['url'])
                if match:
                    self.last_video_id = match.group(1)
        
    def setup_directories(self):
        """Создает необходимые директории"""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"Директория загрузок: {DOWNLOADS_DIR}")
    
    
    def get_random_genre(self) -> str:
        """Возвращает случайный жанр"""
        return random.choice(GENRES)
    
    def _get_ydl_base_opts(self) -> dict:
        """Возвращает базовые опции для yt-dlp"""
        opts = {
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': DOWNLOAD_TIMEOUT,
            'retries': 3,
            'extract_flat': 'in_playlist',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'format': 'bestaudio/best',
            'postprocessors': [],
            'nocheckcertificate': True,
            'ignoreerrors': True,
        }
        
        # Добавляем cookies если есть
        if TEMP_COOKIE_PATH and os.path.exists(TEMP_COOKIE_PATH):
            opts['cookiefile'] = TEMP_COOKIE_PATH
        elif YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts['cookiefile'] = YOUTUBE_COOKIES_PATH
        
        # Добавляем прокси если включен
        if PROXY_ENABLED and PROXY_URL:
            opts['proxy'] = PROXY_URL
            logger.debug(f"Использую прокси: {PROXY_URL}")
        
        return opts
    
    def _get_source_specific_opts(self, source: Source) -> dict:
        """Возвращает опции, специфичные для источника"""
        opts = {}
        
        # Настройки cookies для SoundCloud
        if source == Source.SOUNDCLOUD and SOUNDCLOUD_COOKIES_PATH and os.path.exists(SOUNDCLOUD_COOKIES_PATH):
            opts['cookiefile'] = SOUNDCLOUD_COOKIES_PATH
        
        return opts
    
    async def _get_search_results(self, query: str, source: Source, count: int = 5) -> List[str]:
        """Ищет видео и возвращает список URL"""
        source_map = {
            Source.YOUTUBE: "ytsearch",
            Source.YOUTUBE_MUSIC: "ytmsearch",
            Source.SOUNDCLOUD: "scsearch",
            Source.JAMENDO: "jamendosearch",
            Source.ARCHIVE: "iasearch",
            Source.DEEZER: "dzsearch"  # Добавлен Deezer
        }
        
        search_prefix = source_map.get(source)
        if not search_prefix:
            logger.error(f"Неподдерживаемый источник: {source}")
            return []
        
        # Проверяем кэш
        cache_key = f"{source.value}:{query}"
        if cache_key in self._cache:
            logger.debug(f"Использую кэшированные результаты для: {query}")
            return self._cache[cache_key]
        
        search_query = f"{search_prefix}{count}:{query}"
        logger.info(f"Поиск: '{search_query}'")
        
        ydl_opts = self._get_ydl_base_opts()
        source_opts = self._get_source_specific_opts(source)
        ydl_opts.update(source_opts)
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(
                    ydl.extract_info, 
                    search_query, 
                    download=False
                )
                
                if not info or not info.get('entries'):
                    logger.warning(f"Нет результатов для: {query}")
                    return []
                
                urls = []
                for entry in info['entries']:
                    if entry and 'url' in entry:
                        urls.append(entry['url'])
                
                # Кэшируем результаты
                self._cache[cache_key] = urls
                return urls
                
        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            return []
    
    async def _get_video_info(self, video_url: str) -> Optional[dict]:
        """Получает информацию о видео"""
        ydl_opts = self._get_ydl_base_opts()
        ydl_opts.update({'skip_download': True})
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(
                    ydl.extract_info, 
                    video_url, 
                    download=False
                )
                return info
        except Exception as e:
            logger.warning(f"Не удалось получить информацию: {video_url} - {e}")
            return None
    
    async def _check_file_size(self, filepath: str) -> bool:
        """Проверяет размер файла"""
        try:
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if size_mb > MAX_AUDIO_SIZE_MB:
                logger.warning(f"Файл слишком большой: {size_mb:.2f} МБ")
                return False
            return True
        except Exception as e:
            logger.error(f"Ошибка проверки размера файла: {e}")
            return False
    
    async def _execute_single_download(self, video_url: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает одно видео"""
        unique_id = str(uuid.uuid4())[:8]
        
        ydl_opts = self._get_ydl_base_opts()
        ydl_opts.update({
            'extract_flat': False,
            'outtmpl': os.path.join(DOWNLOADS_DIR, f'{unique_id}.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'progress_hooks': [self._progress_hook],
        })
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(
                    ydl.extract_info, 
                    video_url, 
                    download=True
                )
                
                if not info:
                    return None
                
                # Получаем путь к файлу
                base_filename = ydl.prepare_filename(info)
                if '.' in base_filename:
                    final_filepath = base_filename.rsplit('.', 1)[0] + '.mp3'
                else:
                    final_filepath = base_filename + '.mp3'
                
                # Дополнительные проверки
                final_filepath = final_filepath.replace('.webm', '.mp3').replace('.m4a', '.mp3')
                
                if not os.path.exists(final_filepath):
                    # Пытаемся найти файл по уникальному ID
                    for file in os.listdir(DOWNLOADS_DIR):
                        if file.startswith(unique_id):
                            final_filepath = os.path.join(DOWNLOADS_DIR, file)
                            break
                
                if not os.path.exists(final_filepath):
                    logger.error(f"Файл не найден: {final_filepath}")
                    return None
                
                # Проверяем размер файла
                if not await self._check_file_size(final_filepath):
                    try:
                        os.remove(final_filepath)
                    except:
                        pass
                    return None
                
                # Создаем информацию о треке
                title = info.get('title', 'Unknown')
                if len(title) > 100:
                    title = title[:97] + '...'
                
                artist = info.get('uploader', 'Unknown Artist')
                if len(artist) > 100:
                    artist = artist[:97] + '...'
                
                track_info = TrackInfo(
                    title=title,
                    artist=artist,
                    duration=int(info.get('duration', 0)),
                    source=info.get('extractor_key', 'Unknown')
                )
                
                logger.info(f"Скачан трек: {track_info.title}")
                return (final_filepath, track_info)
                
        except Exception as e:
            logger.warning(f"Не удалось скачать {video_url}: {e}")
            # Очищаем возможные частично скачанные файлы
            for file in os.listdir(DOWNLOADS_DIR):
                if unique_id in file:
                    try:
                        os.remove(os.path.join(DOWNLOADS_DIR, file))
                    except:
                        pass
            return None
    
    def _progress_hook(self, d):
        """Хук для отслеживания прогресса загрузки"""
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '?').strip()
            speed = d.get('_speed_str', '?').strip()
            if percent != '?' and speed != '?':
                logger.debug(f"Загрузка: {percent}, скорость: {speed}")
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек по запросу"""
        async with download_lock:
            logger.info(f"Скачивание трека: '{query}' с {source.value}")
            
            candidate_urls = await self._get_search_results(query, source, count=3)
            
            if not candidate_urls:
                return None
            
            for i, url in enumerate(candidate_urls):
                logger.info(f"Попытка {i+1}/{len(candidate_urls)}")
                result = await self._execute_single_download(url)
                if result:
                    return result
            
            logger.error(f"Не удалось скачать трек: '{query}'")
            return None
    
    async def download_longest_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек по запросу"""
        async with download_lock:
            logger.info(f"Поиск аудиокниги: '{query}'")
            
            candidate_urls = await self._get_search_results(query, source, count=5)
            
            if not candidate_urls:
                return None
            
            # Находим самое длинное видео
            longest_urls = []
            max_duration = 0
            
            for url in candidate_urls:
                info = await self._get_video_info(url)
                if info:
                    duration = info.get('duration', 0)
                    if duration > max_duration:
                        max_duration = duration
                        longest_urls = [url]
                    elif duration == max_duration:
                        longest_urls.append(url)
            
            if not longest_urls:
                logger.warning(f"Не найдено подходящих видео для: '{query}'")
                return None
            
            # Берем первый из самых длинных
            longest_url = random.choice(longest_urls) if len(longest_urls) > 1 else longest_urls[0]
            logger.info(f"Выбрано видео длительностью: {max_duration}с")
            return await self._execute_single_download(longest_url)
    
    async def close(self):
        """Очистка ресурсов"""
        logger.info("Очистка ресурсов загрузчика...")
        
        # Очищаем кэш
        self._cache.clear()
        
        # Очищаем старые файлы (старше 1 часа)
        try:
            current_time = time.time()
            for filename in os.listdir(DOWNLOADS_DIR):
                filepath = os.path.join(DOWNLOADS_DIR, filename)
                try:
                    if os.path.isfile(filepath):
                        file_age = current_time - os.path.getmtime(filepath)
                        if file_age > 3600:  # 1 час
                            os.remove(filepath)
                            logger.debug(f"Удален старый файл: {filename}")
                except Exception as e:
                    logger.debug(f"Не удалось удалить файл {filename}: {e}")
        except Exception as e:
            logger.error(f"Ошибка при очистке файлов: {e}")
