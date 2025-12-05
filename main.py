import asyncio
import logging
import signal
import sys

from telegram.ext import Application

from config import settings
from handlers import BotHandlers

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    """Основная функция запуска бота"""
    logger.info("🚀 Запуск Music Bot v2.0...")
    
    # Проверка обязательных переменных
    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        sys.exit(1)
    
    # Проверка FFmpeg
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.error("❌ FFmpeg не найден!")
            sys.exit(1)
        logger.info("✅ FFmpeg доступен")
    except Exception as e:
        logger.error(f"❌ Ошибка проверки FFmpeg: {e}")
        sys.exit(1)
    
    app = None  # Инициализируем app как None
    try:
        # Создание приложения
        app = Application.builder().token(settings.BOT_TOKEN).build()
        handlers = BotHandlers(app)
        
        # Регистрация обработчиков
        await handlers.register_handlers(app)
        
        # Запуск бота
        logger.info("✅ Бот запускается...")
        await app.initialize()
        
        if app.updater:
            await app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"]
            )
        
        logger.info("✅ Бот успешно запущен и ожидает сообщений...")
        
        # Ожидание сигнала завершения
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
            
        await stop_event.wait()
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в main: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("👋 Завершение работы бота...")
        if app and app.updater:
            await app.updater.stop()
        if app:
            await app.stop()
            await app.shutdown()
        logger.info("Бот полностью остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем (KeyboardInterrupt)")
    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка на верхнем уровне: {e}", exc_info=True)
        sys.exit(1)