# Architecture

## Overview

PiAi Assistant is a single-process Python orchestrator that coordinates five pre-existing HTTP services running on the LLM8850 NPU. The orchestrator owns the conversation state machine, manages audio I/O, retrieves memory context, executes tool calls, and drives the Whisplay display.

```
┌──────────────────────────────────────────────────────────────────┐
│                        Raspberry Pi 5                            │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                    ORCHESTRATOR (main.py)                  │  │
│  │                                                            │  │
│  │  State Machine: IDLE → RECORDING → TRANSCRIBING →          │  │
│  │                 THINKING → SPEAKING → IDLE                 │  │
│  │                                                            │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │  │
│  │  │ Recorder │  │  Memory  │  │  Tools   │  │  Display │  │  │
│  │  │  (VAD)   │  │(ChromaDB)│  │ Registry │  │  Client  │  │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │  │
│  └──────────────────────┬─────────────────────────┬───────────┘  │
│                         │ HTTP                     │ TCP :12345   │
│            ┌────────────┼────────────┐            │              │
│            │            │            │            │              │
│     ┌──────▼───┐ ┌──────▼───┐ ┌─────▼────┐ ┌────▼──────────┐   │
│     │  Whisper │ │ Qwen3-4B │ │  Kokoro  │ │ chatbot-ui.py │   │
│     │  ASR     │ │  LLM     │ │  TTS     │ │ Display Sidecar│   │
│     │  :8801   │ │  :8000   │ │  :8803   │ │  (Python)     │   │
│     └──────────┘ └──────────┘ └──────────┘ └───────────────┘   │
│                                                                  │
│     All AI services run on LLM8850 NPU (8GB VRAM)               │
└──────────────────────────────────────────────────────────────────┘
```

---

## Components

### Orchestrator (`src/orchestrator.py`)

The central coordinator. Runs in a single process with a background pipeline thread.

**Responsibilities:**
- Own the conversation state machine
- Connect to the display sidecar and listen for button events
- Launch the voice pipeline on button press
- Coordinate all service calls in the correct order
- Manage tool call parsing and execution loop
- Store conversation summaries in memory

**State machine:**

```
       button_press
IDLE ──────────────► RECORDING
                         │
                    VAD silence / max_record_s
                         │
                         ▼
                   TRANSCRIBING ──► (empty result) ──► IDLE
                         │
                    utterance ready
                         │
                         ▼
                     THINKING ──► (tool call loop) ──► THINKING
                         │
                    final_response ready
                         │
                         ▼
                     SPEAKING ──► (sentence by sentence TTS + play)
                         │
                    all sentences played
                         │
                         ▼
                        IDLE

     button_press during THINKING/SPEAKING:
         → interrupt_flag set → player.stop() → IDLE
```

---

### Service Clients (`src/services/`)

Thin HTTP wrappers around the pre-existing NPU servers. Each client is stateless and independently replaceable.

| Client | File | Purpose |
|---|---|---|
| `ASRClient` | `asr.py` | POST `/recognize` to Whisper server |
| `LLMClient` | `llm.py` | reset / generate / poll loop to Qwen3 binary |
| `TTSClient` | `tts.py` | POST `/synthesize` to Kokoro server |
| `DisplayClient` | `display.py` | TCP socket to `chatbot-ui.py` sidecar |
| `VisionProvider` | `vision.py` | ABC for cloud vision (placeholder default) |

---

### LLM Client — Custom Poll Protocol

The Qwen3-4B inference binary does **not** use the OpenAI API. It uses a bespoke three-step protocol:

```
POST /api/reset    {"system_prompt": "..."}   ← sets context, called once per query
POST /api/generate {"prompt": "...", ...}      ← starts generation
GET  /api/generate_provider  (every 500ms)    ← poll until done=true
  → {"done": false, "response": "partial..."}
  → {"done": true,  "response": "final chunk"}
```

The orchestrator accumulates all `response` chunks. The poll loop checks `interrupt_flag` on every cycle so a button press cancels generation immediately.

**SetKVCache error:** When the context window is full, the NPU returns `"SetKVCache failed"` in the response text. The client detects this, truncates the response, and logs a warning. The next `reset()` call clears the cache.

---

### Audio (`src/audio/`)

**Recorder (`recorder.py`):**
- Uses `sounddevice.RawInputStream` at 16kHz, mono, int16
- Frame size: 480 samples (30ms) — required by webrtcvad
- VAD loop: tracks `speech_started` flag and `silent_frames` counter
- Stops after `silence_timeout_s` (default 2s) of continuous silence post-speech
- Auto-detects WM8960 card index from `/proc/asound/cards`
- Uses `plughw:N,0` (not `hw:N,0`) to enable ALSA rate conversion

**Player (`player.py`):**
- Wraps `aplay -D default` subprocess
- `stop()` terminates the subprocess immediately
- Thread-safe via lock around the current process reference

---

### Memory (`src/memory/`)

Local semantic memory using ChromaDB (SQLite-backed, no server) and sentence-transformers.

```
User utterance
      │
      ▼
 Embedder (all-MiniLM-L6-v2, ~50ms on Pi CPU)
      │ 384-dim vector
      ▼
 ChromaDB cosine search
      │ top-k chunks above score_threshold
      ▼
 Injected into LLM system_prompt as "## Relevant context from memory:"
```

**Memory hint auto-store (from CAAL):**
n8n tool responses may include a `memory_hint` dict. These are automatically stored as structured chunks with a configurable TTL:
```json
{"memory_hint": {"flight_number": "UA1234", "departure": "08:00"}}
```
Stored as `"flight_number: UA1234"` with `expires_at = now + default_hint_ttl_s`.

**Cosine distance conversion:**
ChromaDB returns distances in `[0, 2]`. The store converts to similarity: `1 - dist/2 → [0, 1]` and filters below `score_threshold` (default 0.65).

---

### Tool System (`src/tools/`)

#### ToolRegistry

Maps tool names to Python callables. The orchestrator calls `registry.call(name, arguments)` and receives a string result.

#### ToolDataCache (from CAAL)

A rolling deque of the last `data_cache_size` (default 3) tool call results. Prepended to the LLM prompt before each generate call so the model can answer follow-up questions without re-invoking tools.

```
Recent tool results (use these to answer follow-up questions):
  - capture_image({"question": "what is on the desk?"}) → I see a laptop, coffee mug, and notebook
```

#### Tool Call Parsing

The LLM signals a tool call by emitting a `<tool_call>` block in its response:

```
<tool_call>{"name": "capture_image", "arguments": {"question": "what do you see?"}}</tool_call>
```

The orchestrator uses a `re.DOTALL` regex to extract the JSON block. Tool calls are parsed **before** stripping `<think>` tags, because Qwen3 may place the tool call inside a think block.

#### Built-in Tools

| Tool | File | Behaviour |
|---|---|---|
| `capture_image` | `camera.py` | Picamera2 capture → VisionProvider.analyze_image() |
| `home_automation` | `home.py` | Stub — returns "not configured" message |

#### n8n Tools (`n8n.py`)

When `n8n.enabled: true`:
1. On startup: GET `/api/v1/workflows` → find active workflows with webhook triggers
2. Each workflow's webhook node `notes` field becomes the tool description
3. Workflow name (sanitised) becomes the tool name
4. Execution: POST `{webhook_base}/{tool_name}` with arguments as JSON body
5. Response `memory_hint` auto-stored to ChromaDB

Security: credentials stay encrypted in n8n. The orchestrator only sends parameters — never API keys or tokens.

---

### Display Sidecar

`chatbot-ui.py` (from the `whisplay-ai-chatbot-llm8850` reference repo) is a separate Python process that owns all GPIO, SPI, and LCD rendering. It exposes a **TCP socket on port 12345** using newline-delimited JSON.

**Our `DisplayClient` sends:**
```json
{"status": "thinking", "emoji": "🤔", "RGB": "#ff6800", "text": "Thinking...", "brightness": 80}
```

**It receives:**
```json
{"event": "button_pressed"}
{"event": "button_released"}
```

The `DisplayClient` connects with retry (15 attempts × 3s), runs a daemon recv thread, and fires the registered button callback. All sends are fire-and-forget (no ACK wait).

---

### Vision Provider (`src/services/vision.py`)

Implements the `VisionProvider` ABC:

```python
class VisionProvider(ABC):
    def analyze_image(self, image_path: str, question: str) -> str: ...
    def health_check(self) -> bool: ...
```

Currently only `PlaceholderVisionProvider` exists (returns a canned message). To add Gemini:
1. Create `GeminiVisionProvider(VisionProvider)` in `src/services/vision_gemini.py`
2. Add `GEMINI_API_KEY` to `.env`
3. Set `services.vision.provider: gemini` in `config.yaml`
4. Uncomment the `elif provider == "gemini"` branch in `create_vision_client()`

---

## Configuration Architecture

```
config.yaml          →  src/config.py  →  Config dataclass
                                               │
.env (secrets)       ────────────────────────►│
                                               │
                                         All modules
                                    receive typed Config
                                    (no raw env access
                                     outside config.py)
```

**Separation of concerns:**
- `config.yaml` — service topology, ports, model parameters, UI theme, feature flags
- `.env` — API keys only (`GEMINI_API_KEY`, `N8N_TOKEN`, etc.)
- No module reads `os.environ` directly except `src/config.py`

All relative paths in `config.yaml` are resolved to absolute paths at load time.

---

## Startup Sequence

```
start.sh
  │
  ├─ Detect WM8960 card index → set speaker volume via amixer
  ├─ Source .env
  ├─ Launch chatbot-ui.py (display sidecar) → background
  ├─ sleep 3 (wait for socket to bind)
  └─ python3 main.py
        │
        ├─ load_config()     — parse config.yaml + .env
        ├─ setup_logging()   — console + rotating file
        ├─ ensure_data_dirs()— create data/ subdirs
        ├─ wait_for_services()— poll LLM/ASR/TTS health (up to 180s)
        └─ Orchestrator.run()
              │
              ├─ Embedder()        — load sentence-transformers (~5-30s first run)
              ├─ MemoryStore()     — open/create ChromaDB collection
              ├─ register_tools()  — built-ins + n8n discovery
              ├─ display.connect_with_retry()
              ├─ _transition(IDLE) — update display
              └─ display.start_event_listener()  ← blocks here
```

---

## Design Decisions

### Why not OpenAI API format?
The `main_axcl_aarch64` binary uses a bespoke reset/generate/poll protocol, not `/chat/completions`. The `LLMClient` is purpose-built for this API.

### Why sequential TTS (not parallel with generation)?
Generating the full response first, then splitting into sentences for TTS, is simpler and more reliable at Qwen3-4B's 3.65 tok/s speed. Parallel streaming+TTS adds complexity for minimal perceived latency benefit at this generation rate.

### Why aplay instead of sounddevice for playback?
`aplay` handles the WM8960 ALSA constraints cleanly without PortAudio indirection. It's battle-tested on Pi and easy to terminate via subprocess signals.

### Why ChromaDB over a simpler solution?
Semantic search is necessary for useful memory retrieval — keyword matching would miss paraphrased references. ChromaDB's embedded SQLite mode requires no separate server and works reliably on Pi.

### Why n8n for tools?
Inspired by CAAL: n8n decouples tool implementation from agent code, stores credentials securely, and allows non-developers to add automations. New tools require zero Python changes.
