FROM python:3.11-slim

# Устанавливаем системные зависимости, включая FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем и устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем остальной код проекта
COPY . .

# Создаем директорию для загрузок
RUN mkdir -p downloads && chmod 777 downloads

# Запускаем приложение
CMD ["python", "-u", "main.py"]