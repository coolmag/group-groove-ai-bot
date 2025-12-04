import os
import logging
import asyncio
import yt_dlp
from typing import Optional, Tuple
import re

from config import DOWNLOADS_DIR, TrackInfo, Source
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    """Менеджер загрузки аудио с YouTube и других источников."""
    
    def __init__(self):
        self.setup_directories()
        self.last_video_id = None
        
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
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            'skip_unavailable_fragments': True,
        }
        
        # Добавляем cookies если есть
        cookies_text = os.getenv("COOKIES_TEXT", "")
        if cookies_text:
            import tempfile
            import atexit
            
            # Создаем временный файл для cookies
            cookies_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            cookies_file.write(cookies_text)
            cookies_file.flush()
            cookies_file.close()
            
            base_options['cookiefile'] = cookies_file.name
            
            # Удаляем файл при выходе
            def cleanup():
                try:
                    os.unlink(cookies_file.name)
                except:
                    pass
            
            atexit.register(cleanup)
        
        if source == Source.YOUTUBE_MUSIC:
            base_options['extractor_args'] = {'youtube': {'player_client': ['web_music']}}
        
        return base_options
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек по запросу."""
        async with download_lock:
            logger.info(f"Скачивание трека: '{query}' с {source.value}")
            
            # Проверяем, является ли запрос video_id (11 символов YouTube ID)
            if re.match(r'^[a-zA-Z0-9_-]{11}$', query):
                video_id = query
                search_query = video_id
                logger.info(f"Использую video_id: {video_id}")
            else:
                video_id = None
                # Формируем поисковый запрос
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
            
            loop = asyncio.get_event_loop()
            
            for attempt in range(3):
                try:
                    logger.info(f"Попытка {attempt + 1}/3")
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
                        
                        # Сохраняем video_id для кэширования
                        self.last_video_id = video_info.get('id')
                        
                        # Формируем путь к файлу
                        audio_path = os.path.join(DOWNLOADS_DIR, f"{video_info['id']}.mp3")
                        
                        if not os.path.exists(audio_path):
                            logger.error(f"Файл не создан: {audio_path}")
                            # Пробуем найти файл по шаблону
                            import glob
                            pattern = os.path.join(DOWNLOADS_DIR, f"{video_info['id']}.*")
                            files = glob.glob(pattern)
                            if files:
                                audio_path = files[0]
                            else:
                                continue
                        
                        # Получаем информацию о треке
                        title = video_info.get('title', 'Unknown Track')[:100]
                        artist = 'Unknown Artist'
                        
                        # Пытаемся извлечь артиста
                        if video_info.get('artist'):
                            artist = video_info['artist'][:100]
                        elif video_info.get('uploader'):
                            artist = video_info['uploader'][:100]
                        elif video_info.get('channel'):
                            artist = video_info['channel'][:100]
                        
                        duration = int(video_info.get('duration', 0))
                        if duration == 0:
                            duration = 180  # Дефолтное значение
                        
                        track_info = TrackInfo(
                            title=title,
                            artist=artist,
                            duration=duration,
                            source=source.value
                        )
                        
                        logger.info(f"Успешно скачан: {artist} - {title} ({duration} сек)")
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
            
            if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                search_query = f"ytsearch10:{query}"
            elif source == Source.SOUNDCLOUD:
                search_query = f"scsearch10:{query}"
            else:
                search_query = query
            
            ydl_opts = self._get_ydl_options(source)
            
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
                    entries = [e for e in info['entries'] if e]
                    if not entries:
                        return None
                    
                    longest_track = max(entries, key=lambda x: x.get('duration', 0))
                    
                    # Скачиваем самый длинный трек
                    video_id = longest_track['id']
                    self.last_video_id = video_id
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl_download:
                        await loop.run_in_executor(
                            None,
                            lambda: ydl_download.download([video_id])
                        )
                    
                    audio_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp3")
                    
                    if not os.path.exists(audio_path):
                        # Пробуем найти файл
                        import glob
                        pattern = os.path.join(DOWNLOADS_DIR, f"{video_id}.*")
                        files = glob.glob(pattern)
                        if not files:
                            return None
                        audio_path = files[0]
                    
                    title = longest_track.get('title', 'Unknown Track')[:100]
                    artist = longest_track.get('uploader', 'Unknown Artist')[:100]
                    duration = longest_track.get('duration', 0)
                    
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
        """Очистка ресурсов."""
        logger.info("Загрузчик остановлен")
