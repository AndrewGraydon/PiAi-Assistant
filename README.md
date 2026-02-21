# PiAi Assistant

A voice-first, mostly-offline AI assistant running on a **Raspberry Pi 5** with a **M5Stack LLM8850** NPU accelerator. All speech recognition, language model inference, and text-to-speech run locally on the NPU. Vision queries fall back to a configurable cloud provider.

---

## Hardware

| Component | Details |
|---|---|
| Host | Raspberry Pi 5 (8GB RAM), Ubuntu 22.04 / Debian 12 |
| Accelerator | M5Stack LLM8850 (M.2 2242, 8GB VRAM) via dual M.2 HAT |
| Display / Audio | Whisplay HAT — 240×280 LCD, RGB LED, physical button, WM8960 audio codec |
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
- **Fully configurable** — `config.yaml` for structure, `.env` for secrets

---

## NPU Services & VRAM Budget

All AI models run on the LLM8850's 8GB VRAM:

| Service | Model | VRAM | Port |
|---|---|---|---|
| LLM | Qwen3-4B (w8a16 int8) | ~6.3 GB | 8000 |
| ASR | Whisper-Small | ~0.5 GB | 8801 |
| TTS | Kokoro-82M | ~0.3 GB | 8803 |
| Display sidecar | chatbot-ui.py | CPU only | 12345 (TCP) |

> Vision (InternVL) is not resident — vision queries use a cloud provider to stay within the 8GB budget.

---

## Project Structure

```
assistant/
├── config.yaml              # Service topology, model settings, UI theme
├── .env.template            # API key template — copy to .env
├── .env                     # Secrets (gitignored)
├── requirements.txt
├── start.sh                 # Convenience launcher
├── main.py                  # Entry point
├── systemd/                 # Systemd unit files for production
│   ├── assistant-llm.service
│   ├── assistant-asr.service
│   ├── assistant-tts.service
│   ├── assistant-display.service
│   └── assistant-orchestrator.service
├── docs/
│   ├── ARCHITECTURE.md      # Component and service architecture
│   ├── DATA_FLOW.md         # Pipeline data flows with sequence diagrams
│   └── SETUP.md             # Installation and deployment guide
└── src/
    ├── config.py            # Typed config dataclasses
    ├── orchestrator.py      # State machine + pipeline loop
    ├── services/            # HTTP clients (LLM, ASR, TTS, vision, display)
    ├── audio/               # Recorder (VAD) + player (aplay)
    ├── memory/              # ChromaDB store + sentence-transformers embedder
    ├── tools/               # Tool registry, camera, home stub, n8n client
    └── utils/               # VAD wrapper, service health checks
```

---

## Quick Start

### 1. Prerequisites

```bash
# System packages (Pi OS / Ubuntu)
sudo apt install python3-picamera2 python3-dev libportaudio2 alsa-utils

# Python dependencies
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.template .env
# Edit .env — add API keys if using cloud vision
# Edit config.yaml — adjust ports, voice, thresholds as needed
```

### 3. Start NPU services

Start the LLM, ASR, and TTS services (see `systemd/` for production setup):

```bash
# LLM (Qwen3-4B) — takes ~133s to initialise
cd ~/PiAi/Qwen3-4B
python3 qwen3_tokenizer_uid.py --port 12300 &
sleep 8
./main_api_axcl_aarch64 --url_tokenizer_model http://127.0.0.1:12300 \
  --template_filename_axmodel "qwen3-4b-ax650/qwen3_p128_l%d_together.axmodel" \
  --axmodel_num 28 --filename_post_axmodel qwen3-4b-ax650/qwen3_post.axmodel \
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
cd ~/PiAi/assistant
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

A:
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
sudo cp systemd/*.service /etc/systemd/system/

# Edit paths in each unit file (replace /home/pi/PiAi with your actual paths)
sudo nano /etc/systemd/system/assistant-llm.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable assistant-llm assistant-asr assistant-tts \
                       assistant-display assistant-orchestrator
sudo systemctl start assistant-llm assistant-asr assistant-tts \
                      assistant-display assistant-orchestrator

# View logs
journalctl -u assistant-orchestrator -f
```

---

## Monitoring

```bash
# NPU temperature, memory, and utilisation
axcl-smi

# Service logs
journalctl -u assistant-orchestrator -f
journalctl -u assistant-llm -f

# Assistant logs
tail -f data/logs/assistant.log
```

> The LLM8850 can reach 70°C under full load. Ensure the active cooling fan is running.

---

## Docs

- [Architecture](docs/ARCHITECTURE.md) — component overview, service topology, design decisions
- [Data Flow](docs/DATA_FLOW.md) — sequence diagrams for voice, vision, and n8n tool pipelines
- [Setup Guide](docs/SETUP.md) — full hardware and software installation walkthrough

---

## Acknowledgements

- [whisplay-ai-chatbot-llm8850](https://github.com/m5stack/whisplay-ai-chatbot-llm8850) — reference chatbot and display sidecar
- [CAAL](https://github.com/caal-project/caal) — n8n tool pattern, ToolDataCache, and memory_hint design
- [whisper.axcl](https://github.com/PiSugar/whisper.axcl) — Whisper ASR on LLM8850
- [kokoro.LM8850](https://github.com/m5stack/kokoro.LM8850) — Kokoro TTS on LLM8850
- [Qwen3](https://github.com/QwenLM/Qwen3) — base language model
