import os
import logging
import asyncio
import random
from typing import Optional, Tuple
import yt_dlp

from config import DOWNLOADS_DIR, GENRES, Source, TrackInfo

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        self.setup_directories()
        
    def setup_directories(self):
        """Создает необходимые директории"""
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        
    def get_random_genre(self) -> str:
        return random.choice(GENRES)

    async def download_track(self, query: str, source: Source) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Downloading: '{query}' from {source.value}")
        
        methods = {
            Source.YOUTUBE: self.download_from_youtube,
            Source.VK: self.download_from_vk,
            Source.SOUNDCLOUD: self.download_from_soundcloud,
            Source.ARCHIVE: self.download_from_archive,
        }
        
        if source not in methods:
            logger.error(f"Unsupported source: {source}")
            return None, None
            
        return await methods[source](query)

    async def download_from_youtube(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивание с YouTube"""
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'socket_timeout': 30,
            'retries': 3,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Search for the video
                info = await asyncio.to_thread(
                    ydl.extract_info, 
                    f"ytsearch1:{query}", 
                    download=False
                )
                
                if not info or 'entries' not in info or not info['entries']:
                    logger.warning(f"No YouTube results for: {query}")
                    return None, None
                
                # Get the first result
                video_info = info['entries'][0]
                video_url = video_info['webpage_url']
                
                # Download the video
                download_info = await asyncio.to_thread(
                    ydl.extract_info, 
                    video_url, 
                    download=True
                )
                
                filename = ydl.prepare_filename(download_info)
                mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'
                
                track_info = TrackInfo(
                    title=video_info.get('title', 'Unknown'),
                    artist=video_info.get('uploader', 'Unknown Artist'),
                    duration=video_info.get('duration', 0)
                )
                
                return mp3_filename, track_info
                
        except Exception as e:
            logger.error(f"YouTube download error: {str(e)}")
            return None, None

    async def download_from_vk(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивание из VK"""
        logger.warning("VK download not implemented yet, falling back to YouTube")
        return await self.download_from_youtube(query)

    async def download_from_soundcloud(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивание с SoundCloud"""
        logger.warning("SoundCloud download not implemented yet, falling back to YouTube")
        return await self.download_from_youtube(query)

    async def download_from_archive(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        """Скачивание с Archive.org"""
        logger.warning("Archive.org download not implemented yet, falling back to YouTube")
        return await self.download_from_youtube(query)
