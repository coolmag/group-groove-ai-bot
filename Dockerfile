# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies, including ffmpeg and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Update yt-dlp to the latest version
RUN pip install -U yt-dlp

# Create downloads directory with permissions
RUN mkdir -p /app/downloads && chmod -R 777 /app/downloads

# Copy the rest of the application code into the container
COPY . .

# Command to run the application
CMD ["python3", "main.py"]
