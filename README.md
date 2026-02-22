# PiAi Assistant

A voice-first, mostly-offline AI assistant running on a **Raspberry Pi 5** with a **M5Stack LLM8850** NPU accelerator. All speech recognition, language model inference, and text-to-speech run locally on the NPU. Vision queries fall back to a configurable cloud provider.

---

## Hardware

| Component | Details |
|---|---|
| Host | Raspberry Pi 5 (8GB RAM), hostname `aiserver` |
| Accelerator | M5Stack LLM8850 (M.2 2242, 8GB VRAM) via dual M.2 HAT |
| Display / Audio | Whisplay HAT — 240×280 LCD, RGB LED, physical button, WM8960 audio codec (card 2) |
| Camera | Freenove FNK0085 CSI camera module |
| Power | 5V/3A non-PD adapter (PD adapters cause instability under NPU load) |

---

## Features

- **100% local voice pipeline** — Whisper ASR → Qwen3-4B LLM → Kokoro TTS, all on the NPU
- **Button-triggered** — press the Whisplay button to speak; press again mid-response to interrupt
- **Automatic silence detection** — webrtcvad stops recording when you finish speaking
- **RAG memory** — ChromaDB + sentence-transformers stores and retrieves conversation context
- **Tool calling** — LLM can invoke tools via structured `<tool_call>` blocks
- **n8n integration** — add new tools as n8n webhook workflows without changing code
- **Vision on demand** — camera captures on tool call; cloud vision provider (placeholder → Gemini/OpenAI)
- **In-process display driver** — WhisPlayBoard drives LCD/LED/button directly via GPIO/SPI (no sidecar)
- **Fully configurable** — `config.yaml` for structure, `.env` for secrets

---

## NPU Services & VRAM Budget

All AI models run on the LLM8850's 8GB VRAM:

| Service | Model | VRAM | Port |
|---|---|---|---|
| LLM | Qwen3-4B (w8a16 int8) | ~6.3 GB | 8000 |
| ASR | Whisper-Small | ~0.5 GB | 8801 |
| TTS | Kokoro-82M | ~0.3 GB | 8803 |
| Display | WhisPlayBoard (in-process GPIO/SPI) | CPU only | — |

> Vision (InternVL) is not resident — vision queries use a cloud provider to stay within the 8GB budget.

---

## Project Structure

```
PiAi-Assistant/
├── config.yaml              # Service topology, model settings, UI theme
├── .env.template            # API key template — copy to .env
├── .env                     # Secrets (gitignored)
├── requirements.txt
├── start.sh                 # Convenience launcher (manual/dev)
├── main.py                  # Entry point
├── services/                # NPU service launchers + display driver
│   ├── llm/serve.sh         # Starts tokenizer + main_api_axcl_aarch64
│   ├── asr/serve.sh         # Starts Whisper Flask server
│   ├── tts/serve.sh         # Starts Kokoro TTS server
│   └── display/WhisPlay.py  # Whisplay HAT driver (LCD, LED, button)
├── systemd/                 # Systemd unit files for production
│   ├── piAi-llm.service
│   ├── piAi-asr.service
│   ├── piAi-tts.service
│   └── piAi-orchestrator.service
├── docs/
│   ├── ARCHITECTURE.md      # Component and service architecture
│   ├── DATA_FLOW.md         # Pipeline data flows with sequence diagrams
│   └── SETUP.md             # Installation and deployment guide
└── src/
    ├── config.py            # Typed config dataclasses
    ├── orchestrator.py      # State machine + pipeline loop
    ├── services/            # HTTP clients (LLM, ASR, TTS, vision) + display driver wrapper
    ├── audio/               # Recorder (VAD) + player (aplay)
    ├── memory/              # ChromaDB store + sentence-transformers embedder
    ├── tools/               # Tool registry, camera, home stub, n8n client
    └── utils/               # VAD wrapper, service health checks
```

---

## Quick Start

### 1. Prerequisites

```bash
# System packages (Pi OS Bookworm)
sudo apt install python3-picamera2 python3-dev libportaudio2 alsa-utils fonts-noto-core fonts-dejavu-core

# Create the piAi conda environment
conda create -n piAi python=3.11 -y

# Install Python dependencies into piAi env
~/miniforge3/envs/piAi/bin/pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.template .env
# Edit .env — add API keys if using cloud vision or n8n
# Review config.yaml — adjust voice, thresholds, or assistant name
```

### 3. Start NPU services (or use systemd)

The NPU services are managed by systemd and start automatically on boot. For a manual run:

```bash
# LLM (Qwen3-4B) — takes ~133s to initialise
cd ~/Qwen3-4B
python3 qwen3_tokenizer_uid.py --port 12300 &
sleep 8
./main_api_axcl_aarch64 \
  --system_prompt "Reply in English. Be concise. /no_think" \
  --url_tokenizer_model http://127.0.0.1:12300 \
  --template_filename_axmodel "qwen3-4b-ax650/qwen3_p128_l%d_together.axmodel" \
  --axmodel_num 36 --filename_post_axmodel qwen3-4b-ax650/qwen3_post.axmodel \
  --filename_tokens_embed qwen3-4b-ax650/model.embed_tokens.weight.bfloat16.bin \
  --tokens_embed_num 151936 --tokens_embed_size 2560 \
  --use_mmap_load_embed 1 --devices 0 &

# ASR (Whisper)
cd ~/whisper.axcl && python3 server/main.py &

# TTS (Kokoro)
cd ~/kokoro.LM8850 && python3 kokoro_svr.py --port 8803 &
```

### 4. Run

```bash
cd ~/PiAi-Assistant
./start.sh
```

The assistant will wait up to 180 seconds for NPU services to be ready, then display the idle screen. **Press the Whisplay button to speak.**

---

## Configuration

All structural configuration lives in `config.yaml`. Secrets (API keys) live in `.env`.

### Key settings

```yaml
services:
  llm:
    host: http://localhost:8000
    temperature: 0.7
    enable_thinking: false   # Set true to enable Qwen3 chain-of-thought

  vision:
    provider: placeholder    # Change to: gemini | openai | anthropic

n8n:
  enabled: false             # Set true to enable n8n workflow tools
  url: http://localhost:5678
  token: ""                  # Or set N8N_TOKEN in .env

memory:
  enabled: true
  top_k: 3                   # Number of memory chunks to retrieve per query

assistant:
  name: Jarvis               # Assistant name shown on display
```

See `config.yaml` for the full reference with all options documented.

---

## Adding Tools via n8n

When `n8n.enabled: true`, the assistant automatically discovers active n8n workflows that have a webhook trigger. Each workflow becomes a callable tool.

**To add a new tool:**
1. Create a workflow in n8n with a **Webhook Trigger** node
2. Add a description in the webhook node's **Notes** field — this becomes the tool description the LLM sees
3. End the workflow with an HTTP Response node returning:
   ```json
   {
     "message": "Spoken response for the user",
     "data": {},
     "memory_hint": { "key": "value" }
   }
   ```
4. Activate the workflow — it will be discovered on next assistant startup

No code changes required.

---

## Production Deployment (systemd)

```bash
# Copy unit files
sudo cp ~/PiAi-Assistant/systemd/piAi-*.service /etc/systemd/system/

# Enable services (start on boot)
sudo systemctl daemon-reload
sudo systemctl enable piAi-llm piAi-asr piAi-tts piAi-orchestrator

# Start now
sudo systemctl start piAi-llm piAi-asr piAi-tts
# Wait ~2 minutes, then:
sudo systemctl start piAi-orchestrator

# View logs
journalctl -u piAi-orchestrator -f
journalctl -u piAi-llm piAi-asr piAi-tts piAi-orchestrator -f
```

---

## Monitoring

```bash
# NPU temperature, memory, and utilisation
axcl-smi

# Service status
sudo systemctl status piAi-llm piAi-asr piAi-tts piAi-orchestrator

# Service logs (live)
journalctl -u piAi-orchestrator -f

# Application log file
tail -f ~/PiAi-Assistant/data/logs/assistant.log
```

> The LLM8850 can reach 70°C under full load. Ensure the active cooling fan is running.

---

## Docs

- [Architecture](docs/ARCHITECTURE.md) — component overview, service topology, design decisions
- [Data Flow](docs/DATA_FLOW.md) — sequence diagrams for voice, vision, and n8n tool pipelines
- [Setup Guide](docs/SETUP.md) — full hardware and software installation walkthrough

---

## Acknowledgements

- [Whisplay](https://github.com/m5stack/Whisplay) — Whisplay HAT hardware driver (WhisPlay.py)
- [CAAL](https://github.com/caal-project/caal) — n8n tool pattern, ToolDataCache, and memory_hint design
- [whisper.axcl](https://github.com/PiSugar/whisper.axcl) — Whisper ASR on LLM8850
- [kokoro.LM8850](https://github.com/m5stack/kokoro.LM8850) — Kokoro TTS on LLM8850
- [Qwen3](https://github.com/QwenLM/Qwen3) — base language model
