import asyncio
import os
import sys
import signal
import logging
import atexit

# --- Механизм блокировки ---
LOCK_FILE_PATH = "/tmp/music_bot.lock"

def create_lock_file():
    if os.path.exists(LOCK_FILE_PATH):
        # Проверяем, не завис ли старый процесс
        try:
            with open(LOCK_FILE_PATH, "r") as f:
                pid = int(f.read())
            # Проверяем, существует ли процесс с таким PID
            if os.path.exists(f"/proc/{pid}"):
                logging.warning(f"Найден активный lock-файл (PID: {pid}). Другой экземпляр уже запущен.")
                return False
            else:
                logging.warning("Найден старый lock-файл от зависшего процесса. Удаляем его.")
        except (ValueError, FileNotFoundError):
             logging.warning("Найден поврежденный lock-файл. Удаляем его.")

    # Создаем новый lock-файл
    try:
        with open(LOCK_FILE_PATH, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(remove_lock_file)
        return True
    except IOError as e:
        logging.error(f"Не удалось создать lock-файл: {e}")
        return False

def remove_lock_file():
    if os.path.exists(LOCK_FILE_PATH):
        try:
            os.remove(LOCK_FILE_PATH)
            logging.info("Lock-файл удален.")
        except OSError:
            pass

# --- Конец механизма блокировки ---


# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main_bot_logic():
    """Основная логика бота, вынесенная из main"""
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler
    from config import settings
    from handlers import BotHandlers

    logger.info("🚀 Запуск Music Bot...")
    
    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        sys.exit(1)
    
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.error("❌ FFmpeg не найден!")
            sys.exit(1)
        logger.info("✅ FFmpeg доступен")
    except Exception as e:
        logger.error(f"❌ Ошибка проверки FFmpeg: {e}")
        sys.exit(1)
    
    app = Application.builder().token(settings.BOT_TOKEN).build()
    handlers = BotHandlers(app)
    
    commands = [
        ("start", handlers.start), ("menu", handlers.show_menu),
        ("play", handlers.handle_play), ("p", handlers.handle_play),
        ("audiobook", handlers.handle_audiobook), ("ab", handlers.handle_audiobook),
        ("radio", handlers.handle_radio),
        ("source", handlers.handle_source), ("src", handlers.handle_source),
        ("status", handlers.handle_status), ("stat", handlers.handle_status),
        ("help", handlers.handle_help),
    ]
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    app.add_handler(CallbackQueryHandler(handlers.handle_callback))
    
    logger.info("✅ Бот запускается...")
    await app.initialize()
    if app.updater:
        await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Бот успешно запущен!")
    
    # Ожидание сигнала завершения
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
        
    await stop_event.wait()
    
    logger.info("Завершение работы бота...")
    if app.updater:
        await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main():
    """Главная функция-обертка с блокировкой"""
    if not create_lock_file():
        logger.info("Экземпляр уже запущен. Этот процесс будет завершен.")
        sys.exit(1)
        
    try:
        asyncio.run(main_bot_logic())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в main: {e}", exc_info=True)
        sys.exit(1)
    finally:
        remove_lock_file()


if __name__ == "__main__":
    main()
