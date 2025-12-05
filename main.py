import asyncio
import os
import signal
import sys
import logging
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.error import BadRequest, Forbidden

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Импорты наших модулей
from config import settings, TrackInfo, Source
from handlers import BotHandlers


async def main():
    """Главная функция бота"""
    logger.info("🚀 Запуск Music Bot...")
    
    # Проверка токена
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
    
    try:
        # Создаем приложение бота
        app = Application.builder().token(settings.BOT_TOKEN).build()
        
        # Создаем обработчики
        handlers = BotHandlers()
        
        # Регистрируем команды
        commands = [
            ("start", handlers.start),
            ("menu", handlers.show_menu),
            ("play", handlers.handle_play),
            ("p", handlers.handle_play),
            ("audiobook", handlers.handle_audiobook),
            ("ab", handlers.handle_audiobook),
            ("radio", handlers.handle_radio),
            ("source", handlers.handle_source),
            ("src", handlers.handle_source),
            ("status", handlers.handle_status),
            ("stat", handlers.handle_status),
            ("help", handlers.handle_help),
        ]
        
        for command, handler in commands:
            app.add_handler(CommandHandler(command, handler))
        
        # Регистрируем колбэки
        app.add_handler(CallbackQueryHandler(handlers.handle_callback))
        
        # Запускаем бота
        logger.info("✅ Бот запускается...")
        await app.initialize()
        
        if app.updater:
            await app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"]
            )
        
        logger.info("✅ Бот успешно запущен!")
        
        # Ждем сигнала завершения
        stop_event = asyncio.Event()
        
        def signal_handler():
            logger.info("Получен сигнал завершения")
            stop_event.set()
        
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
        
        await stop_event.wait()
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        raise
    finally:
        logger.info("Завершение работы бота...")
        try:
            if 'app' in locals():
                if app.updater:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
        except Exception as e:
            logger.error(f"Ошибка при завершении: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        sys.exit(1)