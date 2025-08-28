import logging
from pyrogram import Client
from pytgcalls import GroupCall

from config import API_ID, API_HASH, BOT_TOKEN

logger = logging.getLogger(__name__)

# Для v3 нужно вручную управлять экземплярами звонков для каждого чата
group_calls = {}

class VoiceEngine:
    def __init__(self):
        self._app = Client(
            name="pyrogram_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
        )
        self._is_running = False

    async def start(self):
        if self._is_running:
            return
        logger.info("Starting Voice Engine (Pyrogram Client)...")
        await self._app.start()
        self._is_running = True
        logger.info("Voice Engine started successfully.")

    async def stop(self):
        if not self._is_running:
            return
        logger.info("Stopping Voice Engine...")
        for chat_id in list(group_calls.keys()):
            await self.leave_chat(chat_id)
        await self._app.stop()
        self._is_running = False
        logger.info("Voice Engine stopped.")

    def _get_group_call(self, chat_id: int) -> GroupCall:
        if chat_id not in group_calls:
            group_calls[chat_id] = GroupCall(self._app, chat_id)
        return group_calls[chat_id]

    async def join_chat(self, chat_id: int):
        try:
            group_call = self._get_group_call(chat_id)
            await group_call.join()
            logger.info(f"Successfully joined voice chat: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to join voice chat {chat_id}: {e}")

    async def leave_chat(self, chat_id: int):
        try:
            if chat_id in group_calls:
                await group_calls[chat_id].leave()
                del group_calls[chat_id]
                logger.info(f"Successfully left voice chat: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to leave voice chat {chat_id}: {e}")

    async def play_audio(self, chat_id: int, audio_file_path: str):
        try:
            group_call = self._get_group_call(chat_id)
            # В v3 для проигрывания используется метод start_audio
            await group_call.start_audio(audio_file_path, repeat=False)
            logger.info(f"Started playing {audio_file_path} in chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to play audio in chat {chat_id}: {e}")