import os
import logging
import asyncio
import random
from typing import Optional, Tuple
import yt_dlp
from sclib.asyncio import SoundcloudAPI, Track

from config import DOWNLOADS_DIR, GENRES, TrackInfo
from locks import download_lock

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        self.soundcloud_api = SoundcloudAPI()
        self.ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(DOWNLOADS_DIR, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }],
        }

    def get_random_genre(self) -> str:
        return random.choice(GENRES)

    async def download_track(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        logger.info(f"Searching SoundCloud for: {query}")
        async with download_lock:
            try:
                results = await self.soundcloud_api.search(query, limit=5)
                if not results or not results.tracks:
                    logger.warning(f"SoundCloud found no tracks for query: {query}")
                    return None, None
                
                # Берем случайный трек из найденных
                track: Track = random.choice(results.tracks)
                # Используем yt-dlp для скачивания по прямой ссылке
                return await self._execute_yt_dlp(track.permalink_url)
            except Exception as e:
                logger.error(f"Error processing SoundCloud track for '{query}': {e}", exc_info=True)
                return None, None

    async def _execute_yt_dlp(self, url: str) -> Optional[Tuple[str, TrackInfo]]:
        try:
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                info = await asyncio.wait_for(
                    asyncio.to_thread(ydl.extract_info, url, download=True),
                    timeout=45.0
                )
            
            entry = info.get('entries', [info])[0]
            audio_path = os.path.join(DOWNLOADS_DIR, f"{entry['id']}.mp3")
            track_info = TrackInfo(
                title=entry.get('title', 'Unknown Title'),
                artist=entry.get('uploader', 'Unknown Artist'),
                duration=int(entry.get('duration', 0))
            )
            logger.info(f"Download successful: {track_info.title}")
            return audio_path, track_info
        except asyncio.TimeoutError:
            logger.error(f"Download timed out for url: {url}")
            return None, None
        except Exception as e:
            logger.error(f"yt-dlp download error for url '{url}': {e}", exc_info=True)
            return None, None