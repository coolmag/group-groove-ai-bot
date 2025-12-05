FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей ВКЛЮЧАЯ компилятор
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    git \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Создаем папку для загрузок
RUN mkdir -p downloads

# Запускаем бота
CMD ["python", "main.py"]
