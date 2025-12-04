import asyncio

# Блокировки для предотвращения гонок данных
download_lock = asyncio.Lock()
state_lock = asyncio.Lock()
radio_update_lock = asyncio.Lock()
