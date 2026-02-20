#!/usr/bin/env bash
# ============================================================
# PiAi Assistant — convenience launcher
# Starts the display sidecar then the orchestrator.
# For production use, prefer the systemd services.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Detect WM8960 sound card and set volume ---
CARD_INDEX=$(awk '/wm8960soundcard/ {print $1}' /proc/asound/cards 2>/dev/null | head -n1 || true)
CARD_INDEX="${CARD_INDEX:-1}"
echo "[start.sh] Using sound card index: $CARD_INDEX"
amixer -c "$CARD_INDEX" set Speaker 114 2>/dev/null || echo "[start.sh] Warning: amixer set failed (non-fatal)"

# --- Verify .env exists ---
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "[start.sh] ERROR: .env not found."
    echo "           Copy .env.template to .env and fill in any required secrets."
    exit 1
fi

# --- Load .env into shell environment ---
set -a
# shellcheck source=/dev/null
source "$SCRIPT_DIR/.env"
set +a

# --- Locate the display sidecar ---
# Expects the whisplay-ai-chatbot-llm8850 repo to be a sibling directory
DISPLAY_REPO="$(dirname "$SCRIPT_DIR")/whisplay-ai-chatbot-llm8850/python"
if [ ! -f "$DISPLAY_REPO/chatbot-ui.py" ]; then
    echo "[start.sh] WARNING: Display sidecar not found at $DISPLAY_REPO/chatbot-ui.py"
    echo "           Running without display. Clone whisplay-ai-chatbot-llm8850 as a sibling directory."
    DISPLAY_PID=""
else
    echo "[start.sh] Starting display sidecar..."
    CUSTOM_FONT_PATH="$DISPLAY_REPO/NotoSansSC-Bold.ttf" \
        python3 "$DISPLAY_REPO/chatbot-ui.py" &
    DISPLAY_PID=$!
    echo "[start.sh] Display sidecar PID: $DISPLAY_PID"
    # Give the socket time to bind
    sleep 3
fi

# --- Cleanup function ---
cleanup() {
    echo ""
    echo "[start.sh] Shutting down..."
    if [ -n "${DISPLAY_PID:-}" ] && kill -0 "$DISPLAY_PID" 2>/dev/null; then
        kill "$DISPLAY_PID"
    fi
    exit 0
}
trap cleanup SIGINT SIGTERM

# --- Start orchestrator ---
# Health checks inside main.py wait up to 180s for NPU services.
echo "[start.sh] Starting orchestrator..."
python3 "$SCRIPT_DIR/main.py" --config "$SCRIPT_DIR/config.yaml" "$@"

cleanup
