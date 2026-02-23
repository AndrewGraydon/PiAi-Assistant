# PiAi Assistant — Claude Context

This file gives Claude full project context for every session. Read this before making any changes.

---

## What This Project Is

**PiAi Assistant** is a voice-first, mostly-offline AI assistant running on a **Raspberry Pi 5** with an **M5Stack LLM8850 NPU** (M.2 2242, 8GB VRAM). All speech recognition, LLM inference, and text-to-speech run locally on the NPU. Vision falls back to a cloud provider.

**GitHub repo:** `https://github.com/AndrewGraydon/PiAi-Assistant.git`
**Local Mac path:** `/Users/andrew.graydon/Documents/Code/Python/PiAi/assistant/`
**Pi path:** `/home/andrew/PiAi-Assistant/`
**Pi hostname:** `aiserver` — `ssh andrew@10.10.0.129`

---

## Hardware

| Component | Details |
|---|---|
| Host | Raspberry Pi 5 (8GB), Debian 12, hostname: `aiserver` |
| NPU | M5Stack LLM8850 (M.2 2242, 8GB VRAM, 24 TOPS) via dual M.2 HAT |
| HAT | Whisplay — 240×280 LCD, RGB LED, WM8960 audio codec (card index **2**), physical button |
| Camera | Freenove FNK0085 CSI module |
| Power | 5V/3A **non-PD** adapter (PD causes instability under NPU load) |

---

## NPU Services & Ports

All AI models run on the LLM8850. The orchestrator talks to them over localhost HTTP.

| Service | Model | Port | VRAM | systemd unit |
|---|---|---|---|---|
| LLM | Qwen3-4B (w8a16 int8) | 8000 | ~6.3 GB | `piAi-llm.service` |
| ASR | Whisper-Small | 8801 | ~0.5 GB | `piAi-asr.service` |
| TTS | Kokoro-82M | 8803 | ~0.3 GB | `piAi-tts.service` |
| Display | WhisPlayBoard (GPIO/SPI) | n/a — in-process | CPU only | n/a |
| LLM tokenizer | qwen3_tokenizer_uid.py | 12300 | internal | (child of piAi-llm) |

> Total VRAM: ~7.1 GB / 8 GB. No room for vision models — use cloud provider.
> Port 12300 is the LLM's internal tokenizer. The orchestrator only ever talks to port 8000.
> **No display sidecar** — display is driven directly via WhisPlayBoard GPIO/SPI in-process.

---

## Project Structure

```
PiAi-Assistant/
├── CLAUDE.md                ← this file
├── config.yaml              ← structural config (ports, thresholds, personality, tools)
├── .env                     ← secrets only (gitignored)
├── .env.template            ← template for .env
├── requirements.txt         ← orchestrator Python deps (installed into piAi conda env)
├── start.sh                 ← manual launcher (uses piAi conda env)
├── main.py                  ← entry point
├── services/                ← service scripts owned by this repo
│   ├── llm/serve.sh         ← starts Qwen3-4B (activates qwen3 conda env)
│   ├── asr/serve.sh         ← starts Whisper ASR server
│   ├── tts/serve.sh         ← starts Kokoro TTS (activates kokoro conda env)
│   └── display/WhisPlay.py  ← Whisplay HAT GPIO/SPI driver (copied from Whisplay repo)
├── systemd/                 ← production unit files (4 services, all piAi-*.service)
├── data/                    ← runtime data (gitignored)
│   ├── recordings/          ← WAV files from mic
│   ├── tts/                 ← WAV files from Kokoro
│   ├── captures/            ← JPEG files from camera
│   ├── logs/                ← rotating log files
│   └── chroma/              ← ChromaDB SQLite database
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_FLOW.md
│   └── SETUP.md
└── src/
    ├── config.py
    ├── orchestrator.py
    ├── services/            ← llm.py, asr.py, tts.py, vision.py, display.py
    ├── audio/               ← recorder.py (VAD), player.py (aplay)
    ├── memory/              ← store.py (ChromaDB), embedder.py (sentence-transformers)
    ├── tools/               ← registry.py, camera.py, home.py, n8n.py
    └── utils/               ← vad.py, health.py
```

---

## Pi Directory Layout

The model/binary directories remain outside the repo (too large for git):

| Directory | Contents | Managed by |
|---|---|---|
| `~/PiAi-Assistant/` | **This repo** — all code, configs, scripts | git |
| `~/Qwen3-4B/` | LLM binary (`main_api_axcl_aarch64`), tokenizer, model shards | manual |
| `~/whisper.axcl/` | Whisper C++ binary + Flask server | manual |
| `~/whisper-small-axmodel/` | Whisper axmodel files (2.8GB) | manual |
| `~/kokoro.LM8850/` | Kokoro server + model files (1.1GB) | manual |
| `~/Whisplay/` | Original Whisplay driver repo (reference only) | manual |
| `~/miniforge3/envs/piAi/` | Orchestrator Python env | conda |
| `~/miniforge3/envs/kokoro/` | Kokoro TTS Python env | conda |
| `~/miniforge3/envs/qwen3/` | Qwen3 tokenizer Python env | conda |
| `~/PiAi-Backup/` | Backup of original service code before consolidation | manual |

---

## Conda Environments

Three separate conda environments are required due to conflicting dependencies:

| Env | Used by | Key packages |
|---|---|---|
| `piAi` | Orchestrator (`main.py`) | chromadb, sentence-transformers, sounddevice, webrtcvad, requests, PyYAML |
| `kokoro` | Kokoro TTS server | torch 2.10, onnxruntime, kokoro 0.9.4, spacy, soundfile |
| `qwen3` | Qwen3 tokenizer | minimal — tokenizer server only |

The `services/*/serve.sh` scripts activate the correct env before starting each service.

---

## Systemd Services

Four unit files in `systemd/`. All use `User=andrew`, paths under `/home/andrew/`.

| Service | Script | Key timing |
|---|---|---|
| `piAi-llm` | `services/llm/serve.sh` | `TimeoutStartSec=300` (LLM init ~2 min) |
| `piAi-asr` | `services/asr/serve.sh` | `TimeoutStartSec=60` |
| `piAi-tts` | `services/tts/serve.sh` | `TimeoutStartSec=60` |
| `piAi-orchestrator` | `main.py` via piAi conda env | `After=` all three above; waits 180s internally |

**To install/update services on Pi:**
```bash
sudo cp ~/PiAi-Assistant/systemd/piAi-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable piAi-llm piAi-asr piAi-tts piAi-orchestrator
sudo systemctl restart piAi-llm piAi-asr piAi-tts
sudo systemctl restart piAi-orchestrator
```

---

## Key Configuration (`config.yaml`)

All structural config lives here. Secrets (API keys) live only in `.env`.

```yaml
services:
  llm:    host: http://localhost:8000, temperature: 0.7, enable_thinking: false, poll_interval_ms: 150, max_tool_rounds: 5
  asr:    host: http://localhost:8801, language: en, timeout_s: 120
  tts:    host: http://localhost:8803, voice: af_heart, speed: 1.0, sample_rate: 24000
  display: host/port unused — display driven via WhisPlayBoard GPIO directly
  vision: provider: placeholder   # placeholder | gemini | openai | anthropic

n8n:
  enabled: false   # set true to enable n8n workflow tools
  url: http://localhost:5678

audio:
  sample_rate: 16000, channels: 1, vad_aggressiveness: 2
  silence_timeout_s: 1.5, max_record_s: 30
  device_name: null   # auto-detects WM8960 (card 2 on this Pi)

memory:
  enabled: true, top_k: 3, score_threshold: 0.65
  default_hint_ttl_s: 604800    # 7 days
  embedding_model: all-MiniLM-L6-v2

A:
  name: Jarvis
  # system_prompt: in config.yaml but NOT used for LLM — personality is in serve.sh
  # Tool descriptions are auto-injected from ToolRegistry into user prompt

display:
  brightness: 80
  idle: 😴 #000055 | listen: 😐 #00ff00 | think: 🤔 #ff6800 | speak: 🗣️ #0055ff
```

---

## Architecture Overview

```
ORCHESTRATOR (src/orchestrator.py)
       │
       │── State Machine: IDLE → RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE
       │
       ├── src/audio/recorder.py     sounddevice 16kHz + webrtcvad 30ms frames
       ├── src/audio/player.py       aplay subprocess
       ├── src/services/asr.py       POST /recognize → Whisper :8801
       ├── src/services/llm.py       generate / poll → Qwen3 :8000 (reset only for KV recovery)
       ├── src/services/tts.py       POST /synthesize → Kokoro :8803
       ├── src/services/display.py   WhisPlayBoard GPIO/SPI (in-process, no TCP)
       ├── src/memory/store.py       ChromaDB cosine search (local SQLite)
       ├── src/memory/embedder.py    sentence-transformers (all-MiniLM-L6-v2, 384-dim)
       └── src/tools/
           ├── registry.py           ToolRegistry + ToolDataCache (CAAL pattern)
           ├── camera.py             Picamera2 → VisionProvider
           ├── home.py               stub
           └── n8n.py                n8n webhook discovery + execution
```

---

## Display System

**No sidecar process.** `src/services/display.py` imports `WhisPlayBoard` directly from
`services/display/WhisPlay.py` and drives the hardware in-process.

- `WhisPlayBoard` controls LCD (240×280, SPI), RGB LED (PWM), and button (GPIO interrupt)
- Falls back gracefully to no-op if `RPi.GPIO`/`spidev` are unavailable (dev machine)

**LCD layout (two-part, inspired by original Whisplay AI chatbot):**
- **Header (98px):** status text (24pt NotoSans-Bold) + battery icon (row 1), emoji (40pt DejaVuSans) centered (row 2)
- **Text area (182px):** word-wrapped response text (20pt), auto-scrolling at 30fps
- LCD has 20px rounded corners → status text inset `CORNER_INSET=20` from left edge
- Status text width clamped to avoid overlapping battery icon

**Render thread:** Background thread at 30fps handles scrolling animation. Sleeps when no animation needed (no busy-wait). Wakes on new text, battery update, or active scrolling.

**Key methods:**
- `send(payload)` — updates header (backlight, RGB LED fade, emoji, status text)
- `set_response_text(text, scroll_speed, follow_tail)` — sets scrollable text area content
  - `follow_tail=True`: streaming mode — snaps scroll to bottom (used during LLM generation)
  - `follow_tail=False`: normal mode — scrolls from top to bottom (used during TTS playback)
- `update_battery_level(level)` — triggers header re-render with new battery percentage
- `start_event_listener(cb)` — registers GPIO button interrupt; no polling thread needed

**Streaming TTS:** The orchestrator streams LLM text to the display in real-time via `on_progress` callback during generation. Complete sentences are queued for TTS synthesis+playback in a background worker thread, so the user hears speech while the LLM is still generating.

---

## LLM Protocol (non-standard — NOT OpenAI API)

The `main_api_axcl_aarch64` binary (note: **api** in name, **axmodel_num=36**) uses a bespoke protocol:

```
POST /api/reset    {}                            ← clears KV cache, re-prefills --system_prompt
POST /api/generate {"prompt": "...", ...}        ← starts generation (400 if already running)
GET  /api/generate_provider  (every 150ms)       ← poll until done=true
  → {"done": false, "response": "partial..."}
  → {"done": true,  "response": "final chunk"}
POST /api/stop                                   ← abort running generation (404 on old firmware)
```

**System prompt architecture:**
- System prompt is baked into the binary at startup via `--system_prompt` CLI arg in `services/llm/serve.sh`
- `/api/reset` does **NOT** accept a `system_prompt` JSON field — it re-prefills from the CLI arg
- `reset()` is only called for KV cache error recovery, **NOT** per-conversation
- Dynamic context (RAG memory, tool descriptions) is prepended to the **user prompt** by the orchestrator

**Critical behaviours in `src/services/llm.py`:**
- `generate()` waits for `done=True` (idle) before starting; falls back to `_stop()` if busy
- Detects `"SetKVCache failed"` → context window full → auto-calls `reset()` to recover
- Checks `interrupt_flag` (threading.Event) on every 150ms poll cycle
- `/no_think` is included in the serve.sh system prompt (not appended at runtime)

---

## Tool Call Protocol

The LLM signals a tool call by emitting a `<tool_call>` block:

```
<tool_call>{"name": "capture_image", "arguments": {"question": "what is on the desk?"}}</tool_call>
```

**Parsing rules (in `src/orchestrator.py`):**
- Regex: `re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.DOTALL)`
- Tool calls are parsed **BEFORE** stripping `<think>` tags — Qwen3 may place tool calls inside think blocks
- Think tag stripping: `re.compile(r'<think>.*?</think>', re.DOTALL)`

**Tool chain loop** (CAAL non-streaming pattern, max `max_tool_rounds=5`):
1. `_build_context_block()` — prepend tool descriptions + RAG memory to user prompt
2. `llm.generate(prompt)` → check for `<tool_call>`
3. If tool call found: execute, add result to `ToolDataCache`, build next prompt with result
4. If no tool call: strip think tags → final response → TTS

---

## Memory System (CAAL patterns)

**ChromaDB** (embedded SQLite, no server) + **sentence-transformers**:
- Cosine distance [0,2] → similarity: `1 - dist/2` → filter by `score_threshold=0.65`
- `memory.add(text)` — stores conversation summaries after each exchange
- `memory.add_hint(key, value, ttl_s)` — stores `"key: value"` with expiry metadata
- `memory.query(utterance)` → top-k chunks injected into **user prompt** via `_build_context_block()`

**ToolDataCache** (rolling buffer, last 3 results):
- Prepended to LLM prompt before each generate call
- Enables follow-up questions without re-invoking tools

**n8n memory_hint auto-store:**
- n8n tools return `{"message": "...", "memory_hint": {"key": "value"}}`
- Orchestrator auto-stores hints to ChromaDB with `default_hint_ttl_s=604800` (7 days)

---

## Audio Hardware Details

- **Sound card:** WM8960 on Whisplay HAT — card index **2** on this Pi (0=vc4hdmi0, 1=vc4hdmi1, 2=wm8960soundcard)
- **Auto-detected** via `/proc/asound/cards` — `device_name: null` in config works correctly
- **Device string:** `plughw:2,0` (not `hw:2,0`) — the `plug` layer enables ALSA rate conversion
- **Frame constraint:** webrtcvad requires exactly 30ms frames: 480 samples × 2 bytes = 960 bytes at 16kHz
- **Playback:** `aplay -D default` subprocess — `player.stop()` terminates subprocess immediately
- **Volume:** Set at startup via `amixer -c 2 set Speaker 114`

---

## State Machine & Interrupt

```
IDLE ──(button)──► RECORDING ──(VAD silence)──► TRANSCRIBING ──► THINKING ──► SPEAKING ──► IDLE
                                                                      ↑           │
                                                                      └─(tool)────┘

Button press during THINKING or SPEAKING:
  → interrupt_flag.set() → player.stop() → _transition(IDLE)
  → LLM poll loop exits on next 150ms cycle
  → TTS worker thread drains queue and stops
```

---

## n8n Tool Discovery

When `n8n.enabled: true`:
1. `GET /api/v1/workflows` → find active workflows with webhook triggers
2. Webhook node's **Notes** field → tool description (what the LLM sees)
3. Workflow name sanitised → tool name: `"Google Tasks"` → `"google_tasks"`
4. Tool execution: `POST {webhook_base}/{tool_name}` with arguments as JSON body
5. Credentials stay encrypted in n8n — orchestrator never sees API keys

**n8n response contract:**
```json
{
  "message": "Spoken response for the user",
  "data": {},
  "memory_hint": {"key": "value"}
}
```

---

## Vision Provider

`VisionProvider` ABC in `src/services/vision.py`:
```python
def analyze_image(self, image_path: str, question: str) -> str: ...
def health_check(self) -> bool: ...
```

Default: `PlaceholderVisionProvider` (returns canned message — no cloud calls).

**To add Gemini:**
1. Create `src/services/vision_gemini.py` implementing `VisionProvider`
2. Add `GEMINI_API_KEY` to `.env`
3. Set `services.vision.provider: gemini` in `config.yaml`
4. Uncomment the `elif provider == "gemini"` branch in `create_vision_client()`

---

## Key Implementation Notes

### Things that will break if you change them
- **webrtcvad frame size** must be exactly 480 samples (30ms @ 16kHz). Changing `sample_rate` or frame duration will cause VAD errors.
- **LLM binary is `main_api_axcl_aarch64`** (not `main_axcl_aarch64`). The `api` variant uses port 8000.
- **axmodel_num is 36** (all 36 transformer layers). Using 28 produces garbled/garbage output.
- **LLM port 8000** is the inference binary. Port 12300 is the tokenizer (internal). Never call 12300 from orchestrator code.
- **LLM system prompt must be a single-line string** in `serve.sh` — embedded newlines break bash argument quoting.
- **Do NOT call `/api/reset` with a `system_prompt` body** — it doesn't work. The system prompt is only set via the `--system_prompt` CLI arg at binary startup.
- **Tool calls parsed before think tag stripping** — do not reorder this in orchestrator.py.
- **`plughw:N,0` not `hw:N,0`** for audio capture — `hw:` will fail on WM8960 due to rate mismatch.
- **WM8960 is card index 2** on this Pi (not 1) — recorder auto-detects, but be aware if hardcoding.

### Common gotchas
- **SetKVCache failed** — normal after long conversations. LLM context window (~1024 tokens) is full. Detected in `llm.generate()`, auto-calls `reset()` to recover for the next query.
- **Cosine distance** from ChromaDB is in [0,2] not [0,1]. Conversion: `similarity = 1 - dist/2`.
- **picamera2** cannot be reused across calls — create a new instance per capture, always call `stop()` + `close()` in a `finally` block.
- **Very short TTS fragments** (< 2 words) are skipped — Kokoro errors on them.
- **sentence-transformers** downloads ~90MB model on first run. Set `TRANSFORMERS_OFFLINE=1` in `.env` for air-gapped Pi.
- **ChromaDB SQLite** requires SQLite ≥ 3.35. If older: `pip install pysqlite3-binary` and monkey-patch in `store.py`.
- **WhisPlayBoard LCD rendering** uses NotoSans-Bold for text (`/usr/share/fonts/truetype/noto/`) and DejaVuSans for emoji glyphs (`/usr/share/fonts/truetype/dejavu/`). Falls back through a preference list if fonts are missing. Install `fonts-noto-core` for best results.

### Logging
- Format: `%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s`
- Rotating file: 10MB × 3 files at `data/logs/assistant.log`
- Noisy libraries suppressed to WARNING: httpx, urllib3, chromadb, sentence_transformers
- Override: `LOG_LEVEL=DEBUG` in `.env` or `--log-level DEBUG` CLI flag

---

## Development Workflow

### Deploying to Pi
```bash
# From Mac — push to GitHub then pull on Pi
git push origin main
ssh andrew@10.10.0.129 "cd ~/PiAi-Assistant && git pull origin main"

# Or rsync directly (faster for iteration)
rsync -av --exclude='.git' --exclude='data/' --exclude='__pycache__' \
  /Users/andrew.graydon/Documents/Code/Python/PiAi/assistant/ \
  andrew@10.10.0.129:~/PiAi-Assistant/

# Restart orchestrator only (Python code changes)
ssh andrew@10.10.0.129 "sudo systemctl restart piAi-orchestrator"

# Restart all services (config or service script changes)
ssh andrew@10.10.0.129 "sudo systemctl restart piAi-llm piAi-asr piAi-tts piAi-orchestrator"
```

### Viewing logs on Pi
```bash
ssh andrew@10.10.0.129 "tail -f ~/PiAi-Assistant/data/logs/assistant.log"
ssh andrew@10.10.0.129 "journalctl -u piAi-orchestrator -f"
ssh andrew@10.10.0.129 "journalctl -u piAi-llm -f"
```

### Setting up GitHub SSH on Pi (for git pull)
The Pi currently has no GitHub SSH key. Use rsync or set one up:
```bash
ssh andrew@10.10.0.129 "ssh-keygen -t ed25519 -C 'andrew@aiserver' -f ~/.ssh/id_ed25519 -N ''"
ssh andrew@10.10.0.129 "cat ~/.ssh/id_ed25519.pub"
# Add the output key to GitHub → Settings → SSH keys
```

---

## Extensibility Points

| Feature | How to add |
|---|---|
| New vision provider | Implement `VisionProvider` ABC in `src/services/vision_{name}.py`, add to `create_vision_client()` factory, set `provider:` in config.yaml |
| New built-in tool | Add class to `src/tools/`, register in `Orchestrator._register_tools()` with a `description` — automatically injected into user prompt via `_build_context_block()` |
| New n8n tool | Create n8n workflow with webhook trigger, add description in node Notes, activate — no code changes needed |
| Wake word | Add always-on VAD/keyword loop in `src/audio/` and trigger `_on_button_pressed()` programmatically |
| Home Assistant | Replace `HomeTool` stub with HA REST API calls, or create an n8n workflow that talks to HA |
| Custom LCD image | Modify `_render_header()` / `_render_text_area()` in `src/services/display.py` — full Pillow image access |
