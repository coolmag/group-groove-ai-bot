import os
import logging
import asyncio
import yt_dlp
import tempfile
import atexit
from typing import Optional, Tuple
import re
import sys

from config import DOWNLOADS_DIR, TrackInfo, Source, COOKIES_TEXT
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    """Улучшенный менеджер загрузки с поддержкой cookies."""
    
    def __init__(self):
        self.setup_directories()
        self.last_video_id = None
        self.cookies_file_path = None
        self.cookies_setup()
        
    def setup_directories(self):
        """Создает директорию для загрузок."""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"Директория загрузок: {DOWNLOADS_DIR}")
    
    def cookies_setup(self):
        """Настраивает cookies файл для YouTube."""
        if not COOKIES_TEXT:
            logger.warning("COOKIES_TEXT пуст! YouTube будет блокировать запросы.")
            return
        
        try:
            # Создаем временный файл для cookies
            cookies_file = tempfile.NamedTemporaryFile(
                mode='w', 
                suffix='.txt', 
                delete=False,
                encoding='utf-8'
            )
            cookies_file.write(COOKIES_TEXT)
            cookies_file.flush()
            cookies_file.close()
            
            self.cookies_file_path = cookies_file.name
            logger.info(f"Cookies файл создан: {self.cookies_file_path}")
            
            # Регистрируем удаление файла при выходе
            atexit.register(self.cleanup_cookies)
            
        except Exception as e:
            logger.error(f"Ошибка создания cookies файла: {e}")
            self.cookies_file_path = None
    
    def cleanup_cookies(self):
        """Удаляет временный cookies файл."""
        if self.cookies_file_path and os.path.exists(self.cookies_file_path):
            try:
                os.unlink(self.cookies_file_path)
                logger.debug(f"Cookies файл удален: {self.cookies_file_path}")
            except Exception as e:
                logger.error(f"Ошибка удаления cookies файла: {e}")
    
    def get_random_genre(self) -> str:
        """Возвращает случайный жанр для радио."""
        import random
        genres = [
            "lofi hip hop", "chillhop", "synthwave", "jazz", "classical",
            "ambient", "electronic", "retro wave", "study music", "focus music",
            "pop", "rock", "indie", "reggae", "blues"
        ]
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
            'no_warnings': False,
            'ignoreerrors': True,
            'socket_timeout': 30,
            'retries': 2,
            'fragment_retries': 2,
            'skip_unavailable_fragments': True,
            'noplaylist': True,
            'verbose': False,
        }
        
        # Добавляем cookies если есть
        if self.cookies_file_path and os.path.exists(self.cookies_file_path):
            base_options['cookiefile'] = self.cookies_file_path
            logger.debug(f"Использую cookies файл: {self.cookies_file_path}")
        else:
            logger.warning("Cookies файл не найден! YouTube запросы могут быть заблокированы.")
        
        # Специальные настройки для разных источников
        if source == Source.YOUTUBE_MUSIC:
            base_options['extractor_args'] = {
                'youtube': {
                    'player_client': ['web_music', 'android_music'],
                    'player_skip': ['webpage', 'configs'],
                }
            }
            base_options['cookiesfrombrowser'] = ('chrome',)
        
        elif source == Source.YOUTUBE:
            base_options['extractor_args'] = {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'player_skip': ['webpage', 'configs'],
                }
            }
        
        return base_options
    
    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивает трек по запросу с улучшенной обработкой ошибок."""
        async with download_lock:
            logger.info(f"Запрос: '{query}' с {source.value}")
            
            # Проверяем, является ли запрос video_id
            is_video_id = False
            video_id = None
            
            if re.match(r'^[a-zA-Z0-9_-]{11}$', query):
                video_id = query
                is_video_id = True
                logger.info(f"Использую video_id: {video_id}")
            
            # Формируем поисковый запрос в зависимости от источника
            if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                if is_video_id:
                    search_query = video_id
                else:
                    search_query = f"ytsearch1:{query}"
            elif source == Source.SOUNDCLOUD:
                search_query = f"scsearch1:{query}" if not is_video_id else query
            elif source == Source.JAMENDO:
                search_query = f"jamendo1:{query}" if not is_video_id else query
            elif source == Source.ARCHIVE:
                search_query = f"archive1:{query}" if not is_video_id else query
            else:
                search_query = query
            
            ydl_opts = self._get_ydl_options(source)
            
            for attempt in range(2):  # Уменьшили до 2 попыток
                try:
                    logger.info(f"Попытка {attempt + 1}/2 для {source.value}")
                    
                    # Используем run_in_executor для асинхронного выполнения
                    def download_task():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            return ydl.extract_info(search_query, download=True)
                    
                    # Таймаут для скачивания
                    info = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, download_task),
                        timeout=45  # 45 секунд таймаут
                    )
                    
                    if not info:
                        logger.warning(f"Пустой ответ от {source.value}")
                        continue
                    
                    # Извлекаем информацию о видео
                    if 'entries' in info:
                        video_info = info['entries'][0]
                        if not video_info:
                            logger.warning(f"Нет видео в записях от {source.value}")
                            continue
                    else:
                        video_info = info
                    
                    self.last_video_id = video_info.get('id')
                    
                    # Проверяем, создался ли файл
                    expected_file = os.path.join(DOWNLOADS_DIR, f"{self.last_video_id}.mp3")
                    
                    if not os.path.exists(expected_file):
                        # Ищем файл с любым расширением
                        import glob
                        pattern = os.path.join(DOWNLOADS_DIR, f"{self.last_video_id}.*")
                        files = glob.glob(pattern)
                        if not files:
                            logger.error(f"Файл не создан для video_id: {self.last_video_id}")
                            continue
                        expected_file = files[0]
                    
                    # Получаем метаданные
                    title = video_info.get('title', 'Unknown Track')
                    if len(title) > 100:
                        title = title[:97] + "..."
                    
                    # Ищем артиста
                    artist = 'Unknown Artist'
                    for field in ['artist', 'uploader', 'channel', 'creator', 'channel_name']:
                        if video_info.get(field):
                            artist_name = str(video_info[field])
                            if len(artist_name) > 100:
                                artist_name = artist_name[:97] + "..."
                            artist = artist_name
                            break
                    
                    duration = int(video_info.get('duration', 0))
                    if duration == 0:
                        duration = 180  # Дефолтная длительность
                    
                    track_info = TrackInfo(
                        title=title,
                        artist=artist,
                        duration=duration,
                        source=source.value
                    )
                    
                    logger.info(f"Успешно: {artist} - {title} ({duration} сек)")
                    return (expected_file, track_info)
                    
                except asyncio.TimeoutError:
                    logger.error(f"Таймаут {source.value} (попытка {attempt + 1})")
                    if attempt == 0:
                        await asyncio.sleep(2)
                    continue
                    
                except yt_dlp.utils.DownloadError as e:
                    error_msg = str(e)
                    logger.error(f"Ошибка загрузки {source.value}: {error_msg[:200]}")
                    
                    # Проверяем блокировку
                    if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                        if any(pattern in error_msg for pattern in 
                               ["HTTP Error 429", "Too Many Requests", "blocked", 
                                "captcha", "Sign in", "login_required"]):
                            logger.error("⚠️ YouTube заблокировал запрос! Проверьте cookies.")
                            raise Exception("YouTube заблокировал запрос. Нужны свежие cookies.")
                    
                    if attempt == 0:
                        await asyncio.sleep(1)
                    continue
                    
                except Exception as e:
                    logger.error(f"Неожиданная ошибка {source.value}: {str(e)[:200]}")
                    if attempt == 0:
                        await asyncio.sleep(1)
                    continue
            
            logger.error(f"Не удалось скачать с {source.value} после 2 попыток: {query}")
            return None
    
    async def download_audiobook(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        """Специализированный поиск аудиокниг."""
        async with download_lock:
            logger.info(f"Поиск аудиокниги: '{query}'")
            
            # Для аудиокниг используем расширенный поиск
            search_queries = [
                f"{query} аудиокнига полная версия",
                f"{query} аудиокнига",
                f"{query} полная версия",
                f"{query} full audiobook"
            ]
            
            ydl_opts = self._get_ydl_options(source)
            
            for search_query in search_queries:
                try:
                    logger.info(f"Пробую поиск: '{search_query}'")
                    
                    # Поиск без скачивания сначала
                    if source in [Source.YOUTUBE, Source.YOUTUBE_MUSIC]:
                        yt_search = f"ytsearch10:{search_query}"
                    else:
                        yt_search = search_query
                    
                    def search_task():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            return ydl.extract_info(yt_search, download=False)
                    
                    info = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, search_task),
                        timeout=30
                    )
                    
                    if not info or 'entries' not in info:
                        continue
                    
                    entries = [e for e in info['entries'] if e]
                    if not entries:
                        continue
                    
                    # Ищем самые длинные треки (предположительно аудиокниги)
                    long_tracks = [e for e in entries if e.get('duration', 0) > 1800]  # Более 30 минут
                    
                    if long_tracks:
                        # Берем самый длинный
                        chosen_track = max(long_tracks, key=lambda x: x.get('duration', 0))
                    else:
                        # Или самый длинный из всех
                        chosen_track = max(entries, key=lambda x: x.get('duration', 0))
                    
                    # Скачиваем выбранный трек
                    video_id = chosen_track['id']
                    self.last_video_id = video_id
                    
                    def download_audio():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([video_id])
                    
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, download_audio),
                        timeout=120
                    )
                    
                    # Проверяем файл
                    expected_file = os.path.join(DOWNLOADS_DIR, f"{video_id}.mp3")
                    if not os.path.exists(expected_file):
                        import glob
                        pattern = os.path.join(DOWNLOADS_DIR, f"{video_id}.*")
                        files = glob.glob(pattern)
                        if not files:
                            continue
                        expected_file = files[0]
                    
                    # Формируем информацию
                    title = chosen_track.get('title', 'Unknown Audiobook')
                    if len(title) > 100:
                        title = title[:97] + "..."
                    
                    artist = chosen_track.get('uploader', 'Unknown Author')
                    if len(artist) > 100:
                        artist = artist[:97] + "..."
                    
                    duration = chosen_track.get('duration', 0)
                    
                    track_info = TrackInfo(
                        title=title,
                        artist=artist,
                        duration=duration,
                        source=f"Audiobook {source.value}"
                    )
                    
                    logger.info(f"Найдена аудиокнига: {artist} - {title} ({duration} сек)")
                    return (expected_file, track_info)
                    
                except Exception as e:
                    logger.warning(f"Поиск '{search_query}' не удался: {e}")
                    continue
            
            logger.error(f"Не удалось найти аудиокнигу: {query}")
            return None
    
    async def close(self):
        """Очистка ресурсов."""
        self.cleanup_cookies()
        logger.info("Загрузчик остановлен")