import logging
import yt_dlp
from models import Source, TrackInfo
from config import PROXY_ENABLED, PROXY_URL, YOUTUBE_COOKIES_PATH

logger = logging.getLogger(__name__)

class TrackInfoExtractor:
    async def extract_track_info(self, query: str, source: Source) -> TrackInfo | None:
        try:
            if source == Source.YOUTUBE:
                url = f"ytsearch:{query}"
            else:
                raise ValueError(f"Unknown source: {source}")

            logger.info(f"Extracting info for: '{query}' from {source.value}")

            ydl_opts = {
                "format": "bestaudio/best",
                "noplaylist": True,
                "quiet": True,
                "extract_flat": False,
            }

            # Добавляем прокси, если включено
            if PROXY_ENABLED and PROXY_URL:
                ydl_opts['proxy'] = PROXY_URL
                logger.info("Using proxy for yt-dlp.")

            # Добавляем cookies, если указан путь
            if YOUTUBE_COOKIES_PATH:
                ydl_opts['cookiefile'] = YOUTUBE_COOKIES_PATH
                logger.info(f"Using cookies for yt-dlp from {YOUTUBE_COOKIES_PATH}.")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                return TrackInfo(title=info["title"], url=info["webpage_url"])
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None
