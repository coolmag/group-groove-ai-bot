import asyncio
import random
import os
from telegram.ext import Application
from telegram.constants import ParseMode

from logger import logger
from config import settings
from states import BotState
from base_downloader import BaseDownloader, DownloadResult


class RadioService:
    """Сервис радио, который проигрывает музыку в чате."""
    
    def __init__(self, state: BotState, bot: Application.bot, downloader: BaseDownloader):
        self.state = state
        self.bot = bot
        self.downloader = downloader
        self._task: Optional[asyncio.Task] = None

    async def start(self, chat_id: int):
        """Запускает фоновую задачу радио, если она еще не запущена."""
        if self._task and not self._task.done():
            logger.warning(f"Радио уже запущено в чате {chat_id}.")
            return

        self.state.radio.is_on = True
        self.state.radio.skip_event.clear()
        self._task = asyncio.create_task(self._radio_loop(chat_id))
        logger.info(f"Радио-задача создана для чата {chat_id}")

    async def stop(self):
        """Останавливает радио."""
        self.state.radio.is_on = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Радио остановлено.")

    async def skip(self):
        """Пропускает текущий трек."""
        if self.state.radio.is_on:
            self.state.radio.skip_event.set()
            logger.info("Событие 'skip' установлено.")

    async def _radio_loop(self, chat_id: int):
        """Основной цикл радио."""
        logger.info(f"Радио-цикл запущен для чата {chat_id}")
        await asyncio.sleep(2)  # Небольшая задержка перед стартом

        while self.state.radio.is_on:
            result = None
            try:
                # 1. Выбираем жанр и скачиваем трек
                genre = random.choice(settings.RADIO_GENRES)
                self.state.radio.current_genre = genre
                logger.info(f"[Радио] Играет '{genre}' в чате {chat_id}")
                
                result = await self.downloader.download_with_retry(genre)

                if result and result.success:
                    # 2. Отправляем трек
                    track_info = result.track_info
                    caption = f"📻 *Радио:* {track_info.display_name}"
                    
                    with open(result.file_path, 'rb') as audio:
                        await self.bot.send_audio(
                            chat_id=chat_id,
                            audio=audio,
                            title=track_info.title,
                            performer=track_info.artist,
                            duration=track_info.duration,
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN
                        )
                    
                    # 3. Ждем перед следующим треком
                    try:
                        # Ждем либо до конца кулдауна, либо пока не придет 'skip'
                        await asyncio.wait_for(
                            self.state.radio.skip_event.wait(),
                            timeout=settings.RADIO_COOLDOWN
                        )
                    except asyncio.TimeoutError:
                        # Это нормальный исход, просто продолжаем
                        pass
                    
                    if self.state.radio.skip_event.is_set():
                        logger.info("[Радио] Трек пропущен, играем следующий.")
                        self.state.radio.skip_event.clear()

                else:
                    # Если скачать не удалось, ждем перед новой попыткой
                    logger.warning(f"[Радио] Не удалось скачать трек для жанра '{genre}'.")
                    await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.info("Радио-цикл отменен.")
                break
            except Exception as e:
                logger.error(f"Критическая ошибка в радио-цикле: {e}", exc_info=True)
                await asyncio.sleep(60) # Пауза в случае серьезной ошибки
            finally:
                # 4. Очищаем файл
                if result and result.file_path and os.path.exists(result.file_path):
                    try:
                        os.remove(result.file_path)
                    except OSError as e:
                        logger.error(f"Ошибка удаления файла {result.file_path}: {e}")
        
        logger.info(f"Радио-цикл завершен для чата {chat_id}")