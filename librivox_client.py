import logging
import aiohttp
from typing import List
from models import AudioBook, AudioBookChapter

logger = logging.getLogger(__name__)

class LibriVoxClient:
    BASE_URL = "https://librivox.org/api/feed/audiobooks"

    async def search_books(self, title_query: str) -> List[AudioBook]:
        params = {
            "title": f"^{title_query}",
            "format": "json",
            "extended": 1,
            "limit": 5,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.BASE_URL, params=params) as response:
                    response.raise_for_status()
                    data = await response.json()
                    if not data.get("books"):
                        return []

                    books = []
                    for book_data in data["books"]:
                        chapters = [
                            AudioBookChapter(
                                title=track["title"],
                                url=track["play_url"]
                            )
                            for track in book_data.get("tracks", [])
                        ]
                        book = AudioBook(
                            id=book_data["id"],
                            title=book_data["title"],
                            author=book_data["authors"][0]["last_name"],
                            chapters=chapters
                        )
                        books.append(book)
                    return books
        except Exception as e:
            logger.error(f"Error fetching data from LibriVox API: {e}")
            return []