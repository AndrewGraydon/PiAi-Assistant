#!/bin/bash
# serve.sh — Start Kokoro TTS service
# Activates the kokoro conda environment and starts the TTS server.
#
# Model files live in: ~/kokoro.LM8850/ (not managed by this repo)
# API port: 8803

set -e

TTS_DIR="$HOME/kokoro.LM8850"

if [ ! -d "$TTS_DIR" ]; then
    echo "ERROR: kokoro.LM8850 directory not found at $TTS_DIR"
    exit 1
fi

cd "$TTS_DIR"

# Source conda
CONDA_PATH="$HOME/miniforge3/etc/profile.d/conda.sh"
if [ -f "$CONDA_PATH" ]; then
    source "$CONDA_PATH"
else
    echo "ERROR: Conda not found at $CONDA_PATH"
    exit 1
fi

conda activate kokoro

echo "Starting Kokoro TTS server on port 8803..."
python kokoro_svr.py --port 8803
