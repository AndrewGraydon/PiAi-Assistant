#!/usr/bin/env bash
# start.sh — Development/manual launcher for PiAi Assistant
#
# Starts the orchestrator only. Assumes the three NPU services are already
# running (either via systemd or started manually):
#   qwen3.service   → http://localhost:8000  (LLM)
#   whisper.service → http://localhost:8801  (ASR)
#   kokoro.service  → http://localhost:8803  (TTS)
#
# The display (LCD, RGB LED, button) is driven directly via WhisPlayBoard
# GPIO — no sidecar process required.
#
# For production use, install the systemd unit files from systemd/ instead.
# See docs/SETUP.md for full instructions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Validate .env ---
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[start.sh] ERROR: .env not found."
    echo "           Copy .env.template to .env and fill in any required secrets."
    exit 1
fi

# --- Load .env ---
set -a
# shellcheck source=/dev/null
source "$SCRIPT_DIR/.env"
set +a

# --- Set WM8960 speaker volume ---
CARD_INDEX=$(awk '/wm8960soundcard/ {print $1}' /proc/asound/cards 2>/dev/null | head -n1 || true)
if [ -n "$CARD_INDEX" ]; then
    echo "[start.sh] Setting speaker volume (card $CARD_INDEX)..."
    amixer -c "$CARD_INDEX" set Speaker 114 2>/dev/null || echo "[start.sh] Warning: amixer set failed (non-fatal)"
else
    echo "[start.sh] WARNING: WM8960 sound card not found — skipping volume setup"
fi

# --- Create runtime data directories ---
mkdir -p "$SCRIPT_DIR/data/recordings" \
         "$SCRIPT_DIR/data/tts" \
         "$SCRIPT_DIR/data/captures" \
         "$SCRIPT_DIR/data/logs"

# --- Cleanup handler ---
cleanup() {
    echo ""
    echo "[start.sh] Shutting down..."
    exit 0
}
trap cleanup SIGINT SIGTERM

# --- Select Python interpreter (prefer piAi conda env) ---
PYTHON="$HOME/miniforge3/envs/piAi/bin/python"
if [ ! -f "$PYTHON" ]; then
    echo "[start.sh] WARNING: piAi conda env not found at $PYTHON, falling back to python3"
    PYTHON=python3
fi

# --- Start orchestrator ---
# Health checks inside main.py wait up to 180s for NPU services to be ready.
echo "[start.sh] Starting PiAi Assistant orchestrator..."
"$PYTHON" "$SCRIPT_DIR/main.py" --config "$SCRIPT_DIR/config.yaml" "$@"

cleanup
