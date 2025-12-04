FROM python:3.11-slim

WORKDIR /app

# Установка FFmpeg и очистка кэша в одном слое
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Копирование зависимостей отдельным слоем для кэширования
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Копирование остального кода
COPY . .

# Создание директории для загрузок с правильными правами
RUN mkdir -p /tmp/music_bot_downloads \
    && chmod 755 /tmp/music_bot_downloads

# Переменные окружения
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNLOADS_DIR=/tmp/music_bot_downloads

# Запуск от непривилегированного пользователя
USER nobody

CMD ["python", "-u", "main.py"]
