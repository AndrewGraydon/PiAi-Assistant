#!/bin/bash
# serve.sh — Start Whisper ASR service
# Runs the Flask server that wraps the whisper C++ binary.
#
# Binary and model files live in: ~/whisper.axcl/ and ~/whisper-small-axmodel/
# API port: 8801 (set via WHISPER_PORT env var)

set -e

ASR_DIR="$HOME/whisper.axcl"

if [ ! -d "$ASR_DIR" ]; then
    echo "ERROR: whisper.axcl directory not found at $ASR_DIR"
    exit 1
fi

cd "$ASR_DIR"

echo "Starting Whisper ASR server on port 8801..."
WHISPER_PORT=8801 python server/main.py
