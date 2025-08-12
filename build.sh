#!/usr/bin/env bash
# exit on error
set -o errexit

echo "----> Installing Python dependencies..."
pip install -r requirements.txt

echo "----> Installing FFmpeg..."
# The directory for FFmpeg binary, relative to the project root
FFMPEG_DIR="./.ffmpeg"

# Create directories
mkdir -p "$FFMPEG_DIR/bin"

# URL for a static amd64 build of FFmpeg from johnvansickle.com
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

# Download and extract the binary into the .ffmpeg/bin directory
curl -sL "$FFMPEG_URL" | tar -xJ --strip-components=1 -C "$FFMPEG_DIR/bin"

# Add the directory to the PATH for subsequent commands in this build script
export PATH="$PWD/$FFMPEG_DIR/bin:$PATH"

# Verify installation by printing the version
ffmpeg -version

echo "FFmpeg installation complete."
