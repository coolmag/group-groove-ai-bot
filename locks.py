import asyncio

# Лок для защиты состояния бота
state_lock = asyncio.Lock()

# Лок для предотвращения параллельных загрузок
download_lock = asyncio.Lock()

# Лок для предотвращения параллельного обновления радио
radio_update_lock = asyncio.Lock()