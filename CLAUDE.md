# PiAi Assistant — Claude Context

This file gives Claude full project context for every session. Read this before making any changes.

---

## What This Project Is

**PiAi Assistant** is a voice-first, mostly-offline AI assistant running on a **Raspberry Pi 5** with an **M5Stack LLM8850 NPU** (M.2 2242, 8GB VRAM). All speech recognition, LLM inference, and text-to-speech run locally on the NPU. Vision falls back to a cloud provider.

**GitHub repo:** `https://github.com/AndrewGraydon/PiAi-Assistant.git`
**Local path:** `/Users/andrew.graydon/Documents/Code/Python/PiAi/assistant/`
**Reference repos:** `/Users/andrew.graydon/Documents/Code/Python/PiAi/` (read-only, do not modify)

---

## Hardware

| Component | Details |
|---|---|
| Host | Raspberry Pi 5 (8GB), Debian 12 / Ubuntu 22.04 |
| NPU | M5Stack LLM8850 (M.2 2242, 8GB VRAM, 24 TOPS) via dual M.2 HAT |
| HAT | Whisplay — 240×280 LCD, RGB LED, WM8960 audio codec, physical button |
| Camera | Freenove FNK0085 CSI module |
| Power | 5V/3A **non-PD** adapter (PD causes instability under NPU load) |

---

## NPU Services & Ports

All AI models run on the LLM8850. The orchestrator talks to them over localhost HTTP.

| Service | Model | Port | VRAM |
|---|---|---|---|
| LLM | Qwen3-4B (w8a16 int8) | 8000 | ~6.3 GB |
| ASR | Whisper-Small | 8801 | ~0.5 GB |
| TTS | Kokoro-82M | 8803 | ~0.3 GB |
| Display sidecar | `chatbot-ui.py` | 12345 (TCP) | CPU only |
| LLM tokenizer | qwen3_tokenizer_uid.py | 12300 | internal |

> Total VRAM: ~7.1 GB / 8 GB. No room for vision models — use cloud provider.
> Port 12300 is the LLM's internal tokenizer. The orchestrator only ever talks to port 8000.

---

## Project Structure

```
assistant/
├── CLAUDE.md                ← this file
├── config.yaml              ← structural config (ports, thresholds, personality, tools)
├── .env                     ← secrets only (gitignored)
├── .env.template            ← template for .env
├── requirements.txt
├── start.sh                 ← dev launcher (starts display sidecar + orchestrator)
├── main.py                  ← entry point
├── systemd/                 ← production unit files (5 services)
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

## Key Configuration (`config.yaml`)

All structural config lives here. Secrets (API keys) live only in `.env`.

```yaml
services:
  llm:    host: http://localhost:8000, temperature: 0.7, enable_thinking: false, poll_interval_ms: 500, max_tool_rounds: 5
  asr:    host: http://localhost:8801, language: en, timeout_s: 120
  tts:    host: http://localhost:8803, voice: af_heart, speed: 1.0, sample_rate: 24000
  display: host: 127.0.0.1, port: 12345
  vision: provider: placeholder   # placeholder | gemini | openai | anthropic

n8n:
  enabled: false   # set true to enable n8n workflow tools
  url: http://localhost:5678
  webhook_base: http://localhost:5678/webhook
  token: ""        # or set N8N_TOKEN in .env

audio:
  sample_rate: 16000, channels: 1, vad_aggressiveness: 2
  silence_timeout_s: 2.0, max_record_s: 30

memory:
  enabled: true, top_k: 3, score_threshold: 0.65
  default_hint_ttl_s: 604800    # 7 days
  embedding_model: all-MiniLM-L6-v2

assistant:
  name: Jarvis
  # system_prompt: defined in config.yaml — contains tool call examples

display:
  brightness: 80
  idle: 😴 #000055 | listen: 😐 #00ff00 | think: 🤔 #ff6800 | speak: 🗣️ #0055ff
```

All relative paths in `config.yaml` are resolved to absolute paths at load time by `src/config.py`.

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
       ├── src/services/llm.py       reset / generate / poll → Qwen3 :8000
       ├── src/services/tts.py       POST /synthesize → Kokoro :8803
       ├── src/services/display.py   TCP newline-JSON → chatbot-ui.py :12345
       ├── src/memory/store.py       ChromaDB cosine search (local SQLite)
       ├── src/memory/embedder.py    sentence-transformers (all-MiniLM-L6-v2, 384-dim)
       └── src/tools/
           ├── registry.py           ToolRegistry + ToolDataCache (CAAL pattern)
           ├── camera.py             Picamera2 → VisionProvider
           ├── home.py               stub
           └── n8n.py                n8n webhook discovery + execution
```

---

## LLM Protocol (non-standard — NOT OpenAI API)

The `main_axcl_aarch64` binary uses a bespoke 3-step protocol:

```
POST /api/reset    {"system_prompt": "..."}     ← sets KV cache, call once per query
POST /api/generate {"prompt": "...", ...}        ← starts generation
GET  /api/generate_provider  (every 500ms)       ← poll until done=true
  → {"done": false, "response": "partial..."}
  → {"done": true,  "response": "final chunk"}
```

**Critical behaviours in `src/services/llm.py`:**
- Appends `"\n/no_think"` to system prompt when `enable_thinking: false`
- Detects `"SetKVCache failed"` in response → NPU context window full → truncates, logs warning, next `reset()` clears it
- Checks `interrupt_flag` (threading.Event) on every 500ms poll cycle

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
1. `llm.reset(system_prompt)` — inject RAG context + n8n descriptions
2. `llm.generate(prompt)` → check for `<tool_call>`
3. If tool call found: execute, add result to `ToolDataCache`, build next prompt with result
4. If no tool call: strip think tags → final response → TTS

---

## Memory System (CAAL patterns)

**ChromaDB** (embedded SQLite, no server) + **sentence-transformers**:
- Cosine distance [0,2] → similarity: `1 - dist/2` → filter by `score_threshold=0.65`
- `memory.add(text)` — stores conversation summaries after each exchange
- `memory.add_hint(key, value, ttl_s)` — stores `"key: value"` with expiry metadata
- `memory.query(utterance)` → top-k chunks injected into system prompt as `## Relevant context from memory:`

**ToolDataCache** (rolling buffer, last 3 results):
- Prepended to LLM prompt before each generate call
- Enables follow-up questions without re-invoking tools
- e.g. "The humidity?" after "What's the weather?" — answers from cache

**n8n memory_hint auto-store:**
- n8n tools return `{"message": "...", "memory_hint": {"key": "value"}}`
- Orchestrator auto-stores hints to ChromaDB with `default_hint_ttl_s=604800` (7 days)

---

## Audio Hardware Details

- **Sound card:** WM8960 on Whisplay HAT — auto-detected via `/proc/asound/cards` ("wm8960soundcard")
- **Device string:** `plughw:N,0` (not `hw:N,0`) — the `plug` layer enables ALSA rate conversion
- **Frame constraint:** webrtcvad requires exactly 30ms frames: 480 samples × 2 bytes = 960 bytes at 16kHz
- **Playback:** `aplay -D default` subprocess — `player.stop()` terminates subprocess immediately
- **Volume:** Set at startup via `amixer -c N set Speaker 114`

---

## Display Sidecar Protocol

`chatbot-ui.py` (from whisplay-ai-chatbot-llm8850 reference repo) runs as a separate process. It owns GPIO, SPI, and LCD. We communicate via **TCP port 12345, newline-delimited JSON**.

**We send:**
```json
{"status": "thinking", "emoji": "🤔", "RGB": "#ff6800", "text": "Thinking...", "brightness": 80}
```

**We receive:**
```json
{"event": "button_pressed"}
{"event": "button_released"}
```

`DisplayClient.send()` is fire-and-forget (no ACK). Button callback fires in a separate thread to avoid blocking the recv loop. Connects with retry (15 × 3s).

---

## State Machine & Interrupt

```
IDLE ──(button)──► RECORDING ──(VAD silence)──► TRANSCRIBING ──► THINKING ──► SPEAKING ──► IDLE
                                                                      ↑           │
                                                                      └─(tool)────┘

Button press during THINKING or SPEAKING:
  → interrupt_flag.set() → player.stop() → _transition(IDLE)
  → LLM poll loop exits on next 500ms cycle
  → _speak_response() skips remaining sentences
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

## Systemd Services

Five unit files in `systemd/`. Default paths assume `User=pi`, `WorkingDirectory=/home/pi/PiAi/...`. Edit paths before deploying.

| Service | Manages | Key timing |
|---|---|---|
| `assistant-llm` | Qwen3 tokenizer (12300) + inference binary (8000) | `TimeoutStartSec=240` (LLM init ~133s) |
| `assistant-asr` | Whisper server (8801) | `TimeoutStartSec=60` |
| `assistant-tts` | Kokoro server (8803) | `TimeoutStartSec=60` |
| `assistant-display` | chatbot-ui.py sidecar (12345) | `Restart=always` |
| `assistant-orchestrator` | main.py | `After=` all four above; `TimeoutStartSec=300`; waits 180s internally for services |

---

## Key Implementation Notes

### Things that will break if you change them
- **webrtcvad frame size** must be exactly 480 samples (30ms @ 16kHz). Changing `sample_rate` or frame duration will cause VAD errors.
- **LLM port 8000** is the inference binary. Port 12300 is the tokenizer (internal). Never call 12300 from orchestrator code.
- **Tool calls parsed before think tag stripping** — do not reorder this in orchestrator.py.
- **`plughw:N,0` not `hw:N,0`** for audio capture — `hw:` will fail on WM8960 due to rate mismatch.

### Common gotchas
- **SetKVCache failed** — normal after long conversations. LLM context window (p128) is full. Detected in `llm.generate()`, response truncated, next `reset()` clears it.
- **Cosine distance** from ChromaDB is in [0,2] not [0,1]. Conversion: `similarity = 1 - dist/2`.
- **picamera2** cannot be reused across calls — create a new instance per capture, always call `stop()` + `close()` in a `finally` block.
- **Very short TTS fragments** (< 2 words) are skipped — Kokoro errors on them.
- **sentence-transformers** downloads ~90MB model on first run. Set `TRANSFORMERS_OFFLINE=1` in `.env` for air-gapped Pi.
- **ChromaDB SQLite** requires SQLite ≥ 3.35. On older Pi OS: `pip install pysqlite3-binary` and monkey-patch.

### Logging
- Format: `%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s`
- Rotating file: 10MB × 3 files at `data/logs/assistant.log`
- Noisy libraries suppressed to WARNING: httpx, urllib3, chromadb, sentence_transformers
- Override: `LOG_LEVEL=DEBUG` in `.env` or `--log-level DEBUG` CLI flag

---

## Development Workflow

### Running locally (macOS dev machine)
Most of the Python code can be edited and tested on macOS. Hardware-specific code (picamera2, sounddevice with WM8960, webrtcvad) is import-guarded or will fail gracefully.

```bash
cd /Users/andrew.graydon/Documents/Code/Python/PiAi/assistant
pip install -r requirements.txt
python3 main.py --log-level DEBUG   # Will fail waiting for NPU services (expected)
```

### Deploying to Pi
```bash
# On the Pi — pull latest from GitHub
cd ~/PiAi/assistant
git pull origin main

# Restart orchestrator only (if only Python code changed)
sudo systemctl restart assistant-orchestrator

# Restart everything (if config or services changed)
sudo systemctl restart assistant-llm assistant-asr assistant-tts assistant-display assistant-orchestrator
```

### Viewing logs on Pi
```bash
tail -f ~/PiAi/assistant/data/logs/assistant.log
journalctl -u assistant-orchestrator -f
```

---

## Reference Repos (read-only)

These repos provided the implementation patterns. Do not modify them.

| Repo | Path | What it provides |
|---|---|---|
| whisplay-ai-chatbot-llm8850 | `../whisplay-ai-chatbot-llm8850/` | chatbot-ui.py display sidecar, LLM reset/generate/poll protocol reference |
| whisper.axcl | `../whisper.axcl/` | Whisper ASR server (port 8801) |
| kokoro.LM8850 | `../kokoro.LM8850/` | Kokoro TTS server (port 8803), /synthesize and /health endpoints |
| Qwen3-4B | `../Qwen3-4B/` | LLM inference binary + tokenizer server + startup args |
| CAAL | `../CAAL/` | n8n tool pattern, ToolDataCache, memory_hint design |

---

## Extensibility Points

| Feature | How to add |
|---|---|
| New vision provider | Implement `VisionProvider` ABC in `src/services/vision_{name}.py`, add to `create_vision_client()` factory, set `provider:` in config.yaml |
| New built-in tool | Add class to `src/tools/`, register in `Orchestrator._register_tools()`, add tool description to `assistant.system_prompt` in config.yaml |
| New n8n tool | Create n8n workflow with webhook trigger, add description in node Notes, activate — no code changes needed |
| Wake word | Add a always-on VAD/keyword loop in `src/audio/` and trigger `_on_button_pressed()` programmatically |
| Home Assistant | Replace `HomeTool` stub with HA REST API calls, or create an n8n workflow that talks to HA |
| Streaming TTS | Split `_speak_response()` to begin synthesizing first sentence while LLM is still generating |
