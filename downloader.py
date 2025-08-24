# downloader.py (v8 фикс)
import logging
import yt_dlp
from config import DOWNLOADS_DIR, Source, TrackInfo

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    @staticmethod
    async def download_track(query: str, source: Source) -> TrackInfo | None:
        try:
            if source == Source.YOUTUBE:
                url = f"ytsearch:{query}"
            elif source == Source.VKMUSIC:
                # Заглушка для будущей интеграции VK
                url = f"ytsearch:{query}"
            else:
                raise ValueError(f"Unknown source: {source}")

            logger.info(f"Downloading: '{query}' from {source.value}")

            ydl_opts = {
                "format": "bestaudio/best",
                "noplaylist": True,
                "quiet": True,
                "extract_flat": False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                return TrackInfo(title=info["title"], url=info["webpage_url"])
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None
