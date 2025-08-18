FROM python:3.11-slim

# Устанавливаем переменные окружения
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Устанавливаем зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Создаем пользователя бота
RUN useradd -m -u 1001 radio_bot
USER radio_bot
WORKDIR /home/radio_bot/app

# Копируем зависимости и код
COPY --chown=radio_bot:radio_bot requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=radio_bot:radio_bot . .

# Создаем директорию для загрузок
RUN mkdir -p downloads

# Запуск бота
CMD ["python", "main.py"]
