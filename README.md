# Groove AI Bot v5

Музыкальный Telegram-бот-радио с поиском треков, голосованием за жанры и отправкой треков в чат.

## Возможности
- Радио-режим: бот отправляет трек **раз в минуту** по выбранному жанру
- Поиск `/play <название>`: показывает до 10 результатов, выбор через инлайн-кнопки
- Голосование `/vote`: каждую **полночь** стартует авто-голосование; можно запускать вручную
- Источники: **Last.fm (поиск по жанрам)** → **YouTube** → **SoundCloud** (через `yt-dlp`)
- Fallback: если жанр пустой — переключится на близкие теги из преднастроенного списка
- Красивые кнопки статуса и меню
- Анти-повторы (история последних 20 треков)

## Запуск

### 1) Настрой переменные окружения
Создай `.env` (или используй переменные Railway/Render/Docker):

```
TELEGRAM_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-1001234567890        # основной чат (опционально)
ADMINS=12345678,987654321              # user_id админов через запятую

LASTFM_API_KEY=50b0f5049406e6e6e38829e527da390b

# cookies опционально
YOUTUBE_COOKIES_PATH=/app/youtube_cookies.txt
SOUNDCLOUD_COOKIES_PATH=/app/soundcloud_cookies.txt

# Proxy (опционально)
PROXY_ENABLED=false
PROXY_URL=http://user:pass@host:port
```

### 2) Docker
```
docker build -t groove-ai-bot:v5 .
docker run --env-file .env -v $(pwd)/downloads:/app/downloads groove-ai-bot:v5
```

### 3) Railway
- Создай новый проект → Deploy from repo
- Variables: добавь переменные из секции выше
- Убедись, что **ffmpeg** установлен (в Dockerfile уже есть)

## Команды
- `/start` — приветствие + статус
- `/menu` — меню с кнопками
- `/play <название>` или `/p <название>` — поиск
- `/vote` — запустить голосование
- `/ron` / `/roff` — включить/выключить радио (админ)
- `/source <youtube|soundcloud>` — смена приоритета источников (админ)
- `/skip` — следующий трек (админ)

## Примечания
- Для корректности `JobQueue` убедись, что установлено: `pip install "python-telegram-bot[job-queue]"` (в requirements включено).
- Если `ffmpeg` не найден — Dockerfile уже устанавливает.
- Для YouTube регион-поиск может зависеть от куков — можно положить файл `youtube_cookies.txt` в корень.
