FROM python:3.11-slim

# Устанавливаем системные зависимости включая FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Создаем директорию для загрузок
RUN mkdir -p downloads && chmod 777 downloads

# Запускаем приложение
CMD ["python", "-u", "main.py"]