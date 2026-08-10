#!/bin/bash
echo "🚀 Stream Monitor — Setup"

# Create virtual environment
if [ ! -d "Stream_Venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv Stream_Venv
fi

# Activate and install dependencies
source Stream_Venv/bin/activate
pip install --upgrade pip
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"

chmod +x stream_manager.py

if ! command -v ffmpeg &> /dev/null; then
    echo ""
    echo "⚠️  ffmpeg was not found on PATH. Stream Monitor uses it for live"
    echo "    thumbnail previews and to record streams. Install it via your"
    echo "    package manager (e.g. apt install ffmpeg, pacman -S ffmpeg,"
    echo "    brew install ffmpeg)."
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "Just type ./stream_manager.py to run it — it launches itself"
echo "under Stream_Venv automatically, no terminal or typing needed."
