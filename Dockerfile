# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем зависимости, чтобы избежать переустановки при каждом изменении кода
RUN pip install --no-cache-dir --upgrade pip

# Копируем только файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# Указываем команду для запуска приложения
CMD ["python", "main.py"]
