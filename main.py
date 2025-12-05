#!/usr/bin/env python3
import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from config import settings
from handlers import BotHandlers

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def debug_message(update: Update, context):
    """Отладочный обработчик всех сообщений"""
    user = update.effective_user
    chat = update.effective_chat
    text = update.message.text if update.message else "No text"
    
    logger.info(f"📨 Получено сообщение от {user.id} ( @{user.username}) в чате {chat.id}: {text}")
    
    if text.startswith('/'):
        await update.message.reply_text(f"✅ Получена команда: {text}")
    else:
        await update.message.reply_text(f"📝 Вы написали: {text}")


async def main():
    """Основная функция запуска бота"""
    logger.info("🚀 Запуск Music Bot v2.0 с диагностикой...")
    
    # Проверка обязательных переменных
    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        sys.exit(1)
    
    if not settings.ADMIN_IDS:
        logger.warning("⚠️ ADMIN_IDS не установлен!")
    
    logger.info(f"📊 Настройки: Admin IDs: {settings.ADMIN_IDS}, Source: YouTube")
    
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
        # Создание приложения
        app = Application.builder().token(settings.BOT_TOKEN).build()
        handlers = BotHandlers(app)
        
        # ДОБАВИТЬ ОТЛАДОЧНЫЙ ОБРАБОТЧИК
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_message))
        
        # Регистрация обработчиков
        await handlers.register_handlers(app)
        
        # Запуск бота
        logger.info("✅ Бот запускается...")
        logger.info(f"✅ Токен: {settings.BOT_TOKEN[:10]}...")
        
        await app.initialize()
        
        # Запуск polling с подробными параметрами
        logger.info("🔄 Запуск polling...")
        
        if app.updater:
            await app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
                poll_interval=0.5,
                timeout=10
            )
        
        logger.info("✅ Бот успешно запущен и ожидает сообщений...")
        logger.info("📝 Отправьте /start боту в личные сообщения!")
        
        # Бесконечное ожидание
        await asyncio.Event().wait()
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка: {e}", exc_info=True)
        sys.exit(1)
