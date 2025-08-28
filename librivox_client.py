import logging
import aiohttp
from typing import List
from models import AudioBook, AudioBookChapter

logger = logging.getLogger(__name__)

class LibriVoxClient:
    BASE_URL = "https://librivox.org/api/feed/audiobooks"

    async def search_books(self, title_query: str) -> List[AudioBook]:
        """Ищет аудиокниги по названию."""
        params = {
            "title": title_query,  # Ищем по всему названию
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
        except aiohttp.ClientError as e:
            logger.error(f"Error fetching data from LibriVox API: {e}")
            return []
        except Exception as e:
            logger.error(f"An unexpected error occurred in LibriVox client: {e}")
            return []
