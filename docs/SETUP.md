# Setup Guide

Complete hardware and software installation walkthrough for PiAi Assistant on Raspberry Pi 5 + M5Stack LLM8850.

---

## Table of Contents

1. [Hardware Assembly](#1-hardware-assembly)
2. [Operating System](#2-operating-system)
3. [M5Stack LLM8850 Setup](#3-m5stack-llm8850-setup)
4. [Clone Reference Repositories](#4-clone-reference-repositories)
5. [NPU Model Files](#5-npu-model-files)
6. [Clone and Configure PiAi Assistant](#6-clone-and-configure-piai-assistant)
7. [Python Dependencies](#7-python-dependencies)
8. [Pre-Download ML Models](#8-pre-download-ml-models)
9. [Audio Verification](#9-audio-verification)
10. [First Run (Manual)](#10-first-run-manual)
11. [Production Setup (systemd)](#11-production-setup-systemd)
12. [Adding Cloud Vision](#12-adding-cloud-vision)
13. [Adding n8n Tools](#13-adding-n8n-tools)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Hardware Assembly

### Components

| Component | Part | Notes |
|---|---|---|
| SBC | Raspberry Pi 5 (8GB) | 4GB works but limits headroom |
| Accelerator | M5Stack LLM8850 (M.2 2242) | The NPU — carries all AI models |
| HAT | M5Stack Whisplay HAT | LCD, RGB LED, button, WM8960 audio |
| Camera | Freenove FNK0085 | CSI ribbon cable, 5MP |
| Storage | 32GB+ microSD (A2 rated) | Or USB SSD for faster model load |
| Power | 5V/3A non-PD USB-C adapter | **Non-PD only** — PD adapters cause instability under NPU load |

### Assembly Order

1. **Fit the M5Stack Dual M.2 HAT** to the Raspberry Pi 5 GPIO header
2. **Seat the LLM8850** in the M.2 2242 slot; secure the retention screw
3. **Stack the Whisplay HAT** on top of the dual M.2 HAT (it passes through the GPIO)
4. **Connect the camera** ribbon cable to the Pi's CSI port (camera port 0 — the one closest to the USB ports)
5. **Connect a speaker** to the 3.5mm audio out on the Whisplay HAT (or use the 3W speaker pads with a compatible speaker)

> **Cooling**: The LLM8850 reaches 70°C under full inference load. Ensure the active cooling fan on the dual M.2 HAT is connected and spinning. Passive cooling alone is insufficient.

---

## 2. Operating System

### Recommended: Raspberry Pi OS (64-bit, Bookworm)

```bash
# Flash with Raspberry Pi Imager
# Choose: Raspberry Pi OS (64-bit) — "Bookworm" (Debian 12)
# Enable SSH, set hostname, username, and WiFi in Imager settings before flashing
```

> Ubuntu Server 22.04 (64-bit) also works. The main difference is that `picamera2` requires `sudo apt install python3-picamera2` on both.

### First boot — update and enable interfaces

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Enable camera, SPI, I2C
sudo raspi-config
# → Interface Options → Camera → Enable
# → Interface Options → SPI → Enable
# → Interface Options → I2C → Enable

# Reboot
sudo reboot
```

### Verify camera

```bash
# Should list the camera
libcamera-hello --list-cameras
```

---

## 3. M5Stack LLM8850 Setup

The LLM8850 requires the AXCL runtime drivers. Follow M5Stack's official installation guide for your OS. Key steps:

### Install AXCL runtime

```bash
# Download the AXCL SDK package from M5Stack/AX-Samples releases
# (check M5Stack LLM8850 documentation for the current URL)
sudo dpkg -i axcl_runtime_<version>_arm64.deb

# Verify the NPU is detected
axcl-smi
```

Expected output from `axcl-smi`:
```
+---------------------------+
| AXCL-SMI  <version>       |
+---------------------------+
| Device 0: LLM8850         |
| Temperature: 45°C         |
| Memory: 0 / 8192 MiB used |
+---------------------------+
```

### Install AXCL Python bindings (if required by any NPU services)

```bash
pip install axcl
```

---

## 4. Clone Reference Repositories

All AI service repositories are cloned directly to `~/`. There is no `~/PiAi/` parent directory — each repo lives at the top level of your home directory.

### Whisper ASR

```bash
git clone https://github.com/PiSugar/whisper.axcl.git ~/whisper.axcl
```

### Kokoro TTS

```bash
git clone https://github.com/m5stack/kokoro.LM8850.git ~/kokoro.LM8850
```

### Whisplay HAT driver (reference)

```bash
git clone https://github.com/m5stack/Whisplay.git ~/Whisplay
```

### PiAi Assistant (this repo)

```bash
git clone https://github.com/AndrewGraydon/PiAi-Assistant.git ~/PiAi-Assistant
```

Your directory structure should now look like:

```
~/
├── PiAi-Assistant/                   ← this repo (already exists on the Pi)
├── Qwen3-4B/                         ← next step (LLM binary + model shards)
├── whisper.axcl/                     ← Whisper C++ binary + Flask server
├── whisper-small-axmodel/            ← Whisper axmodel files (separate directory)
├── kokoro.LM8850/                    ← Kokoro TTS server + model files
└── Whisplay/                         ← Whisplay HAT driver (reference only)
```

> `~/whisper-small-axmodel/` is a separate directory from `~/whisper.axcl/`. The axmodel files are not inside the whisper.axcl repo.

---

## 5. NPU Model Files

### Qwen3-4B (LLM)

The Qwen3-4B w8a16 int8 quantised axmodel is not in a public git repo — download it from M5Stack's model releases or the AX-Samples repository.

```bash
mkdir -p ~/Qwen3-4B
cd ~/Qwen3-4B

# Download the model archive (check M5Stack documentation for current URL)
# The archive should contain:
#   qwen3-4b-ax650/               ← directory with 36 axmodel shards + post model + embed weights
#   qwen3_tokenizer_uid.py        ← tokenizer server
#   main_api_axcl_aarch64         ← inference binary (note: "api" in the name)
```

Verify the expected files are present:

```bash
ls ~/Qwen3-4B/qwen3-4b-ax650/
# Expected: qwen3_p128_l0_together.axmodel ... qwen3_p128_l35_together.axmodel  (36 shards)
#           qwen3_post.axmodel
#           model.embed_tokens.weight.bfloat16.bin
```

Make the inference binary executable:

```bash
chmod +x ~/Qwen3-4B/main_api_axcl_aarch64
```

### Whisper ASR models

The Whisper axmodel files live in a **separate** directory from the `whisper.axcl` repo. On this Pi they are already present in `~/whisper-small-axmodel/`:

```bash
ls ~/whisper-small-axmodel/
# Expected: whisper_small encoder/decoder axmodel files (~2.8 GB total)
```

If you need to download them fresh, follow the README in `~/whisper.axcl/` for the download URL.

### Kokoro TTS models

```bash
cd ~/kokoro.LM8850
# Follow the README to download Kokoro axmodel and voicepack files
```

---

## 6. Clone and Configure PiAi Assistant

```bash
cd ~/PiAi-Assistant
```

### Create your `.env` file

```bash
cp .env.template .env
nano .env
```

`.env` controls secrets only. Most fields are optional — only fill what you need:

```bash
# Required for cloud vision (leave blank to use placeholder)
GEMINI_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Required only if n8n is enabled
N8N_TOKEN=

# Optional: override log level (DEBUG | INFO | WARNING | ERROR)
LOG_LEVEL=INFO

# Optional: pre-downloaded sentence-transformers model cache
SENTENCE_TRANSFORMERS_HOME=/home/pi/.cache/sentence-transformers

# Optional: set to 1 to block any internet downloads at runtime (for air-gapped Pi)
TRANSFORMERS_OFFLINE=0
```

### Review `config.yaml`

The defaults work out of the box. Key settings to consider:

```yaml
assistant:
  name: PiAi                 # Change to your preferred assistant name
  system_prompt: |           # Edit to customise personality and available tools

services:
  llm:
    enable_thinking: false   # Set true to enable Qwen3 chain-of-thought (<think> blocks)
    temperature: 0.7

  vision:
    provider: placeholder    # Change to: gemini | openai | anthropic (see section 12)

n8n:
  enabled: false             # Set true once n8n is running (see section 13)
  url: http://localhost:5678

audio:
  silence_timeout_s: 1.5    # Increase if assistant cuts off before you finish speaking
  vad_aggressiveness: 2     # 0–3; increase if background noise causes false triggers
  speaker_volume: 100       # Speaker volume 0-100

display:
  scroll_speed: 3           # LCD text scroll speed in px/frame at 30fps (0 = no scroll)

memory:
  enabled: true
  top_k: 3                  # Number of past conversation chunks retrieved per query
```

---

## 7. Python Dependencies

### System packages (install first)

```bash
# Required system libraries
sudo apt install -y \
    python3-dev \
    python3-pip \
    libportaudio2 \
    alsa-utils \
    python3-picamera2 \
    libasound2-dev \
    fonts-noto-core \
    fonts-dejavu-core
```

> `python3-picamera2` must be installed via `apt` — it is not available on PyPI and requires system camera libraries.

### Orchestrator Python packages (conda env: piAi)

The orchestrator runs in a dedicated conda environment called `piAi`. Kokoro and Qwen3 each have their own existing conda envs (`kokoro` and `qwen3` respectively) — do not mix them.

```bash
# Activate conda (if not already in your shell profile)
source ~/miniforge3/etc/profile.d/conda.sh

# Create the orchestrator environment
conda create -n piAi python=3.11 -y

# Install orchestrator dependencies into the piAi env
~/miniforge3/envs/piAi/bin/pip install -r ~/PiAi-Assistant/requirements.txt
```

> **Note:** `sentence-transformers` and `chromadb` pull in several large dependencies (PyTorch CPU, ONNX Runtime). This may take 10–20 minutes on a Pi 5. Run it once and it's cached.

### Whisper ASR dependencies

Whisper ASR has its own environment managed by the `whisper.axcl` repo. Follow its README. Typically:

```bash
cd ~/whisper.axcl
pip install -r requirements.txt    # if a requirements.txt exists, otherwise:
pip install flask numpy soundfile
```

### Kokoro TTS dependencies

Kokoro uses the existing `kokoro` conda env. Follow the repo's own instructions if setting up from scratch:

```bash
cd ~/kokoro.LM8850
# Follow the repo README — dependencies install into the kokoro conda env
```

---

## 8. Pre-Download ML Models

The `sentence-transformers` embedding model (`all-MiniLM-L6-v2`, ~90MB) downloads automatically on first use. On a Pi without reliable internet you should pre-download it:

```bash
python3 -c "
from sentence_transformers import SentenceTransformer
SentenceTransformer('all-MiniLM-L6-v2')
print('Model cached.')
"
```

To force offline-only mode after pre-downloading, set in `.env`:
```bash
TRANSFORMERS_OFFLINE=1
```

---

## 9. Audio Verification

### Find the WM8960 sound card

```bash
cat /proc/asound/cards
```

Expected output on this Pi (card index 2 for WM8960):
```
 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1
 2 [wm8960soundcard]: wm8960-soundcard - wm8960-soundcard
```

Note the index next to `wm8960soundcard` — on this Pi it is **2**.

### Set speaker volume

```bash
# Replace N with the card index from above
amixer -c N set Speaker 100%

# Or use the numeric value used in start.sh (114 out of 127)
amixer -c N set Speaker 114
```

### Test microphone capture

```bash
# Record 5 seconds, then play back
arecord -D plughw:2,0 -f S16_LE -r 16000 -d 5 /tmp/test.wav
aplay -D default /tmp/test.wav
```

If you get a device busy or "no such device" error, check that:
- No other process is holding the audio device
- The card index is correct (`cat /proc/asound/cards`)
- You're using `plughw:N,0` not `hw:N,0` (the `plug` prefix enables ALSA rate conversion)

### Test speaker playback

```bash
# Play a test tone
speaker-test -c 1 -t sine -f 1000 -l 1 -D default
```

---

## 10. First Run (Manual)

Start each service in a separate terminal (or using `tmux`/`screen`). This lets you see each service's logs independently before setting up systemd.

### Terminal 1 — LLM (Qwen3-4B)

```bash
cd ~/Qwen3-4B

# Start the tokenizer server
python3 qwen3_tokenizer_uid.py --port 12300 &

# Wait 8 seconds, then start the inference binary
sleep 8
./main_api_axcl_aarch64 \
  --url_tokenizer_model http://127.0.0.1:12300 \
  --template_filename_axmodel "qwen3-4b-ax650/qwen3_p128_l%d_together.axmodel" \
  --axmodel_num 36 \
  --system_prompt "Reply in English. Be concise. /no_think" \
  --filename_post_axmodel qwen3-4b-ax650/qwen3_post.axmodel \
  --filename_tokens_embed qwen3-4b-ax650/model.embed_tokens.weight.bfloat16.bin \
  --tokens_embed_num 151936 \
  --tokens_embed_size 2560 \
  --use_mmap_load_embed 1 \
  --devices 0
```

> The LLM binary takes approximately **133 seconds** to initialise. Wait for `"Model loaded"` or similar before proceeding.

### Terminal 2 — ASR (Whisper)

```bash
cd ~/whisper.axcl
python3 server/main.py
# Listens on port 8801
```

### Terminal 3 — TTS (Kokoro)

```bash
cd ~/kokoro.LM8850
python3 kokoro_svr.py --port 8803
# Listens on port 8803
```

### Terminal 4 — Assistant orchestrator

```bash
cd ~/PiAi-Assistant && ./start.sh
```

The orchestrator will log:
```
Waiting for services... (LLM, ASR, TTS)
All services ready. Starting orchestrator.
Idle — press the button to speak.
```

**Press the Whisplay button to begin a conversation.**

---

## 11. Production Setup (systemd)

Once you've verified everything works manually, configure systemd to start all services automatically on boot.

### Edit unit file paths

All unit files in `systemd/` use `/home/andrew` and `User=andrew`. If your username differs, update each file:

```bash
cd ~/PiAi-Assistant/systemd

# Replace 'andrew' with your username throughout all unit files
sed -i 's|/home/andrew|/home/yourusername|g' *.service
sed -i 's|User=andrew|User=yourusername|g' *.service
sed -i 's|Group=andrew|Group=yourusername|g' *.service
```

### Install unit files

```bash
sudo cp ~/PiAi-Assistant/systemd/piAi-*.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Enable services (start on boot)

```bash
sudo systemctl enable piAi-llm piAi-asr piAi-tts piAi-orchestrator
```

### Start services now

```bash
# Start all services — systemd handles boot ordering automatically.
# The LLM uses Type=notify: serve.sh signals readiness after port 8000
# responds (~127s). ASR and TTS wait (After=piAi-llm.service) until the
# LLM is ready, preventing NPU VRAM contention during init.
sudo systemctl start piAi-llm piAi-asr piAi-tts piAi-orchestrator
```

### Verify all services are running

```bash
sudo systemctl status piAi-llm
sudo systemctl status piAi-asr
sudo systemctl status piAi-tts
sudo systemctl status piAi-orchestrator
```

### View live logs

```bash
# Orchestrator logs (most useful)
journalctl -u piAi-orchestrator -f

# LLM logs (shows token generation)
journalctl -u piAi-llm -f

# All PiAi services together
journalctl -u piAi-llm -u piAi-asr -u piAi-tts -u piAi-orchestrator -f

# Application log file
tail -f ~/PiAi-Assistant/data/logs/assistant.log
```

### Restart individual services

```bash
sudo systemctl restart piAi-orchestrator
```

### Stop everything

```bash
sudo systemctl stop piAi-orchestrator piAi-tts piAi-asr piAi-llm
```

---

## 12. Adding Cloud Vision

By default, the `capture_image` tool returns a canned "Vision not configured" message. To enable real image analysis:

### Option A: Google Gemini (recommended — free tier available)

1. Get an API key from [Google AI Studio](https://aistudio.google.com/app/apikey)

2. Add to `.env`:
   ```bash
   GEMINI_API_KEY=your_key_here
   ```

3. Install the Gemini SDK:
   ```bash
   pip install google-generativeai
   ```

4. Create `src/services/vision_gemini.py`:
   ```python
   import google.generativeai as genai
   from PIL import Image
   from .vision import VisionProvider

   class GeminiVisionProvider(VisionProvider):
       def __init__(self, api_key: str, model: str = "gemini-1.5-flash"):
           genai.configure(api_key=api_key)
           self._model = genai.GenerativeModel(model)

       def analyze_image(self, image_path: str, question: str) -> str:
           img = Image.open(image_path)
           response = self._model.generate_content([question, img])
           return response.text

       def health_check(self) -> bool:
           return True
   ```

5. Update `config.yaml`:
   ```yaml
   services:
     vision:
       provider: gemini
   ```

6. Uncomment the `gemini` branch in `src/services/vision.py`:
   ```python
   elif provider == "gemini":
       from .vision_gemini import GeminiVisionProvider
       import os
       return GeminiVisionProvider(api_key=os.environ["GEMINI_API_KEY"])
   ```

### Option B: OpenAI GPT-4o Vision

Similar pattern — implement `OpenAIVisionProvider` in `src/services/vision_openai.py` and set `provider: openai`.

---

## 13. Adding n8n Tools

n8n is an open-source workflow automation platform. When enabled, PiAi Assistant auto-discovers your n8n workflows as callable tools — no Python code changes needed.

### Install n8n

The easiest setup is Docker on a separate machine, or directly on the Pi:

```bash
# On the Pi (requires Node.js 18+)
sudo apt install -y nodejs npm
npm install -g n8n

# Start n8n (persists data in ~/.n8n)
n8n start
# Access the UI at http://localhost:5678
```

Or with Docker:
```bash
docker run -d \
  --name n8n \
  -p 5678:5678 \
  -v ~/.n8n:/home/node/.n8n \
  n8nio/n8n
```

### Enable in PiAi Assistant

1. Get an n8n API key from n8n UI → Settings → API → Create API Key

2. Add to `.env`:
   ```bash
   N8N_TOKEN=your_n8n_api_key
   ```

3. Update `config.yaml`:
   ```yaml
   n8n:
     enabled: true
     url: http://localhost:5678
     webhook_base: http://localhost:5678/webhook
   ```

### Create a tool workflow

Example: a workflow that reads today's weather.

1. In n8n, create a new workflow named **`get_weather`**
2. Add a **Webhook Trigger** node:
   - Method: POST
   - Path: `get_weather`
   - In the **Notes** field: `Get the current weather. Arguments: location (string).`
3. Add your logic nodes (HTTP Request to a weather API, etc.)
4. End with a **Respond to Webhook** node returning:
   ```json
   {
     "message": "It's 22 degrees and sunny in {{ $json.city }}.",
     "data": {{ $json }},
     "memory_hint": {
       "last_weather_location": "{{ $json.city }}"
     }
   }
   ```
5. **Activate** the workflow

The next time the assistant starts, it will discover `get_weather` as a tool. Ask it: *"What's the weather like in Sydney?"*

### n8n response contract

Every n8n workflow used as a tool must return:

```json
{
  "message": "Spoken response for the user",
  "data": {},
  "memory_hint": {
    "key": "value"
  }
}
```

| Field | Required | Purpose |
|---|---|---|
| `message` | Yes | Read aloud to the user via TTS |
| `data` | No | Cached in ToolDataCache for follow-up questions |
| `memory_hint` | No | Auto-stored to ChromaDB memory with 7-day TTL |

---

## 14. Troubleshooting

### Assistant won't start — "Timeout waiting for services"

The orchestrator waits up to 180 seconds for LLM, ASR, and TTS to be ready.

```bash
# Check which service is not responding
curl http://localhost:8000/api/generate_provider   # LLM — expect 200 or 400
curl -X POST http://localhost:8801/recognize       # ASR — expect 400
curl http://localhost:8803/health                  # TTS — expect {"status":"ok"}

# Check service logs
journalctl -u piAi-llm -n 50
```

Common causes:
- **LLM not ready**: It takes ~127s. The LLM uses `Type=notify` so ASR/TTS wait automatically. The orchestrator then waits up to 180s for all three. If it still times out, check `journalctl -u piAi-llm` for `AX_ENGINE_CreateHandle` errors.
- **AX_ENGINE_CreateHandle errors**: NPU VRAM contention. Stop all services (`sudo systemctl stop piAi-orchestrator piAi-llm piAi-asr piAi-tts`), wait 15s, then start again. If persistent, reboot the Pi.
- **Wrong paths in service files**: Double-check WorkingDirectory and ExecStart paths.
- **Missing model files**: Verify the axmodel files exist in `~/Qwen3-4B/qwen3-4b-ax650/`.

---

### No audio input — recording returns nothing

```bash
# Check available capture devices
arecord -l

# Test capture directly
arecord -D plughw:2,0 -f S16_LE -r 16000 -d 3 /tmp/test.wav && aplay /tmp/test.wav
```

- **Wrong card index**: Check `cat /proc/asound/cards`. If WM8960 is card 0, set `device_name: plughw:0,0` in `config.yaml`.
- **Device busy**: Check for other processes: `fuser /dev/snd/*`
- **Using `hw:` instead of `plughw:`**: The WM8960 requires the `plug` layer for rate conversion. Always use `plughw:N,0`.

---

### Transcription is empty or garbled

- **Microphone too quiet**: Increase capture volume: `amixer -c 2 set Capture 80%`
- **Wrong language**: Set `services.asr.language` in `config.yaml`
- **VAD too aggressive**: Lower `audio.vad_aggressiveness` to `1` or `0`
- **Recording stops too fast**: Increase `audio.silence_timeout_s` to `3.0`

---

### LLM generates gibberish or "SetKVCache failed"

- **SetKVCache failed**: Context window is full. This is normal after long conversations. The orchestrator detects this and truncates. The next query will work after the LLM context is reset.
- **Think tags in response**: Qwen3 may leak `<think>` blocks if `enable_thinking: true`. The orchestrator strips them, but check `data/logs/assistant.log` if you see odd responses.
- **Wrong system prompt format**: Ensure the `assistant.system_prompt` in `config.yaml` is valid YAML (proper indentation under `|` literal block scalar).

---

### Button press not detected

```bash
# Check orchestrator logs for GPIO/display errors
journalctl -u piAi-orchestrator -n 30

# Verify the display driver is loaded in-process (no separate sidecar)
# The orchestrator imports WhisPlayBoard directly — no port 12345 needed
```

- **GPIO permission**: The orchestrator needs GPIO access. Ensure the user is in the `gpio` group: `sudo usermod -aG gpio andrew`
- **Display driver not found**: Verify `services/display/WhisPlay.py` exists in `~/PiAi-Assistant/`. The orchestrator imports it directly at startup.
- **RPi.GPIO not installed**: `pip install RPi.GPIO` inside the `piAi` conda env.

---

### Camera tool returns "not configured" or fails

- **Placeholder provider**: The default vision provider is `placeholder`. See [section 12](#12-adding-cloud-vision) to add a real provider.
- **Picamera2 not installed**: `sudo apt install python3-picamera2`
- **Camera not enabled**: Run `sudo raspi-config` → Interface Options → Camera → Enable, then reboot
- **Wrong ribbon cable port**: The CSI camera must be connected to Camera Port 0 (closest to USB ports on Pi 5). Use the libcamera tools to verify: `libcamera-hello`

---

### ChromaDB / memory errors

```bash
# Check the database directory exists and is writable
ls -la ~/PiAi-Assistant/data/chroma/

# Clear the memory store if it's corrupted (this erases all stored memories)
rm -rf ~/PiAi-Assistant/data/chroma/
```

- **sqlite3 version too old**: ChromaDB requires SQLite ≥ 3.35. Check: `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`
  - Fix: `pip install pysqlite3-binary` and add to the top of `src/memory/store.py`:
    ```python
    import sys
    import pysqlite3
    sys.modules["sqlite3"] = pysqlite3
    ```

---

### Monitoring NPU health

```bash
# Real-time NPU temperature, VRAM usage, utilisation
axcl-smi

# Watch mode (refresh every 2s)
watch -n 2 axcl-smi
```

Healthy readings under load:
- Temperature: 50–70°C (fan must be running above ~60°C)
- VRAM: ~7.1GB used (LLM 6.3GB + ASR 0.5GB + TTS 0.3GB)
- If VRAM usage exceeds 8GB, one of the services has leaked — restart all services

---

### Checking assistant logs

```bash
# Live log tail
tail -f ~/PiAi-Assistant/data/logs/assistant.log

# Last 100 lines
tail -100 ~/PiAi-Assistant/data/logs/assistant.log

# Search for errors
grep -i error ~/PiAi-Assistant/data/logs/assistant.log | tail -20
```

Log levels: `DEBUG` (very verbose), `INFO` (normal operation), `WARNING` (non-fatal issues), `ERROR` (failures).

Set `LOG_LEVEL=DEBUG` in `.env` when diagnosing issues.
