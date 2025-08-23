import os
import logging
import asyncio
from typing import Optional, Tuple, List
import yt_dlp

from config import (
    DOWNLOADS_DIR, GENRES, Source, TrackInfo,
    PROXY_URL, PROXY_ENABLED, YOUTUBE_COOKIES_PATH, FFMPEG_LOCATION
)

logger = logging.getLogger(__name__)

class AudioDownloadManager:
    def __init__(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    async def check_ffmpeg(self) -> None:
        """Log ffmpeg/ffprobe versions if available."""
        for bin_name in ("ffmpeg", "ffprobe"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    bin_name, "-version",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                out, _ = await proc.communicate()
                logger.info("%s: %s", bin_name, (out or b"").decode(errors="ignore").splitlines()[0])
            except Exception as e:
                logger.warning("Failed to run %s -version: %s", bin_name, e)

    def _base_opts(self) -> dict:
        opts = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "socket_timeout": 30,
            "retries": 3,
            "extract_flat": False,
            "outtmpl": os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s"),
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"
            ),
        }
        if YOUTUBE_COOKIES_PATH and os.path.exists(YOUTUBE_COOKIES_PATH):
            opts["cookiefile"] = YOUTUBE_COOKIES_PATH
            logger.info("Using YouTube cookies file.")
        if PROXY_ENABLED and PROXY_URL:
            opts["proxy"] = PROXY_URL
            logger.info("Using proxy: %s", PROXY_URL)
        if FFMPEG_LOCATION:
            # Path (dir) that contains binaries, or full path is also supported by yt-dlp
            opts["ffmpeg_location"] = FFMPEG_LOCATION
        return opts

    def _dl_opts_mp3(self) -> dict:
        o = self._base_opts()
        o["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        o["format"] = "bestaudio/best"
        return o

    def _dl_opts_m4a(self) -> dict:
        o = self._base_opts()
        o["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "192",
        }]
        # Prefer m4a first if available
        o["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        return o

    async def search_tracks(self, query: str, source: Source, limit: int = 10) -> List[TrackInfo]:
        """Return up to 'limit' TrackInfo results for UI selection."""
        prefix_map = {
            Source.YOUTUBE: "ytsearch",
            Source.YOUTUBE_MUSIC: "ytmsearch",
            Source.SOUNDCLOUD: "scsearch",
            Source.JAMENDO: "jamendosearch",  # may require API/config; keep for future
            Source.ARCHIVE: "iasearch",       # may differ; kept as placeholder
        }
        prefix = prefix_map.get(source, "ytsearch")
        search_q = f"{prefix}{limit}:{query}"

        try:
            opts = self._base_opts()
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, search_q, download=False)
            entries = info.get("entries", []) if info else []
        except Exception as e:
            logger.error("Search failed for '%s' on %s: %s", query, source.value, e)
            return []

        results: List[TrackInfo] = []
        for e in entries[:limit]:
            if not e:
                continue
            # Robust extraction
            url = e.get("webpage_url") or e.get("url") or ""
            if not url:
                continue
            title = e.get("title") or "Unknown"
            artist = e.get("artist") or e.get("uploader") or "Unknown Artist"
            duration = int(e.get("duration") or 0)
            tid = e.get("id") or url
            results.append(TrackInfo(
                id=str(tid),
                title=title,
                artist=artist,
                duration=duration,
                source=e.get("extractor_key") or source.value,
                url=url
            ))
        return results

    async def download_by_url(self, url: str, prefer_mp3: bool = True) -> Optional[Tuple[str, TrackInfo]]:
        """Download audio from a direct media page URL and return path + TrackInfo.
        Performs mp3 conversion first, then falls back to m4a if ffmpeg/probe fails.
        """
        # First try MP3
        for prefer in (prefer_mp3, not prefer_mp3):
            try_m4a = not prefer
            opts = self._dl_opts_mp3() if not try_m4a else self._dl_opts_m4a()
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, url, download=True)
                if not info:
                    logger.warning("No info after download for url: %s", url)
                    continue
                # either single dict or entries list
                entry = info.get("entries", [info])[0]
                if not entry:
                    continue
                filename = yt_dlp.YoutubeDL(opts).prepare_filename(entry)
                # Result file extension depends on postprocessor
                ext = "m4a" if try_m4a else "mp3"
                out_file = os.path.splitext(filename)[0] + f".{ext}"

                ti = TrackInfo(
                    id=str(entry.get("id", "")),
                    title=entry.get("title", "Unknown"),
                    artist=entry.get("artist") or entry.get("uploader") or "Unknown Artist",
                    duration=int(entry.get("duration") or 0),
                    source=entry.get("extractor_key", "Unknown"),
                    url=entry.get("webpage_url") or url
                )
                if os.path.exists(out_file):
                    return out_file, ti
                # Fallback: if postprocessing didn't create expected file, try to find any audio file in downloads
                cand = self._find_downloaded_file(entry)
                if cand and os.path.exists(cand):
                    return cand, ti
            except Exception as e:
                logger.error("Download failed (%s) for url '%s': %s", "m4a" if try_m4a else "mp3", url, e)
                # continue loop to try alternate codec
        return None

    def _find_downloaded_file(self, entry) -> Optional[str]:
        """Try to locate a downloaded file by ID regardless of extension."""
        vid = entry.get("id")
        if not vid:
            return None
        for ext in ("mp3", "m4a", "webm", "opus", "m4b", "mp4"):
            p = os.path.join(DOWNLOADS_DIR, f"{vid}.{ext}")
            if os.path.exists(p):
                return p
        return None

    async def close(self):
        # Placeholder for future resource cleanup
        return
