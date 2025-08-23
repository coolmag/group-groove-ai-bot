FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends     ffmpeg curl ca-certificates  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy deps first
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m yt_dlp --version

# App
COPY . .

# Ensure downloads dir
RUN mkdir -p downloads

ENV PYTHONUNBUFFERED=1     TZ=Europe/Amsterdam

CMD ["python", "-u", "main.py"]
