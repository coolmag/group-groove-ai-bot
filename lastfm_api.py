import aiohttp
import logging
import os

log = logging.getLogger("lastfm")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")

async def get_top_tracks_by_genre(genre: str, limit: int = 20):
    if not LASTFM_API_KEY:
        log.error("LASTFM_API_KEY is missing")
        return []
    url = "http://ws.audioscrobbler.com/2.0/"
    params = {
        "method": "tag.gettoptracks",
        "tag": genre,
        "limit": limit,
        "api_key": LASTFM_API_KEY,
        "format": "json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=20) as resp:
                if resp.status != 200:
                    log.warning("Last.fm status=%s", resp.status)
                    return []
                data = await resp.json()
                tracks = data.get("tracks", {}).get("track", [])
                out = []
                for t in tracks:
                    name = t.get("name")
                    artist = (t.get("artist") or {}).get("name")
                    if name and artist:
                        out.append(f"{artist} - {name}")
                return out
    except Exception as e:
        log.exception("Last.fm error: %s", e)
        return []
