FROM python:3.11-slim

# 1. Устанавливаем все системные зависимости (ffmpeg для yt-dlp, wget/gnupg для Chrome, и библиотеки для headless-режима)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Зависимость для yt-dlp
    ffmpeg \
    # Зависимости для установки Chrome
    wget \
    gnupg \
    unzip \
    # Зависимости для работы Chrome/Selenium в headless-режиме
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgdk-pixbuf2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# 2. Добавляем репозиторий Google Chrome
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list

# 3. Устанавливаем Google Chrome
RUN apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# 4. Устанавливаем Chromedriver, соответствующий версии установленного Chrome
RUN CHROME_VERSION=$(google-chrome --product-version | grep -o "^[^.]*") && \
    CHROMEDRIVER_VERSION=$(wget -qO- "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION}") && \
    wget -q --continue -P /tmp "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip" && \
    unzip /tmp/chromedriver_linux64.zip -d /usr/local/bin && \
    rm /tmp/chromedriver_linux64.zip

# 5. Устанавливаем рабочую директорию
WORKDIR /app

# 6. Копируем и устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 7. Копируем остальной код проекта
COPY . .

# 8. Создаем директорию для загрузок
RUN mkdir -p downloads && chmod 777 downloads

# 9. Запускаем приложение
CMD ["python", "-u", "main.py"]
