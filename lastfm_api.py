import aiohttp, os, logging
log = logging.getLogger("lastfm")
API_KEY = os.getenv("LASTFM_API_KEY", "")

async def get_top_tracks_by_genre(genre: str, limit: int = 20):
    if not API_KEY:
        log.warning("LASTFM_API_KEY not set")
        return []
    url = "http://ws.audioscrobbler.com/2.0/"
    params = {"method":"tag.gettoptracks","tag":genre,"limit":limit,"api_key":API_KEY,"format":"json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=20) as resp:
                if resp.status != 200:
                    log.warning("Last.fm returned %s", resp.status)
                    return []
                data = await resp.json()
                tracks = data.get("tracks",{}).get("track",[])
                out = []
                for t in tracks:
                    name = t.get("name")
                    artist = (t.get("artist") or {}).get("name")
                    if name and artist:
                        out.append(f"{artist} - {name}")
                return out
    except Exception as e:
        log.exception("Last.fm fetch failed: %s", e)
        return []
