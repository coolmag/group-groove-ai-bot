FROM python:3.10
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
# Устанавливаем зависимости, а затем СРАЗУ ЖЕ выводим структуру проблемной библиотеки в лог сборки
RUN pip install --upgrade pip setuptools wheel && pip install -r requirements.txt && ls -R /usr/local/lib/python3.10/site-packages/pytgcalls
COPY . /app
# Возвращаем команду для запуска бота
CMD ["python", "main.py"]
