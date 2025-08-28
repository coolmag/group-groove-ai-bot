FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
# Устанавливаем зависимости, а затем выводим структуру проблемной библиотеки
RUN pip install --upgrade pip setuptools wheel && pip install -r requirements.txt && ls -R /usr/local/lib/python3.10/site-packages/pytgcalls
COPY . /app
CMD ["python", "main.py"]
