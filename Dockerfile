# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set UTF-8 encoding to prevent character errors
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

# Set the working directory in the container
WORKDIR /app

# Install system dependencies, including ffmpeg and curl
# We run apt-get update and install in one command to reduce image size
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Force rebuild by adding a changing label
LABEL last_build_date="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"


# Copy the application code into the container
COPY *.py .
COPY requirements.txt .

# Copy credentials if they exist
COPY *.txt .

# Command to run the application
CMD ["python3", "main.py"]