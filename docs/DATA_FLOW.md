# Data Flow

This document describes the end-to-end data flow for each pipeline path through PiAi Assistant, with timing estimates for Raspberry Pi 5 + LLM8850.

---

## 1. Standard Voice Query (Text-Only Response)

The most common path — user asks a question, assistant responds in speech.

```
┌─────────────────────────────────────────────────────────────────────┐
│  User                Pi GPIO / chatbot-ui.py      Orchestrator      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  [presses button]                                                   │
│       │                                                             │
│       ▼                                                             │
│           GPIO interrupt fires                                      │
│           chatbot-ui.py sends ──────────────────► recv thread      │
│           {"event":"button_pressed"}                 │              │
│                                                      │              │
│                                                 _on_button_pressed()│
│                                                      │              │
│                                          ┌─── IDLE → RECORDING ───┐ │
│                                          │   display: 😐 green    │ │
│                                          └────────────────────────┘ │
│                                                      │              │
│  [speaks]                                            │              │
│       │                                    sounddevice.RawInputStream│
│       │  PCM 16kHz mono int16              webrtcvad 30ms frames    │
│       │  ◄──────────────────────────────── frame_size=480 samples  │
│       │                                              │              │
│       │  [silence detected after 2s]                 │              │
│       │                                    record() writes WAV      │
│       │                                    data/recordings/user_ts.wav│
│       │                                              │              │
│                                          ┌─── RECORDING → TRANSCRIBING ──┐│
│                                          │   display: 🤔 orange   │ │
│                                          └───────────────────────────┘│
│                                                      │              │
│                                        ASRClient.recognize()        │
│                                        POST /recognize              │
│                                        {filePath, base64, language} │
│                                                      │              │
│                                        ◄── Whisper NPU (~1-3s) ───► │
│                                                      │              │
│                                        {"recognition":"what time..."}│
│                                                      │              │
│                                          ┌─── TRANSCRIBING → THINKING ──┐│
│                                          │   display: 🤔 orange   │ │
│                                          └───────────────────────────┘│
│                                                      │              │
│                                        Embedder.embed(utterance)    │
│                                        (~50-100ms on Pi CPU)        │
│                                                      │              │
│                                        ChromaDB.query()             │
│                                        → top-k context chunks       │
│                                                      │              │
│                                        LLMClient.reset(system_prompt│
│                                        + RAG context)               │
│                                        POST /api/reset              │
│                                                      │              │
│                                        LLMClient.generate(utterance)│
│                                        POST /api/generate           │
│                                                      │              │
│                                        ┌──── poll loop (500ms) ────┐│
│                                        │ GET /api/generate_provider ││
│                                        │ {"done":false,"response":""}││
│                                        │ ...                        ││
│                                        │ {"done":true,"response":""}││
│                                        └───────────────────────────┘│
│                                        (~3-8s at 3.65 tok/s)        │
│                                                      │              │
│                                        _parse_tool_call() → None    │
│                                        _strip_think_tags()          │
│                                        final_response ready         │
│                                                      │              │
│                                        memory.add(summary)          │
│                                                      │              │
│                                          ┌─── THINKING → SPEAKING ──┐│
│                                          │   display: 🗣️ blue     │ │
│                                          │   text: full response    │ │
│                                          └───────────────────────────┘│
│                                                      │              │
│                                        for sentence in sentences:   │
│                                          TTSClient.synthesize()     │
│                                          POST /synthesize           │
│                                          → Kokoro NPU (~0.5-1s)    │
│                                          → WAV saved to data/tts/  │
│                                          AudioPlayer.play(wav)      │
│                                          aplay subprocess           │
│       │                                              │              │
│  [hears response]                                    │              │
│                                                      │              │
│                                          ┌─── SPEAKING → IDLE ──────┐│
│                                          │   display: 😴 dark blue │ │
│                                          └───────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

### Timing breakdown (typical)

| Stage | Typical | Notes |
|---|---|---|
| User finishes speaking | 2.0s | VAD silence detection |
| Whisper ASR | 1–3s | Whisper-Small on NPU |
| Embedding + RAG | ~150ms | CPU only |
| LLM generate (50 tokens) | 3–8s | Qwen3-4B at 3.65 tok/s |
| TTS first sentence | 0.5–1s | Kokoro on NPU |
| Audio playback | real-time | — |
| **Total to first audio** | **~7–14s** | From button release |

---

## 2. Vision Tool Call Query

User asks something that requires seeing the camera feed. The LLM emits a `<tool_call>` block, triggering a two-pass pipeline.

```
Utterance: "What's on my desk?"

                    Orchestrator
                         │
             LLMClient.reset(system_prompt with tool descriptions)
             LLMClient.generate("What's on my desk?")
                         │
                    ◄── Qwen3-4B generates ──►
                         │
              raw_response =
              "Let me take a look at what's around you.
               <tool_call>
               {"name":"capture_image","arguments":{"question":"what objects are on the desk?"}}
               </tool_call>"
                         │
              _parse_tool_call() ──► tool_call found
                         │
              display.send({"text": "Using capture_image..."})
                         │
              tool_registry.call("capture_image",
                                 {"question": "what objects are on the desk?"})
                         │
              ┌──────────────────────────────────────────┐
              │  CameraTool.__call__()                   │
              │    Picamera2()                           │
              │    configure(1920×1080 still)            │
              │    start() → sleep(0.5s warm-up)        │
              │    capture_array() → numpy RGB array    │
              │    stop() / close()                     │
              │    PIL.Image.save(capture_ts.jpg)        │
              │                                         │
              │    VisionProvider.analyze_image(         │
              │        "captures/capture_ts.jpg",       │
              │        "what objects are on the desk?") │
              │                                         │
              │    ┌─ PlaceholderVisionProvider ──────┐  │
              │    │ returns: "Vision not configured" │  │
              │    └──────────────────────────────────┘  │
              │    (or GeminiVisionProvider if configured)│
              └──────────────────────────────────────────┘
                         │
              tool_cache.add("capture_image", args, result)
                         │
              Second LLM pass:
              prompt = "What's on my desk?\n\n
                        [Tool result for 'capture_image']: I see a laptop,
                        coffee mug, and notebook.\n\n
                        Now provide your final spoken response."
                         │
              LLMClient.generate(prompt, interrupt_flag)
                         │
              final_response = "On your desk I can see a laptop, a coffee mug,
                                and a notebook."
                         │
              → TTS → play
```

### Timing breakdown (vision path)

| Stage | Typical | Notes |
|---|---|---|
| First LLM pass (tool call) | 1–3s | Short — model stops at tool call |
| Camera capture | ~1.5s | Including 0.5s sensor warm-up |
| Vision analysis | ~500ms–2s | Placeholder: instant; Gemini: ~1-2s |
| Second LLM pass (final) | 3–8s | Full response generation |
| TTS + playback | ~1–2s | — |
| **Total** | **~8–17s** | From button release |

---

## 3. n8n Tool Call Query

User request is handled by an n8n workflow (e.g. adding a task, checking weather).

```
Utterance: "Add milk to my shopping list"

                    Orchestrator
                         │
             (n8n tools discovered at startup and injected into system_prompt)
             LLMClient.generate("Add milk to my shopping list")
                         │
              raw_response =
              "<tool_call>
               {"name":"google_tasks","arguments":{"action":"create","task":"Buy milk"}}
               </tool_call>"
                         │
              _parse_tool_call() ──► tool_call found
                         │
              display.send({"text": "Using google_tasks..."})
                         │
              tool_registry.call("google_tasks",
                                 {"action":"create","task":"Buy milk"})
                         │
              ┌──────────────────────────────────────────────────────┐
              │  N8NClient.execute("google_tasks",                   │
              │                    {"action":"create","task":"..."}) │
              │                                                      │
              │  POST http://localhost:5678/webhook/google_tasks     │
              │  Body: {"action":"create","task":"Buy milk"}        │
              │                                                      │
              │  ┌── n8n workflow ────────────────────────────────┐  │
              │  │  Webhook Trigger                               │  │
              │  │        ↓                                       │  │
              │  │  Google Tasks Node (encrypted credentials)     │  │
              │  │  Creates task "Buy milk" in default list       │  │
              │  │        ↓                                       │  │
              │  │  HTTP Response:                                │  │
              │  │  {"message":"Added milk to your shopping list",│  │
              │  │   "memory_hint":{"last_task":"Buy milk",       │  │
              │  │                  "last_list":"Shopping"}}      │  │
              │  └────────────────────────────────────────────────┘  │
              │                                                      │
              │  n8n response received                               │
              └──────────────────────────────────────────────────────┘
                         │
              memory.add_hint("last_task", "Buy milk", ttl_s=604800)
              memory.add_hint("last_list", "Shopping", ttl_s=604800)
              tool_cache.add("google_tasks", args, "Added milk...")
                         │
              Second LLM pass:
              final_response = "I've added milk to your shopping list."
                         │
              → TTS → play
```

### n8n response contract

All n8n workflows called as tools must return this JSON structure:

```json
{
  "message": "Spoken response for the user",
  "data": { },
  "memory_hint": {
    "key": "value",
    "key2": {"value": "val", "ttl": 86400}
  }
}
```

| Field | Required | Purpose |
|---|---|---|
| `message` | Yes | Spoken to the user via TTS |
| `data` | No | Cached in ToolDataCache for follow-up context |
| `memory_hint` | No | Auto-stored to ChromaDB with TTL |

---

## 4. Interrupt Flow

User presses button mid-response to cancel.

```
State: SPEAKING
  │
  ▼
chatbot-ui.py GPIO ──► {"event":"button_pressed"} ──► _on_button_pressed()
                                                            │
                                                    interrupt_flag.set()
                                                    player.stop()          ← aplay terminated
                                                    _transition(IDLE)
                                                    pipeline_running.clear()
                                                            │
                                                    LLM poll loop checks interrupt_flag
                                                    ──► exits immediately if mid-generation
                                                            │
                                                    _speak_response() checks interrupt_flag
                                                    ──► skips remaining sentences
```

---

## 5. Memory RAG Flow

How past conversations are retrieved and injected as context.

```
New utterance: "How do I get to the airport?"

                    Embedder.embed("How do I get to the airport?")
                    → 384-dim float vector
                         │
                    ChromaDB.query(vector, n_results=3)
                         │
                    Results with cosine distances:
                    [("User: what time is my flight?\nAssistant: ...", dist=0.21),
                     ("flight_number: UA1234",                         dist=0.35),
                     ("User: remind me about my trip\nAssistant: ...", dist=0.58)]
                         │
                    Convert distances to similarity: 1 - dist/2
                    Filter below score_threshold (0.65)
                    → chunks: ["User: what time is my flight?...", "flight_number: UA1234"]
                         │
                    system_prompt =
                    "{base_prompt}\n\n
                     ## Relevant context from memory:\n
                     User: what time is my flight?\nAssistant: Your flight UA1234 is at 08:00.\n\n
                     flight_number: UA1234"
                         │
                    LLMClient.reset(system_prompt)
                    LLMClient.generate("How do I get to the airport?")
                         │
                    LLM now knows flight context without being told explicitly
```

---

## 6. ToolDataCache Follow-Up Flow

How the rolling tool result cache enables follow-up questions.

```
Turn 1:
  User: "What's the weather like?"
  → tool_call: get_weather({"location": "Sydney"})
  → result: {"temp": 24, "condition": "sunny", "humidity": 60}
  → tool_cache.add("get_weather", {"location":"Sydney"}, result)
  → Response: "It's 24 degrees and sunny in Sydney."

Turn 2:
  User: "What about the humidity?"
  → tool_cache.get_context_block() returns:
    "Recent tool results:
       - get_weather({"location":"Sydney"}) → {"temp":24,"condition":"sunny","humidity":60}"
  → This is prepended to the LLM prompt
  → LLM responds from cache: "The humidity in Sydney is 60%."
  → No second tool call needed
```

---

## Service Health Check Flow

At startup, `wait_for_services()` polls all three NPU services:

```
main.py starts
     │
     ▼
wait_for_services(llm_host, asr_host, tts_host, timeout=180s)
     │
     ├─ Every 3s: GET  http://localhost:8000/api/generate_provider  ─► 200/400 = LLM ready
     ├─ Every 3s: POST http://localhost:8801/recognize {}           ─► 400     = ASR ready
     └─ Every 3s: GET  http://localhost:8803/health                 ─► 200 ok  = TTS ready
     │
     │  LLM8850 init takes ~133s for Qwen3-4B
     │  Heartbeat log every 10s: "Still waiting (45s elapsed): LLM"
     │
     ▼
All services ready ──► Orchestrator.run()
     │
     (or RuntimeError if timeout exceeded)
```
