import asyncio
import os
import signal
import sys
import logging
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

# Настройка логов
logging.basicConfig(
    level=logging.DEBUG,  # Изменено на DEBUG для детальных логов
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
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
    
    # Логируем первые 10 символов токена для проверки
    logger.info(f"Токен бота: {settings.BOT_TOKEN[:10]}...")
    logger.info(f"ID админов: {settings.ADMIN_IDS}")
    
    # Проверка FFmpeg
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.error("❌ FFmpeg не найден!")
            sys.exit(1)
        logger.info("✅ FFmpeg доступен")
        logger.debug(f"FFmpeg версия: {result.stdout.split('version')[1].split()[0]}")
    except Exception as e:
        logger.error(f"❌ Ошибка проверки FFmpeg: {e}")
        sys.exit(1)
    
    # Проверка директории загрузок
    try:
        os.makedirs(settings.DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"✅ Директория загрузок: {settings.DOWNLOADS_DIR}")
    except Exception as e:
        logger.error(f"❌ Ошибка создания директории: {e}")
    
    try:
        # Создаем приложение бота с настройками таймаутов
        app = Application.builder() \
            .token(settings.BOT_TOKEN) \
            .connect_timeout(30.0) \
            .read_timeout(30.0) \
            .write_timeout(30.0) \
            .pool_timeout(30.0) \
            .build()
        
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
            ("test", handlers.test_command),  # Добавим тестовую команду
        ]
        
        for command, handler in commands:
            try:
                app.add_handler(CommandHandler(command, handler))
                logger.debug(f"✅ Зарегистрирована команда: /{command}")
            except Exception as e:
                logger.error(f"❌ Ошибка регистрации команды /{command}: {e}")
        
        # Регистрируем колбэки
        app.add_handler(CallbackQueryHandler(handlers.handle_callback))
        
        # Добавляем обработчик для всех сообщений (для дебага)
        async def debug_handler(update, context):
            logger.info(f"📨 Получено сообщение от {update.effective_user.id}: {update.message.text}")
            # Отвечаем на любое сообщение для проверки
            if update.message.text and not update.message.text.startswith('/'):
                await update.message.reply_text(f"🤖 Получил ваше сообщение: {update.message.text}")
        
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_handler))
        
        # Обработчик ошибок
        async def error_handler(update, context):
            logger.error(f"🔥 Ошибка в обработчике: {context.error}", exc_info=True)
            if update and update.effective_chat:
                try:
                    await update.effective_chat.send_message(f"⚠️ Произошла ошибка: {str(context.error)[:100]}")
                except:
                    pass
        
        app.add_error_handler(error_handler)
        
        # Запускаем бота
        logger.info("✅ Бот инициализируется...")
        await app.initialize()
        
        if app.updater:
            logger.info("🚀 Запуск polling...")
            await app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query", "chat_member"],
                poll_interval=1.0,
                timeout=30,
                bootstrap_retries=3,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
            )
        
        logger.info("🎉 Бот успешно запущен и слушает сообщения!")
        logger.info(f"📊 Источник по умолчанию: {handlers.state.source.value}")
        
        # Выводим информацию о боте
        bot_info = await app.bot.get_me()
        logger.info(f"🤖 Бот: @{bot_info.username} ({bot_info.first_name})")
        logger.info(f"🔗 Ссылка на бота: https://t.me/{bot_info.username}")
        
        # Периодическая проверка состояния
        async def health_check():
            while True:
                try:
                    await asyncio.sleep(60)
                    count = await app.bot.get_updates(limit=1, timeout=1)
                    logger.debug(f"❤️ Health check: бот жив, получено сообщений: {len(count)}")
                except Exception as e:
                    logger.warning(f"⚠️ Health check error: {e}")
        
        asyncio.create_task(health_check())
        
        # Ждем сигнала завершения
        stop_event = asyncio.Event()
        
        def signal_handler():
            logger.info("🛑 Получен сигнал завершения")
            stop_event.set()
        
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
        
        logger.info("⏳ Ожидание сообщений...")
        await stop_event.wait()
        
    except NetworkError as e:
        logger.error(f"🌐 Ошибка сети: {e}")
    except TimedOut as e:
        logger.error(f"⏱️ Таймаут: {e}")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
        raise
    finally:
        logger.info("🔄 Завершение работы бота...")
        try:
            if 'app' in locals():
                logger.info("⏹️ Останавливаеm polling...")
                if app.updater:
                    await app.updater.stop()
                logger.info("⏹️ Останавливаеm приложение...")
                await app.stop()
                await app.shutdown()
                logger.info("✅ Бот остановлен")
        except Exception as e:
            logger.error(f"⚠️ Ошибка при завершении: {e}")


if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("🎵 Music Bot запускается...")
    logger.info("=" * 50)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"💥 Непредвиденная ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("=" * 50)
        logger.info("🎵 Music Bot завершил работу")
        logger.info("=" * 50)
