import asyncio
import random
from logger import logger
from config import settings


class RadioService:
    """Сервис радио"""
    
    def __init__(self, state):
        self.state = state
        self.is_running = False
    
    async def start(self, chat_id: int):
        """Запуск радио"""
        if self.is_running:
            return
        
        self.is_running = True
        self.state.radio.is_on = True
        
        # Создаем задачу радио
        asyncio.create_task(self._radio_loop(chat_id))
        logger.info(f"Радио запущено в чате {chat_id}")
    
    async def stop(self):
        """Остановка радио"""
        self.is_running = False
        self.state.radio.is_on = False
        logger.info("Радио остановлено")
    
    async def _radio_loop(self, chat_id: int):
        """Цикл радио"""
        while self.is_running and self.state.radio.is_on:
            try:
                # Выбираем случайный жанр
                genre = random.choice(settings.RADIO_GENRES)
                self.state.radio.current_genre = genre
                
                logger.info(f"Радио играет {genre} в чате {chat_id}")
                
                # Ждем перед следующим треком
                await asyncio.sleep(settings.RADIO_COOLDOWN)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка радио: {e}")
                await asyncio.sleep(10)