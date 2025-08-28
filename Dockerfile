FROM python:3.10
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

# Создаем директорию для загрузок, куда yt-dlp будет сохранять файлы
RUN mkdir -p downloads && chmod 777 downloads

CMD ["python", "-u", "main.py"]