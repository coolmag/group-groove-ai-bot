#!/usr/bin/env bash

# A hack to prevent build failure if tput is not available in the Render environment.
# We check if the tput command exists. If not, we define a dummy tput function.
if ! command -v tput &> /dev/null
then
    echo "Warning: tput command not found. Defining a dummy tput function to proceed."
    tput() {
        # This is a shell no-op (does nothing, but exits successfully)
        return 0
    }
    export -f tput
fi

# --- The rest of the build script ---

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
