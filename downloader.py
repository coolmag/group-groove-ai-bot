import os
import logging
import asyncio
from typing import Optional, Tuple, List
import yt_dlp

from config import DOWNLOADS_DIR, Source, TrackInfo, PROXY_URL, PROXY_ENABLED, YOUTUBE_COOKIES_PATH, FFMPEG_LOCATION

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        # defaults tuning
        self.socket_timeout = 20
        self.retries = 2

    async def check_ffmpeg(self) -> None:
        for bin_name in ("ffmpeg", "ffprobe"):
            try:
                proc = await asyncio.create_subprocess_exec(bin_name, "-version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await proc.communicate()
                head = (out or b"").decode(errors="ignore").splitlines()[:1]
                logger.info("%s: %s", bin_name, head[0] if head else "(no output)")
            except Exception as e:
                logger.warning("Failed to run %s -version: %s", bin_name, e)

    def _base_opts(self) -> dict:
        opts = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "socket_timeout": self.socket_timeout,
            "retries": self.retries,
            "extract_flat": False,
            "outtmpl": os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s"),
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36",
        }
        if YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts["cookiefile"] = YOUTUBE_COOKIES_PATH
            logger.info("yt-dlp: using cookiefile")
        if PROXY_ENABLED and PROXY_URL:
            opts["proxy"] = PROXY_URL
            logger.info("yt-dlp: using proxy %s", PROXY_URL)
        if FFMPEG_LOCATION:
            opts["ffmpeg_location"] = FFMPEG_LOCATION
        return opts

    def _dl_opts(self, codec: str) -> dict:
        o = self._base_opts()
        o["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": "192",
        }]
        if codec == "m4a":
            o["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        else:
            o["format"] = "bestaudio/best"
        return o

    async def search_tracks(self, query: str, source: Source, limit: int = 10) -> List[TrackInfo]:
        prefix_map = {
            Source.YOUTUBE: "ytsearch",
            Source.YOUTUBE_MUSIC: "ytmsearch",
            Source.SOUNDCLOUD: "scsearch",
            Source.JAMENDO: "jamendosearch",
            Source.ARCHIVE: "iasearch",
        }
        prefix = prefix_map.get(source, "ytsearch")
        search_q = f"{prefix}{limit}:{query}"
        logger.info("Searching for '%s' on %s", query, source.value)
        try:
            opts = self._base_opts()
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, search_q, download=False)
            entries = info.get("entries", []) if info else []
        except Exception as e:
            logger.error("Search failed: %s", e)
            return []
        results = []
        for e in entries[:limit]:
            if not e:
                continue
            url = e.get("webpage_url") or e.get("url") or ""
            if not url: continue
            title = e.get("title") or "Unknown"
            artist = e.get("artist") or e.get("uploader") or "Unknown Artist"
            duration = int(e.get("duration") or 0)
            tid = e.get("id") or url
            results.append(TrackInfo(id=str(tid), title=title, artist=artist, duration=duration, source=e.get("extractor_key") or source.value, url=url))
        logger.info("Search returned %d result(s)", len(results))
        return results

    async def download_by_url(self, url: str, prefer_mp3: bool = True) -> Optional[Tuple[str, TrackInfo]]:
        logger.info("Downloading URL: %s", url)
        for codec in (["mp3","m4a"] if prefer_mp3 else ["m4a","mp3"]):
            try:
                opts = self._dl_opts(codec)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                    entry = info.get("entries", [info])[0] if info else None
                    if not entry: continue
                    filename = ydl.prepare_filename(entry)
                    out_file = os.path.splitext(filename)[0] + f".{codec}"
                    ti = TrackInfo(
                        id=str(entry.get("id") or ""),
                        title=entry.get("title") or "Unknown",
                        artist=entry.get("artist") or entry.get("uploader") or "Unknown Artist",
                        duration=int(entry.get("duration") or 0),
                        source=entry.get("extractor_key") or "Unknown",
                        url=entry.get("webpage_url") or url,
                    )
                    if os.path.exists(out_file):
                        logger.info("Download OK -> %s", out_file)
                        return out_file, ti
                    cand = self._find_downloaded_file(entry)
                    if cand and os.path.exists(cand):
                        logger.info("Using fallback file: %s", cand)
                        return cand, ti
            except Exception as e:
                logger.error("Download failed for codec %s: %s", codec, e)
        logger.error("Failed to download URL: %s", url)
        return None

    def _find_downloaded_file(self, entry) -> Optional[str]:
        vid = entry.get("id") if isinstance(entry, dict) else None
        if not vid: return None
        for ext in ("mp3","m4a","webm","opus","m4b","mp4"):
            p = os.path.join(DOWNLOADS_DIR, f"{vid}.{ext}")
            if os.path.exists(p): return p
        return None
