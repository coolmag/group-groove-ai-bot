FROM python:3.10
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel && pip install -r requirements.txt
COPY . /app
CMD ["ls", "-R", "/usr/local/lib/python3.10/site-packages/pytgcalls"]