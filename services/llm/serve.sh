#!/bin/bash
# serve.sh — Start Qwen3-4B LLM service
# Activates the qwen3 conda environment, starts the tokenizer server,
# then launches the main_api_axcl_aarch64 inference binary.
#
# Model files live in: ~/Qwen3-4B/ (not managed by this repo)
# Tokenizer port: 12300 (internal only)
# API port: 8000

set -e

PORT=12300
LLM_DIR="$HOME/Qwen3-4B"

# Validate model directory exists
if [ ! -d "$LLM_DIR" ]; then
    echo "ERROR: LLM directory not found at $LLM_DIR"
    exit 1
fi

cd "$LLM_DIR"

# Source conda
CONDA_PATH="$HOME/miniforge3/etc/profile.d/conda.sh"
if [ -f "$CONDA_PATH" ]; then
    source "$CONDA_PATH"
else
    echo "ERROR: Conda not found at $CONDA_PATH"
    exit 1
fi

conda activate qwen3

echo "Starting Qwen3 tokenizer server on port $PORT..."
python qwen3_tokenizer_uid.py --port $PORT &
TOKENIZER_PID=$!

# Wait for tokenizer to be ready
sleep 8

echo "Starting Qwen3-4B inference binary..."
./main_api_axcl_aarch64 \
    --template_filename_axmodel "qwen3-4b-ax650/qwen3_p128_l%d_together.axmodel" \
    --axmodel_num 28 \
    --url_tokenizer_model "http://127.0.0.1:$PORT" \
    --filename_post_axmodel qwen3-4b-ax650/qwen3_post.axmodel \
    --filename_tokens_embed qwen3-4b-ax650/model.embed_tokens.weight.bfloat16.bin \
    --tokens_embed_num 151936 \
    --tokens_embed_size 2560 \
    --use_mmap_load_embed 1 \
    --devices 0

# Clean up tokenizer when inference binary exits
kill $TOKENIZER_PID 2>/dev/null || true
pkill -f "python qwen3_tokenizer_uid.py --port $PORT" 2>/dev/null || true
