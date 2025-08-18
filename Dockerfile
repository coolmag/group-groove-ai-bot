FROM python:3.11-slim

# Install FFmpeg and dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Update pip to ensure proper dependency installation
RUN pip install --no-cache-dir --upgrade pip

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip show pydantic | grep Version

# Copy the rest of the application
COPY . .

# Command to run the bot
CMD ["python", "main.py"]
