import os
import logging
import asyncio
import yt_dlp
from typing import Optional, Tuple, List
from datetime import datetime

from config import DOWNLOADS_DIR, TrackInfo, Source
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    """Менеджер загрузки аудио с YouTube и других источников."""
    
    def __init__(self):
        self.setup_directories()
        self.last_video_id = None  # ← ДОБАВЛЕНО ДЛЯ КЭШИРОВАНИЯ
        
    def setup_directories(self):
        """Создаёт директорию для загрузок."""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"Директория загрузок: {DOWNLOADS_DIR}")
    
    def get_random_genre(self) -> str:
        """Возвращает случайный жанр для радио."""
        import random
        genres = ["lofi hip hop", "chillhop", "synthwave", "jazz", "classical", 
                 "ambient", "electronic", "retro wave", "study music", "focus music"]
        return random.choice(genres)
    
    def _get_ydl_options(self, source: Source) -> dict:
        """Возвращает настройки yt-dlp для разных источников."""
        base_options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            'skip_unavailable_fragments': True,
        }
        
        if source == Source.YOUTUBE_MUSIC:
            base_options['extractor_args'] = {'youtube': {'player_client': ['web_music']}}
        elif source == Source.SOUNDCLOUD:
            base_options['extractor_args'] = {'soundcloud': {'client_id': 'your_client_id'}}
        
        return base_options
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек по запросу."""
        async with download_lock:
            logger.info(f"Скачивание трека: '{query}' с {source.value}")
            
            # Если запрос похож на video_id (11 символов, буквы/цифры/_-)
            import re
            if re.match(r'^[a-zA-Z0-9_-]{11}$', query):
                video_id = query
                search_query = video_id
                logger.info(f"Использую video_id: {video_id}")
            else:
                video_id = None
                # Формируем поисковый запрос в зависимости от источника
                if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                    search_query = f"ytsearch1:{query}"
                elif source == Source.SOUNDCLOUD:
                    search_query = f"scsearch1:{query}"
                elif source == Source.JAMENDO:
                    search_query = f"jamendo1:{query}"
                elif source == Source.ARCHIVE:
                    search_query = f"archive1:{query}"
                else:
                    search_query = query
            
            ydl_opts = self._get_ydl_options(source)
            ydl_opts['outtmpl'] = os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s')
            
            loop = asyncio.get_event_loop()
            
            for attempt in range(3):
                try:
                    logger.info(f"Попытка {attempt + 1}/3")
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        # Используем run_in_executor для избежания блокировки
                        info = await loop.run_in_executor(
                            None, 
                            lambda: ydl.extract_info(search_query, download=True)
                        )
                        
                        if not info:
                            logger.warning(f"Не найдено результатов для: {query}")
                            continue
                        
                        # Получаем первый результат
                        if 'entries' in info:
                            video_info = info['entries'][0]
                        else:
                            video_info = info
                        
                        # Сохраняем video_id для кэширования ← ВАЖНО!
                        self.last_video_id = video_info.get('id')
                        
                        # Формируем путь к файлу
                        audio_path = os.path.join(
                            DOWNLOADS_DIR, 
                            f"{video_info['id']}.mp3"
                        )
                        
                        if not os.path.exists(audio_path):
                            logger.error(f"Файл не создан: {audio_path}")
                            continue
                        
                        # Получаем информацию о треке
                        title = video_info.get('title', 'Unknown')[:100]
                        artist = 'Unknown'
                        
                        # Пытаемся извлечь артиста
                        if video_info.get('artist'):
                            artist = video_info['artist'][:100]
                        elif video_info.get('uploader'):
                            artist = video_info['uploader'][:100]
                        elif 'channel' in video_info:
                            artist = video_info['channel'][:100]
                        
                        duration = int(video_info.get('duration', 0))
                        
                        track_info = TrackInfo(
                            title=title,
                            artist=artist,
                            duration=duration,
                            source=source.value
                        )
                        
                        logger.info(f"Успешно скачан: {artist} - {title}")
                        return (audio_path, track_info)
                        
                except Exception as e:
                    logger.error(f"Ошибка при скачивании (попытка {attempt + 1}): {e}")
                    if attempt < 2:
                        await asyncio.sleep(1)
            
            logger.error(f"Не удалось скачать трек после 3 попыток: {query}")
            return None
    
    async def download_longest_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает самый длинный трек по запросу (для аудиокниг)."""
        async with download_lock:
            logger.info(f"Поиск длинного трека: '{query}'")
            
            # Увеличиваем лимит поиска для аудиокниг
            if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                search_query = f"ytsearch10:{query}"
            elif source == Source.SOUNDCLOUD:
                search_query = f"scsearch10:{query}"
            else:
                search_query = query
            
            ydl_opts = self._get_ydl_options(source)
            ydl_opts['outtmpl'] = os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s')
            
            loop = asyncio.get_event_loop()
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = await loop.run_in_executor(
                        None, 
                        lambda: ydl.extract_info(search_query, download=False)
                    )
                    
                    if not info or 'entries' not in info:
                        return None
                    
                    # Ищем самый длинный трек
                    entries = info['entries']
                    longest_track = None
                    max_duration = 0
                    
                    for entry in entries:
                        if entry and entry.get('duration', 0) > max_duration:
                            max_duration = entry['duration']
                            longest_track = entry
                    
                    if not longest_track:
                        return None
                    
                    # Скачиваем самый длинный трек
                    video_id = longest_track['id']
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl_download:
                        await loop.run_in_executor(
                            None,
                            lambda: ydl_download.download([video_id])
                        )
                    
                    audio_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp3")
                    
                    if not os.path.exists(audio_path):
                        return None
                    
                    # Сохраняем video_id ← ВАЖНО!
                    self.last_video_id = video_id
                    
                    title = longest_track.get('title', 'Unknown')[:100]
                    artist = longest_track.get('uploader', 'Unknown')[:100]
                    duration = max_duration
                    
                    track_info = TrackInfo(
                        title=title,
                        artist=artist,
                        duration=duration,
                        source=f"Long {source.value}"
                    )
                    
                    return (audio_path, track_info)
                    
            except Exception as e:
                logger.error(f"Ошибка при поиске длинного трека: {e}")
                return None
    
    async def close(self):
        """Очистка временных файлов."""
        try:
            # Удаляем старые файлы (старше 1 часа)
            for filename in os.listdir(DOWNLOADS_DIR):
                filepath = os.path.join(DOWNLOADS_DIR, filename)
                if os.path.isfile(filepath):
                    file_age = datetime.now().timestamp() - os.path.getmtime(filepath)
                    if file_age > 3600:  # 1 час
                        os.remove(filepath)
                        logger.debug(f"Удален старый файл: {filename}")
        except Exception as e:
            logger.error(f"Ошибка при очистке файлов: {e}")
