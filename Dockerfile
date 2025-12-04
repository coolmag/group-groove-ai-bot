# Stage 1: Builder
FROM python:3.11-slim as builder

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Установка зависимостей Python
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Установка только runtime зависимостей (FFmpeg)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Копирование Python зависимостей из builder
COPY --from=builder /root/.local /root/.local

# Копирование кода приложения
COPY . .

# Создание директории для загрузок
RUN mkdir -p /tmp/music_bot_downloads \
    && chmod 755 /tmp/music_bot_downloads

# Переменные окружения
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/root/.local/bin:$PATH \
    DOWNLOADS_DIR=/tmp/music_bot_downloads

# Создание и переключение на непривилегированного пользователя
RUN groupadd -r musicbot && useradd -r -g musicbot musicbot \
    && chown -R musicbot:musicbot /app \
    && chown -R musicbot:musicbot /tmp/music_bot_downloads

USER musicbot

# Запуск приложения
CMD ["python", "-u", "main.py"]