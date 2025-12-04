import os
import logging
import asyncio
import yt_dlp
import tempfile
from typing import Optional, Tuple
import re
from datetime import datetime

from config import DOWNLOADS_DIR, TrackInfo, Source, COOKIES_TEXT
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    """Улучшенный менеджер загрузки с обработкой ошибок."""
    
    def __init__(self):
        self.setup_directories()
        self.last_video_id = None
        self.cookies_file = None
        self.setup_cookies()
        
    def setup_directories(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"Директория загрузок: {DOWNLOADS_DIR}")
    
    def setup_cookies(self):
        """Настраивает cookies для YouTube."""
        if COOKIES_TEXT:
            try:
                # Создаем временный файл для cookies
                self.cookies_file = tempfile.NamedTemporaryFile(
                    mode='w', 
                    suffix='.txt', 
                    delete=False,
                    encoding='utf-8'
                )
                self.cookies_file.write(COOKIES_TEXT)
                self.cookies_file.flush()
                logger.info(f"Cookies созданы: {self.cookies_file.name}")
            except Exception as e:
                logger.error(f"Ошибка создания cookies: {e}")
                self.cookies_file = None
    
    def get_random_genre(self) -> str:
        import random
        genres = ["lofi hip hop", "chillhop", "synthwave", "jazz", "classical", 
                 "ambient", "electronic", "retro wave", "study music", "focus music"]
        return random.choice(genres)
    
    def _get_ydl_options(self, source: Source) -> dict:
        """Возвращает настройки yt-dlp."""
        base_options = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': False,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'retries': 2,
            'fragment_retries': 2,
            'skip_unavailable_fragments': True,
            'noplaylist': True,
        }
        
        # Добавляем cookies если есть
        if self.cookies_file:
            base_options['cookiefile'] = self.cookies_file.name
            logger.debug(f"Использую cookies: {self.cookies_file.name}")
        
        # Настройки для разных источников
        if source == Source.YOUTUBE_MUSIC:
            base_options['extractor_args'] = {
                'youtube': {
                    'player_client': ['web_music', 'android_music']
                }
            }
        
        return base_options
    
    def _is_youtube_blocked(self, error_msg: str) -> bool:
        """Проверяет, заблокировал ли YouTube запрос."""
        blocked_patterns = [
            "HTTP Error 429",
            "Too Many Requests",
            "blocked",
            "captcha",
            "Sign in to confirm"
        ]
        return any(pattern in str(error_msg) for pattern in blocked_patterns)
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек с улучшенной обработкой ошибок."""
        async with download_lock:
            logger.info(f"Скачивание: '{query}' с {source.value}")
            
            # Проверяем video_id
            is_video_id = re.match(r'^[a-zA-Z0-9_-]{11}$', query)
            
            # Формируем поисковый запрос
            if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                search_query = f"ytsearch1:{query}" if not is_video_id else query
            elif source == Source.SOUNDCLOUD:
                search_query = f"scsearch1:{query}" if not is_video_id else query
            elif source == Source.JAMENDO:
                search_query = f"jamendo1:{query}" if not is_video_id else query
            elif source == Source.ARCHIVE:
                search_query = f"archive1:{query}" if not is_video_id else query
            else:
                search_query = query
            
            ydl_opts = self._get_ydl_options(source)
            
            for attempt in range(2):  # Уменьшили попытки до 2
                try:
                    logger.info(f"Попытка {attempt + 1}/2 для {source.value}")
                    
                    # Используем run_in_executor для асинхронности
                    def download_task():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            return ydl.extract_info(search_query, download=True)
                    
                    info = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, download_task),
                        timeout=45  # Таймаут 45 секунд
                    )
                    
                    if not info:
                        logger.warning(f"Пустой ответ от {source.value}")
                        continue
                    
                    # Извлекаем информацию о треке
                    if 'entries' in info:
                        video_info = info['entries'][0]
                    else:
                        video_info = info
                    
                    if not video_info:
                        logger.warning(f"Нет видео информации от {source.value}")
                        continue
                    
                    self.last_video_id = video_info.get('id')
                    
                    # Формируем путь к файлу
                    audio_path = os.path.join(DOWNLOADS_DIR, f"{video_info['id']}.mp3")
                    
                    if not os.path.exists(audio_path):
                        # Ищем файл с любым расширением
                        import glob
                        pattern = os.path.join(DOWNLOADS_DIR, f"{video_info['id']}.*")
                        files = glob.glob(pattern)
                        if files:
                            audio_path = files[0]
                        else:
                            logger.error(f"Файл не найден: {video_info['id']}")
                            continue
                    
                    # Получаем метаданные
                    title = video_info.get('title', 'Unknown Track')[:100]
                    artist = 'Unknown Artist'
                    
                    # Извлекаем артиста
                    for field in ['artist', 'uploader', 'channel', 'creator']:
                        if video_info.get(field):
                            artist = video_info[field][:100]
                            break
                    
                    duration = int(video_info.get('duration', 0) or 180)
                    
                    track_info = TrackInfo(
                        title=title,
                        artist=artist,
                        duration=duration,
                        source=source.value
                    )
                    
                    logger.info(f"Успешно: {artist} - {title} ({duration}с)")
                    return (audio_path, track_info)
                    
                except asyncio.TimeoutError:
                    logger.error(f"Таймаут {source.value} (попытка {attempt + 1})")
                    if attempt == 0:
                        await asyncio.sleep(2)
                    continue
                    
                except Exception as e:
                    logger.error(f"Ошибка {source.value}: {str(e)[:200]}")
                    
                    # Проверяем блокировку YouTube
                    if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC] and self._is_youtube_blocked(str(e)):
                        logger.error("⚠️ YouTube заблокировал запрос! Нужны cookies.")
                        raise Exception("YouTube заблокировал запрос")
                    
                    if attempt == 0:
                        await asyncio.sleep(1)
                    continue
            
            logger.error(f"Не удалось скачать с {source.value}: {query}")
            return None
    
    async def download_audiobook(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Специализированный поиск аудиокниг."""
        async with download_lock:
            logger.info(f"Поиск аудиокниги: '{query}'")
            
            # Для аудиокниг используем специальный поиск
            search_query = f"{query} аудиокнига полная версия"
            
            if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                search_query = f"ytsearch5:{search_query}"
            elif source == Source.SOUNDCLOUD:
                search_query = f"scsearch5:{search_query}"
            else:
                search_query = f"{search_query}"
            
            ydl_opts = self._get_ydl_options(source)
            
            try:
                def search_task():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(search_query, download=False)
                
                info = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, search_task),
                    timeout=60
                )
                
                if not info or 'entries' not in info:
                    return None
                
                # Ищем самый длинный трек (предположительно аудиокнига)
                entries = [e for e in info['entries'] if e]
                if not entries:
                    return None
                
                # Ищем треки с длительностью более 30 минут
                long_tracks = [e for e in entries if e.get('duration', 0) > 1800]
                if long_tracks:
                    longest_track = max(long_tracks, key=lambda x: x.get('duration', 0))
                else:
                    longest_track = max(entries, key=lambda x: x.get('duration', 0))
                
                # Скачиваем найденный трек
                video_id = longest_track['id']
                self.last_video_id = video_id
                
                def download_audio():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([video_id])
                
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, download_audio),
                    timeout=120
                )
                
                # Проверяем файл
                audio_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp3")
                if not os.path.exists(audio_path):
                    import glob
                    pattern = os.path.join(DOWNLOADS_DIR, f"{video_id}.*")
                    files = glob.glob(pattern)
                    if not files:
                        return None
                    audio_path = files[0]
                
                title = longest_track.get('title', 'Unknown Audiobook')[:100]
                artist = longest_track.get('uploader', 'Unknown Author')[:100]
                duration = longest_track.get('duration', 0)
                
                track_info = TrackInfo(
                    title=title,
                    artist=artist,
                    duration=duration,
                    source=f"Audiobook {source.value}"
                )
                
                return (audio_path, track_info)
                
            except Exception as e:
                logger.error(f"Ошибка поиска аудиокниги: {e}")
                return None
    
    async def close(self):
        """Очистка ресурсов."""
        if self.cookies_file:
            try:
                os.unlink(self.cookies_file.name)
                logger.info("Cookies файл удален")
            except:
                pass
        logger.info("Загрузчик остановлен")