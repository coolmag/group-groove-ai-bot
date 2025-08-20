# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import random
import shutil
import time
from typing import List, Optional
from pathlib import Path

import yt_dlp
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from config import Constants, State, NowPlaying, YOUTUBE_COOKIES, RADIO_CHAT_ID, DOWNLOAD_DIR
from utils import set_escaped_error, escape_markdown_v2, save_state_from_botdata

logger = logging.getLogger(__name__)

# Глобальная переменная для отслеживания последнего запроса к YouTube
last_youtube_request = 0

async def get_tracks_youtube(genre: str) -> List[dict]:
    global last_youtube_request
    
    # Добавляем задержку между запросами чтобы избежать блокировок
    current_time = time.time()
    if current_time - last_youtube_request < 2:  # 2 секунды между запросами
        await asyncio.sleep(2 - (current_time - last_youtube_request))
    
    last_youtube_request = time.time()
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f"ytsearch{Constants.SEARCH_LIMIT}:{genre} music",
        'noplaylist': True,
        'quiet': True,
        'extract_flat': True,
        'ignoreerrors': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'extractor_args': {
            'youtube': {
                'skip': ['dash', 'hls'],
                'player_client': ['android', 'web']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Accept-Encoding': 'gzip,deflate',
            'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.7',
            'Connection': 'keep-alive',
        }
    }
    
    if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
        ydl_opts['cookiefile'] = YOUTUBE_COOKIES
        logger.info("Using YouTube cookies for authentication")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Используем более специфический запрос для музыки
            query = f"{genre} music official audio"
            info = await asyncio.to_thread(ydl.extract_info, query, download=False)
        
        if not info or 'entries' not in info:
            logger.warning(f"No results found for YouTube query: {query}")
            return []
            
        tracks = []
        for e in info.get("entries", []):
            if e and e.get("url") and e.get("duration", 0) > 30:  # Фильтруем короткие видео
                # Проверяем, что это музыкальный контент
                title = e.get("title", "").lower()
                if any(x in title for x in ['official', 'audio', 'lyric', 'music', 'song']):
                    tracks.append({
                        "url": e["url"], 
                        "title": e.get("title", "Unknown"), 
                        "duration": e.get("duration", 0)
                    })
                # Также добавляем длинные видео, которые могут быть музыкальными
                elif e.get("duration", 0) > 120:
                    tracks.append({
                        "url": e["url"], 
                        "title": e.get("title", "Unknown"), 
                        "duration": e.get("duration", 0)
                    })
        
        logger.info(f"Found {len(tracks)} YouTube tracks for '{genre}'")
        return tracks
        
    except Exception as e:
        logger.error(f"YouTube search failed for '{genre}': {e}")
        # При ошибке пробуем альтернативный подход
        return await get_tracks_youtube_fallback(genre)

async def get_tracks_youtube_fallback(genre: str) -> List[dict]:
    """Альтернативный метод поиска на YouTube через другие запросы"""
    try:
        # Попробуем разные варианты запросов
        queries = [
            f"{genre} music",
            f"{genre} songs",
            f"{genre} official audio",
            f"{genre} full album"
        ]
        
        for query in queries:
            ydl_opts = {
                'format': 'bestaudio/best',
                'default_search': f"ytsearch10:{query}",
                'noplaylist': True,
                'quiet': True,
                'extract_flat': True,
                'ignoreerrors': True,
            }
            
            if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
                ydl_opts['cookiefile'] = YOUTUBE_COOKIES
                
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
                
            if info and 'entries' in info and info['entries']:
                tracks = [
                    {"url": e["url"], "title": e.get("title", "Unknown"), "duration": e.get("duration", 0)}
                    for e in info.get("entries", []) if e and e.get("url")
                ]
                if tracks:
                    logger.info(f"Found {len(tracks)} YouTube tracks using fallback for '{genre}'")
                    return tracks
                    
            await asyncio.sleep(1)  # Задержка между запросами
            
    except Exception as e:
        logger.error(f"YouTube fallback search also failed for '{genre}': {e}")
    
    return []

async def refill_playlist(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info(f"Refilling playlist from {state.source} for genre: {state.genre}")
    
    if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY * 0.5:
        state.played_radio_urls.clear()
        logger.debug("Cleared played URLs to manage memory")

    # Всегда используем YouTube как основной источник
    original_genre = state.genre
    for attempt in range(Constants.MAX_RETRIES):
        try:
            tracks = await get_tracks_youtube(state.genre)
            if not tracks:
                logger.warning(f"No tracks found on YouTube for genre {state.genre}, attempt {attempt + 1}")
                set_escaped_error(state, f"No tracks found on YouTube for genre {state.genre}")
                
                # Пробуем альтернативные жанры
                alternative_genres = [
                    'pop', 'rock', 'electronic', 'hip hop', 'jazz', 
                    'classical', 'lofi', 'chill', 'ambient'
                ]
                
                if state.genre not in alternative_genres:
                    state.genre = random.choice(alternative_genres)
                    logger.info(f"Trying alternative genre: {state.genre}")
                    await context.bot.send_message(
                        RADIO_CHAT_ID, 
                        f"🔀 No tracks found for '{original_genre}'. Trying '{state.genre}' instead."
                    )
                    continue
                
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            filtered_tracks = [
                t for t in tracks
                if t.get("duration") and Constants.MIN_DURATION <= t["duration"] <= Constants.MAX_DURATION
                and t["url"] not in state.played_radio_urls
            ]
            
            if not filtered_tracks:
                logger.warning(f"No valid tracks after filtering on YouTube")
                set_escaped_error(state, f"No valid tracks after filtering on YouTube")
                state.played_radio_urls.clear()
                
                if attempt == Constants.MAX_RETRIES - 1:
                    state.genre = Constants.DEFAULT_GENRE
                    state.radio_playlist.clear()
                    state.played_radio_urls.clear()
                
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue

            urls = [t["url"] for t in filtered_tracks]
            random.shuffle(urls)
            state.radio_playlist.extend(urls)
            state.retry_count = 0
            state.genre = original_genre  # Восстанавливаем оригинальный жанр
            logger.info(f"Added {len(urls)} tracks to playlist")
            await save_state_from_botdata(context.bot_data)
            return
            
        except Exception as e:
            logger.error(f"Playlist refill failed, attempt {attempt + 1}: {e}")
            set_escaped_error(state, f"Playlist refill error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Playlist refill error: {e}")
            state.retry_count += 1
            await asyncio.sleep(Constants.RETRY_INTERVAL)

    logger.error(f"Failed to refill playlist after {Constants.MAX_RETRIES} attempts")
    state.genre = Constants.DEFAULT_GENRE
    set_escaped_error(state, f"Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to default genre.")
    await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Failed to find tracks after {Constants.MAX_RETRIES} attempts. Switched to default genre.")
    await save_state_from_botdata(context.bot_data)

# Остальные функции остаются без изменений, но убедитесь, что они используют YouTube
async def _refill_playlist_if_needed(context: ContextTypes.DEFAULT_TYPE):
    """Checks if the playlist is running low and refills it in the background."""
    state: State = context.bot_data['state']
    if len(state.radio_playlist) < Constants.REFILL_THRESHOLD:
        if context.bot_data['refill_lock'].locked():
            logger.info("Refill is already in progress.")
            return
        
        async with context.bot_data['refill_lock']:
            logger.info("Playlist running low, starting background refill...")
            await refill_playlist(context)
            logger.info("Background playlist refill complete.")

async def check_track_validity(url: str) -> Optional[dict]:
    # Для YouTube добавляем специальные опции
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'simulate': True,
        'ignoreerrors': True,
        'socket_timeout': 30,
        'retries': 3,
    }
    
    if "youtube.com" in url or "youtu.be" in url:
        if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
        
        if not info:
            return None
            
        return {
            "url": url,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0)
        }
    except Exception as e:
        logger.error(f"Track validity check failed for {url}: {e}")
        return None

async def download_and_send_track(context: ContextTypes.DEFAULT_TYPE, url: str) -> int:
    state: State = context.bot_data['state']
    
    # Добавляем задержку между загрузками
    await asyncio.sleep(1)
    
    track_info = await check_track_validity(url)
    if not track_info:
        set_escaped_error(state, "Invalid track URL")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Invalid track URL.")
        state.now_playing = None
        return 0
    
    duration = track_info.get("duration", 0)
    if not (Constants.MIN_DURATION <= duration <= Constants.MAX_DURATION):
        set_escaped_error(state, f"Duration out of range ({duration}s)")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Track duration out of range ({duration}s).")
        state.now_playing = None
        return 0

    DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
    if not os.access(DOWNLOAD_DIR, os.W_OK):
        set_escaped_error(state, "Download directory not writable")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Download directory not writable.")
        state.now_playing = None
        return 0

    # Настройки для YouTube
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(DOWNLOAD_DIR / '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'ffmpeg_location': shutil.which("ffmpeg"),
        'ffprobe_location': shutil.which("ffprobe"),
        'ignoreerrors': True,
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'socket_timeout': 30,
    }
    
    if "youtube.com" in url or "youtu.be" in url:
        if YOUTUBE_COOKIES and os.path.exists(YOUTUBE_COOKIES):
            ydl_opts['cookiefile'] = YOUTUBE_COOKIES

    filepath = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
        
        # Обработка случая, когда трек недоступен
        if not info:
            logger.warning(f"Track not available: {url}")
            return 0
            
        filepath = Path(ydl.prepare_filename(info))

        if not filepath or not filepath.exists():
            # Попробуем найти файл с другим расширением
            possible_extensions = ['.mp3', '.m4a', '.webm']
            for ext in possible_extensions:
                alt_path = filepath.with_suffix(ext)
                if alt_path.exists():
                    filepath = alt_path
                    break
            else:
                set_escaped_error(state, "Failed to download track")
                await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Failed to download track.")
                state.now_playing = None
                return 0

        if filepath.stat().st_size > Constants.MAX_FILE_SIZE:
            set_escaped_error(state, "Track exceeds max file size")
            await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Track too large to send.")
            state.now_playing = None
            return 0

        track_duration = int(info.get("duration", 0))
        state.now_playing = NowPlaying(
            title=info.get("title", "Unknown Track"),
            duration=track_duration,
            url=url
        )
        
        with open(filepath, 'rb') as f:
            await context.bot.send_audio(
                chat_id=RADIO_CHAT_ID,
                audio=f,
                title=state.now_playing.title,
                duration=state.now_playing.duration,
                performer=info.get("uploader", "Unknown Artist")
            )
        logger.info(f"Sent track: {state.now_playing.title}")
        return track_duration
        
    except asyncio.TimeoutError:
        set_escaped_error(state, "Track download timeout")
        await context.bot.send_message(RADIO_CHAT_ID, "[ERR] Track download timed out.")
        return 0
    except TelegramError as e:
        set_escaped_error(state, f"Telegram error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Telegram error: {e}")
        return 0
    except Exception as e:
        set_escaped_error(state, f"Track processing error: {e}")
        await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Track processing error: {e}")
        return 0
    finally:
        state.now_playing = None
        if filepath and filepath.exists():
            try:
                filepath.unlink(missing_ok=True)
            except Exception:
                pass

async def radio_loop(context: ContextTypes.DEFAULT_TYPE):
    state: State = context.bot_data['state']
    logger.info("Starting radio loop")
    
    while True:
        try:
            if not state.is_on:
                logger.info("Radio is off, sleeping")
                await asyncio.sleep(10)
                continue

            asyncio.create_task(_refill_playlist_if_needed(context))
                
            if not state.radio_playlist:
                logger.warning("Playlist is empty, waiting for refill...")
                await asyncio.sleep(Constants.RETRY_INTERVAL)
                continue
            
            url = state.radio_playlist.popleft()
            state.played_radio_urls.append(url)
            
            if len(state.played_radio_urls) > Constants.PLAYED_URLS_MEMORY:
                state.played_radio_urls.popleft()
            
            logger.info(f"Playing track: {url}")
            track_duration = await download_and_send_track(context, url)
            await save_state_from_botdata(context.bot_data)
            
            sleep_time = (track_duration or 0) + Constants.PAUSE_BETWEEN_TRACKS
            
            logger.debug(f"Waiting for {sleep_time} seconds until next track")
            await asyncio.sleep(sleep_time)
            
        except asyncio.CancelledError:
            logger.info("Radio loop cancelled")
            return
        except Exception as e:
            logger.error(f"Radio loop error: {e}")
            set_escaped_error(state, f"Radio loop error: {e}")
            await context.bot.send_message(RADIO_CHAT_ID, f"[ERR] Radio error: {e}")
            await asyncio.sleep(10)
