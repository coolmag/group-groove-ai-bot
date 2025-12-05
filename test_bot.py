#!/usr/bin/env python3
import logging
from telegram.ext import Application, CommandHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def start(update, context):
    await update.message.reply_text("✅ Тестовый бот работает!")
    print(f"✅ Обработана команда /start от {update.effective_user.id}")

async def echo(update, context):
    await update.message.reply_text(f"Вы сказали: {update.message.text}")
    print(f"📨 Сообщение: {update.message.text}")

def main():
    # 🔴 ЗАМЕНИТЕ НА ВАШ НОВЫЙ ТОКЕН
    TOKEN = "7561017292:AAHRXuHLzFprGVs4Ytfc5KWglCbgNNgl22o"
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    
    print("🚀 Запуск тестового бота...")
    print("📝 Напишите /start боту в Telegram")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
