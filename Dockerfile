# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем переменные окружения
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV DEBIAN_FRONTEND noninteractive

# Устанавливаем системные зависимости и чистим кэш
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    iproute2 \
    dnsutils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Создаем директорию приложения
WORKDIR /app

# Устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Создаем директорию для загрузок и устанавливаем права
RUN mkdir -p downloads \
    && chmod 777 downloads

# Команда запуска приложения
CMD ["python", "main.py"]
