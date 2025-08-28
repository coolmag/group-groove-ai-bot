import asyncio
import logging
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped

from config import API_ID, API_HASH, BOT_TOKEN

logger = logging.getLogger(__name__)

class VoiceEngine:
    def __init__(self):
        self._app = Client(
            name="pyrogram_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            # in_memory=True # Для хранения сессии в памяти
        )
        self._call_manager = PyTgCalls(self._app)
        self._is_running = False

    async def start(self):
        """Запускает Pyrogram клиент и обработчик звонков."""
        if self._is_running:
            return
        logger.info("Starting Voice Engine (Pyrogram & PyTgCalls)...")
        await self._app.start()
        await self._call_manager.start()
        self._is_running = True
        logger.info("Voice Engine started successfully.")

    async def stop(self):
        """Останавливает все компоненты движка."""
        if not self._is_running:
            return
        logger.info("Stopping Voice Engine...")
        await self._call_manager.stop()
        await self._app.stop()
        self._is_running = False
        logger.info("Voice Engine stopped.")

    async def join_chat(self, chat_id: int):
        """Подключается к голосовому чату."""
        try:
            await self._call_manager.join_group_call(chat_id, AudioPiped("")) # Пустой файл для инициализации
            logger.info(f"Successfully joined voice chat: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to join voice chat {chat_id}: {e}")

    async def leave_chat(self, chat_id: int):
        """Отключается от голосового чата."""
        try:
            await self._call_manager.leave_group_call(chat_id)
            logger.info(f"Successfully left voice chat: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to leave voice chat {chat_id}: {e}")

    async def play_audio(self, chat_id: int, audio_file_path: str):
        """Проигрывает аудиофайл в голосовом чате."""
        try:
            await self._call_manager.change_stream(
                chat_id,
                AudioPiped(audio_file_path),
            )
            logger.info(f"Started playing {audio_file_path} in chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to play audio in chat {chat_id}: {e}")
