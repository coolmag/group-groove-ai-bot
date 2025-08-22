import asyncio

# Лок для защиты состояния бота (BotState)
state_lock = asyncio.Lock()

# Лок для управления радио-задачами, чтобы избежать одновременного запуска
radio_lock = asyncio.Lock()

# Лок для управления доступом к yt-dlp, чтобы избежать параллельных загрузок
download_lock = asyncio.Lock()