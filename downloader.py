import os
import asyncio
import logging
from typing import Optional, Tuple

import yt_dlp

from config import DOWNLOADS_DIR, PROXY_URL, PROXY_ENABLED, YOUTUBE_COOKIES_PATH, SOUNDCLOUD_COOKIES_PATH, TrackInfo

log = logging.getLogger("downloader")

class AudioDownloadManager:
    def __init__(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def _build_opts(self, outtmpl: str, cookies: Optional[str] = None, prefer_m4a=False):
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3" if not prefer_m4a else "m4a",
            "preferredquality": "0"
        }]
        return {
            "outtmpl": outtmpl,
            "format": "bestaudio/best",
            "postprocessors": postprocessors,
            "quiet": True,
            "no_warnings": False,
            "retries": 3,
            "socket_timeout": 30,
            **({"cookiefile": cookies} if cookies and os.path.exists(cookies) else {}),
            **({"proxy": PROXY_URL} if PROXY_ENABLED and PROXY_URL else {}),
        }

    async def _download(self, url: str, prefer_m4a=False, cookies: Optional[str]=None) -> Optional[Tuple[str, TrackInfo]]:
        filename = os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s")
        ydl_opts = self._build_opts(filename, cookies=cookies, prefer_m4a=prefer_m4a)
        def _run():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # after postprocessing -> set final path
                realpath = ydl.prepare_filename(info)
                # replace ext to mp3/m4a
                base = os.path.splitext(realpath)[0]
                final_path = base + (".m4a" if prefer_m4a else ".mp3")
                ti = TrackInfo(title=info.get("title") or "Unknown", artist=info.get("uploader"), duration=info.get("duration"), url=info.get("webpage_url"), source=info.get("extractor_key"))
                return final_path, ti
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _run)
        except Exception as e:
            log.warning("Download failed for %s: %s", url, e)
            return None

    async def download_by_query(self, query: str) -> Optional[Tuple[str, TrackInfo]]:
        # Try YouTube → SoundCloud
        for url in (f"ytsearch1:{query}", f"scsearch1:{query}"):
            cookies = YOUTUBE_COOKIES_PATH if url.startswith("yt") else SOUNDCLOUD_COOKIES_PATH
            res = await self._download(url, prefer_m4a=url.startswith("ytm"), cookies=cookies)
            if res:
                return res
        return None
