"""
PiAi Assistant Orchestrator.

Implements the conversation state machine:
  IDLE → RECORDING → TRANSCRIBING → THINKING → SPEAKING → IDLE

The orchestrator owns the main event loop. It connects to the display sidecar
socket, waits for button press events, and runs the voice pipeline in a
background thread.

Key design decisions:
  - Button press while IDLE: starts recording
  - Button press while THINKING or SPEAKING: interrupts and returns to IDLE
  - Tool chain uses CAAL's non-streaming loop pattern: execute tools until
    the LLM produces a plain-text final response (no tool call)
  - ToolDataCache provides rolling context for follow-up questions (CAAL)
  - memory_hint from n8n tools auto-stored to ChromaDB (CAAL)
  - Think tags (<think>...</think>) stripped from ALL LLM output
  - Tool calls parsed BEFORE stripping think tags (in case model puts
    tool_call inside a think block)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from enum import Enum, auto
from typing import Callable, List, Optional

from src.config import Config
from src.services.asr import ASRClient
from src.services.display import DisplayClient
from src.services.llm import LLMClient
from src.services.tts import TTSClient
from src.services.vision import create_vision_client
from src.audio.recorder import AudioRecorder
from src.audio.player import AudioPlayer
from src.memory.embedder import Embedder
from src.memory.store import MemoryStore
from src.tools.camera import CameraTool
from src.tools.home import HomeTool
from src.tools.n8n import N8NClient
from src.tools.registry import ToolDataCache, ToolRegistry
from src.utils.battery import BatteryMonitor

log = logging.getLogger(__name__)

# Regex to extract <tool_call>...</tool_call> blocks (DOTALL for multiline JSON)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Regex to strip <think>...</think> blocks
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = State.IDLE
        self._interrupt_flag = threading.Event()
        self._pipeline_running = threading.Event()

        # --- Service clients ---
        self.display = DisplayClient(
            host=config.display_socket.host,
            port=config.display_socket.port,
        )
        self.asr = ASRClient(
            host=config.asr.host,
            language=config.asr.language,
            timeout_s=config.asr.timeout_s,
        )
        self.llm = LLMClient(
            host=config.llm.host,
            temperature=config.llm.temperature,
            top_k=config.llm.top_k,
            enable_thinking=config.llm.enable_thinking,
            poll_interval_ms=config.llm.poll_interval_ms,
        )
        self.tts = TTSClient(
            host=config.tts.host,
            voice=config.tts.voice,
            speed=config.tts.speed,
            language=config.tts.language,
            sample_rate=config.tts.sample_rate,
            output_dir=config.paths.tts_dir,
        )
        self.vision = create_vision_client(config.vision)

        # --- Audio ---
        self.recorder = AudioRecorder(
            sample_rate=config.audio.sample_rate,
            channels=config.audio.channels,
            device_name=config.audio.device_name,
            vad_aggressiveness=config.audio.vad_aggressiveness,
            silence_timeout_s=config.audio.silence_timeout_s,
            max_record_s=config.audio.max_record_s,
        )
        self.player = AudioPlayer()

        # --- Memory ---
        if config.memory.enabled:
            log.info("Initialising memory (ChromaDB + sentence-transformers)...")
            self.embedder = Embedder(config.memory.embedding_model)
            self.memory = MemoryStore(
                db_path=config.memory.db_path,
                collection_name=config.memory.collection,
                embedder=self.embedder,
                top_k=config.memory.top_k,
                score_threshold=config.memory.score_threshold,
            )
        else:
            self.embedder = None
            self.memory = None
            log.info("Memory disabled (memory.enabled = false)")

        # --- Tools ---
        self.tool_registry = ToolRegistry()
        self.tool_cache = ToolDataCache(max_size=config.tools.data_cache_size)

        # n8n client (discovers workflows if enabled)
        self.n8n = N8NClient(
            enabled=config.n8n.enabled,
            url=config.n8n.url,
            webhook_base=config.n8n.webhook_base,
            token=config.n8n.token,
            timeout_s=config.n8n.timeout_s,
        )

        # --- Battery monitor ---
        self.battery_monitor: Optional[BatteryMonitor] = None
        if config.battery.enabled:
            self.battery_monitor = BatteryMonitor(
                callback=self.display.update_battery_level,
                host=config.battery.host,
                port=config.battery.port,
                poll_interval_s=config.battery.poll_interval_s,
            )

        self._register_tools()

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        """Register built-in tools and discover n8n workflows."""

        if self.config.tools.capture_image_enabled:
            camera_tool = CameraTool(
                captures_dir=self.config.paths.captures_dir,
                vision_provider=self.vision,
            )
            self.tool_registry.register(
                "capture_image",
                camera_tool,
                description="Capture and analyze what the camera sees",
            )

        if self.config.tools.home_automation_enabled:
            self.tool_registry.register(
                "home_automation",
                HomeTool(),
                description="Control smart home devices",
            )

        # Discover n8n workflow tools (if enabled)
        if self.config.n8n.enabled:
            n8n_tools = self.n8n.discover_workflows()
            for tool_def in n8n_tools:
                name = tool_def["name"]
                desc = tool_def["description"]
                # Wrap n8n execution in a lambda that routes through N8NClient
                self.tool_registry.register(
                    name,
                    lambda _n=name, **kwargs: self._execute_n8n_tool(_n, kwargs),
                    description=desc,
                )

    def _execute_n8n_tool(self, tool_name: str, arguments: dict) -> str:
        """
        Execute an n8n workflow and handle memory_hint auto-store (CAAL pattern).
        Returns a plain string for the tool chain.
        """
        result = self.n8n.execute(tool_name, arguments)

        # Handle structured n8n response
        if isinstance(result, dict):
            # Auto-store memory hints to ChromaDB
            if self.memory and "memory_hint" in result:
                for key, val in result["memory_hint"].items():
                    ttl = self.config.memory.default_hint_ttl_s
                    # Support {"value": ..., "ttl": ...} form
                    if isinstance(val, dict):
                        ttl = val.get("ttl", ttl) or ttl
                        val = val.get("value", str(val))
                    self.memory.add_hint(str(key), str(val), ttl_s=ttl)

            # Cache structured data
            if "data" in result:
                self.tool_cache.add(tool_name, arguments, result["data"])

            return result.get("message", json.dumps(result, ensure_ascii=False))

        return str(result)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Connect to the display sidecar and block forever waiting for button events.
        """
        log.info("Connecting to display sidecar at %s:%d...",
                 self.config.display_socket.host, self.config.display_socket.port)
        self.display.connect_with_retry(max_retries=15, retry_interval_s=3.0)

        self._transition(State.IDLE)
        self.display.start_event_listener(self._on_button_pressed)

        # Start battery monitor after display is ready (so first update renders)
        if self.battery_monitor:
            self.battery_monitor.start()

        # Set speaker volume from config
        self._set_speaker_volume(self.config.audio.speaker_volume)

        log.info(
            "%s is ready. Press the button to speak.",
            self.config.assistant.name,
        )

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("Orchestrator shutting down...")
        self._interrupt_flag.set()
        if self.battery_monitor:
            self.battery_monitor.stop()
        self.player.stop()
        self.display.disconnect()

    # ------------------------------------------------------------------
    # Button handler
    # ------------------------------------------------------------------

    def _on_button_pressed(self) -> None:
        """Called from display listener thread on button_pressed event."""
        if self.state == State.IDLE:
            if not self._pipeline_running.is_set():
                self._pipeline_running.set()
                threading.Thread(
                    target=self._pipeline_thread,
                    name="pipeline",
                    daemon=True,
                ).start()

        elif self.state in (State.THINKING, State.SPEAKING):
            log.info("Button pressed mid-pipeline — interrupting")
            self._interrupt_flag.set()
            self.player.stop()
            self._transition(State.IDLE)
            self._pipeline_running.clear()

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _pipeline_thread(self) -> None:
        """Full voice pipeline: record → ASR → LLM (with streaming TTS) → IDLE."""
        self._interrupt_flag.clear()
        self.tool_cache.clear()

        try:
            # 1. Record
            self._transition(State.RECORDING)
            ts = int(time.time() * 1000)
            wav_path = os.path.join(
                self.config.paths.recordings_dir, f"user_{ts}.wav"
            )
            recorded = self.recorder.record(wav_path)
            if not recorded or self._interrupt_flag.is_set():
                self._transition(State.IDLE)
                return

            # 2. Transcribe
            self._transition(State.TRANSCRIBING)
            utterance = self.asr.recognize(wav_path)
            if not utterance.strip() or self._interrupt_flag.is_set():
                log.info("Empty transcription — returning to idle")
                self._transition(State.IDLE)
                return

            # Filter out known Whisper ASR failure patterns
            if self._is_asr_garbage(utterance):
                log.warning("ASR garbage detected, ignoring: %r", utterance)
                self._transition(State.IDLE)
                return

            log.info("Utterance: %r", utterance)

            # Show transcribed speech on display
            self.display.set_response_text(f'"{utterance}"')

            # 3. Retrieve memory context
            context_chunks: List[str] = []
            if self.memory:
                context_chunks = self.memory.query(utterance)
                if context_chunks:
                    log.debug("RAG context: %d chunks", len(context_chunks))

            # 4. Think / generate with streaming TTS
            self._transition(State.THINKING)

            # Streaming TTS: queue complete sentences for synthesis+playback
            # while the LLM is still generating more text.
            tts_q: queue.Queue[Optional[str]] = queue.Queue()
            spoken_count = [0]       # mutable int for closure
            tts_started = threading.Event()

            def _on_llm_progress(text: str) -> None:
                """Stream LLM text to display and queue sentences for TTS."""
                clean = self._strip_think_tags(text)
                self.display.set_response_text(clean, follow_tail=True)
                # Queue complete sentences (skip last — may still be growing)
                sentences = self._split_sentences(clean)
                while spoken_count[0] < len(sentences) - 1:
                    tts_q.put(sentences[spoken_count[0]])
                    spoken_count[0] += 1
                    if not tts_started.is_set():
                        tts_started.set()
                        self._transition(State.SPEAKING)

            def _on_tool_round_reset() -> None:
                """Clear queued TTS when a tool call round resets."""
                spoken_count[0] = 0
                while not tts_q.empty():
                    try:
                        tts_q.get_nowait()
                    except queue.Empty:
                        break
                tts_started.clear()

            def _tts_worker() -> None:
                """Synthesize and play sentences from the queue."""
                while True:
                    sentence = tts_q.get()
                    if sentence is None:
                        break
                    if self._interrupt_flag.is_set():
                        while not tts_q.empty():
                            try:
                                tts_q.get_nowait()
                            except queue.Empty:
                                break
                        break
                    clean = self._purify_for_tts(sentence)
                    if len(clean.split()) < 2:
                        continue
                    wav = self.tts.synthesize(clean)
                    if wav and not self._interrupt_flag.is_set():
                        self.player.play(wav)

            tts_thread = threading.Thread(
                target=_tts_worker, daemon=True, name="tts-stream"
            )
            tts_thread.start()

            final_response = self._run_tool_chain(
                utterance, context_chunks,
                on_progress=_on_llm_progress,
                on_tool_round_reset=_on_tool_round_reset,
            )

            if not final_response or self._interrupt_flag.is_set():
                tts_q.put(None)
                tts_thread.join(timeout=5.0)
                self._transition(State.IDLE)
                return

            # Queue remaining sentences (last sentence wasn't queued during streaming)
            all_sentences = self._split_sentences(final_response)
            for s in all_sentences[spoken_count[0]:]:
                tts_q.put(s)
            tts_q.put(None)  # sentinel

            if not tts_started.is_set():
                self._transition(State.SPEAKING)

            # Switch display to normal scroll-from-top mode
            self.display.set_response_text(
                final_response,
                scroll_speed=self.config.display_theme.scroll_speed,
            )

            # 5. Store exchange in memory (skip garbled output)
            if self.memory and self._is_valid_response(final_response):
                summary = f"User: {utterance}\nAssistant: {final_response}"
                self.memory.add(summary)

            # Wait for TTS to finish playing
            tts_thread.join()

        except Exception:
            log.exception("Unhandled pipeline error")
        finally:
            self._pipeline_running.clear()
            if not self._interrupt_flag.is_set():
                self._transition(State.IDLE)

    # ------------------------------------------------------------------
    # Tool chain (CAAL non-streaming pattern)
    # ------------------------------------------------------------------

    def _run_tool_chain(
        self,
        utterance: str,
        context_chunks: List[str],
        on_progress: Optional[Callable[[str], None]] = None,
        on_tool_round_reset: Optional[Callable[[], None]] = None,
    ) -> str:
        """
        Run a tool-execution loop:
          1. Generate with current prompt (includes RAG context + tool descriptions)
          2. If response contains <tool_call>: execute tool, inject result, loop
          3. If no tool call: strip think tags, return as final response

        The system prompt (personality) is baked into the binary at startup
        via --system_prompt CLI arg. Dynamic context (RAG memory, tool
        descriptions) is prepended to the user prompt here.

        on_progress: callback invoked with accumulated text during generation
                     (for streaming to display + TTS).
        on_tool_round_reset: callback invoked when a tool call is detected,
                             allowing the caller to clear any queued TTS.

        Runs up to config.llm.max_tool_rounds iterations.
        """
        context_block = self._build_context_block(context_chunks)
        prompt = f"{context_block}\n\n{utterance}" if context_block else utterance

        for round_num in range(self.config.llm.max_tool_rounds):
            if self._interrupt_flag.is_set():
                return ""

            # Inject ToolDataCache context for follow-up awareness (CAAL)
            tool_context = self.tool_cache.get_context_block()
            full_prompt = f"{tool_context}\n\n{prompt}" if tool_context else prompt

            log.debug("LLM generate (round %d)", round_num + 1)
            raw = self.llm.generate(
                full_prompt,
                interrupt_flag=self._interrupt_flag,
                on_progress=on_progress,
            )

            if not raw or self._interrupt_flag.is_set():
                return ""

            # Parse tool call BEFORE stripping think tags
            # (model may place tool_call inside <think> block)
            tool_call = self._parse_tool_call(raw)

            if tool_call is None:
                # No tool call — this is the final response
                return self._strip_think_tags(raw)

            # Tool call detected — reset streaming TTS state
            if on_tool_round_reset:
                on_tool_round_reset()

            # Execute the tool
            tool_name = tool_call.get("name", "")
            tool_args = tool_call.get("arguments", {})

            if not tool_name or tool_name not in self.tool_registry:
                log.warning("LLM called unknown tool: %r", tool_name)
                return self._strip_think_tags(raw.replace(
                    self._extract_tool_call_block(raw), ""
                ))

            self.display.send({"text": f"Using {tool_name}..."})
            log.info("Tool call: %s(%s)", tool_name, json.dumps(tool_args)[:100])

            result_str = self.tool_registry.call(tool_name, tool_args)

            # Cache result for follow-up questions
            self.tool_cache.add(tool_name, tool_args, result_str)

            # Build next-round prompt with tool result injected
            prompt = (
                f"{utterance}\n\n"
                f"[Tool result for '{tool_name}']: {result_str}\n\n"
                f"Now provide your final spoken response to the user."
            )
            log.debug("Tool result injected, starting round %d", round_num + 2)

        log.warning("Max tool rounds (%d) reached", self.config.llm.max_tool_rounds)
        return "I had trouble completing that request. Please try again."

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

    def _parse_tool_call(self, text: str) -> Optional[dict]:
        """
        Extract and parse the first <tool_call>...</tool_call> JSON block.
        Returns parsed dict or None.
        """
        match = _TOOL_CALL_RE.search(text)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            log.warning("Failed to parse tool call JSON: %s — raw: %r", e, match.group(1)[:200])
            return None

    def _extract_tool_call_block(self, text: str) -> str:
        """Return the full <tool_call>...</tool_call> block for removal."""
        match = _TOOL_CALL_RE.search(text)
        return match.group(0) if match else ""

    def _strip_think_tags(self, text: str) -> str:
        """Remove all <think>...</think> blocks from LLM output."""
        text = _THINK_RE.sub("", text)
        # Clean up stray opening/closing tags
        text = text.replace("<think>", "").replace("</think>", "")
        return text.strip()

    # Known Whisper ASR failure patterns (case-insensitive substrings)
    _ASR_GARBAGE_PATTERNS = [
        "speaking in foreign language",
        "foreign language",
        "inaudible",
        "music playing",
        "background noise",
        "[music]",
        "(music)",
    ]

    def _is_asr_garbage(self, text: str) -> bool:
        """Check if ASR output is a known Whisper hallucination/failure pattern."""
        lower = text.lower().strip()
        for pattern in self._ASR_GARBAGE_PATTERNS:
            if pattern in lower:
                return True
        return False

    def _is_valid_response(self, text: str) -> bool:
        """
        Check if an LLM response looks like valid English output.
        Rejects garbled/repetitive text to avoid polluting ChromaDB memory.
        """
        if not text or len(text) < 5:
            return False
        # Reject if mostly non-ASCII (Chinese, Russian garble from broken generation)
        ascii_chars = sum(1 for c in text if ord(c) < 128)
        if ascii_chars / len(text) < 0.7:
            log.warning("Rejecting non-ASCII response from memory storage (%d%% ASCII)",
                        int(100 * ascii_chars / len(text)))
            return False
        return True

    def _set_speaker_volume(self, volume_pct: int) -> None:
        """Set WM8960 speaker volume. volume_pct: 0-100."""
        alsa_val = int(volume_pct * 127 / 100)
        try:
            # Detect WM8960 card index from /proc/asound/cards
            card = None
            with open("/proc/asound/cards") as f:
                for line in f:
                    if "wm8960" in line:
                        card = line.strip().split()[0]
                        break
            if not card:
                log.debug("No WM8960 card found — skipping volume set")
                return
            subprocess.run(
                ["amixer", "-c", card, "cset",
                 f"name=Speaker Playback Volume", f"{alsa_val},{alsa_val}"],
                capture_output=True, timeout=5,
            )
            log.info("Speaker volume set to %d%% (ALSA %d/127)", volume_pct, alsa_val)
        except Exception as e:
            log.warning("Could not set speaker volume: %s", e)

    def _split_sentences(self, text: str) -> List[str]:
        """
        Split response text into sentences for sentence-by-sentence TTS.
        Splits on ., !, ? followed by whitespace or end-of-string.
        """
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p.strip() for p in parts if p.strip()]

    def _purify_for_tts(self, text: str) -> str:
        """
        Clean text before sending to TTS:
          - Remove markdown bold/italic (**)
          - Remove non-ASCII characters (emoji, special symbols)
          - Collapse extra whitespace
        """
        text = re.sub(r"\*+", "", text)
        text = re.sub(r"[^\x00-\x7F]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Speaking
    # ------------------------------------------------------------------

    def _speak_response(self, text: str) -> None:
        """
        Split text into sentences, synthesize each with TTS, play in order.
        Checks interrupt_flag before each sentence.
        """
        self.display.set_response_text(
            text, scroll_speed=self.config.display_theme.scroll_speed
        )

        sentences = self._split_sentences(text)
        log.debug("Speaking %d sentence(s)", len(sentences))

        for sentence in sentences:
            if self._interrupt_flag.is_set():
                break

            clean = self._purify_for_tts(sentence)
            # Skip fragments that are too short for TTS
            if len(clean.split()) < 2:
                continue

            wav_path = self.tts.synthesize(clean)
            if wav_path and not self._interrupt_flag.is_set():
                self.player.play(wav_path)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _transition(self, new_state: State) -> None:
        """Update state machine and sync display."""
        self.state = new_state
        t = self.config.display_theme

        state_map = {
            State.IDLE: {
                "status": "idle",
                "emoji": t.idle_emoji,
                "RGB": t.idle_color,
                "text": f"Press button to speak",
                "brightness": t.brightness,
            },
            State.RECORDING: {
                "status": "listening",
                "emoji": t.listen_emoji,
                "RGB": t.listen_color,
                "text": "Listening...",
                "brightness": t.brightness,
            },
            State.TRANSCRIBING: {
                "status": "recognizing",
                "emoji": t.think_emoji,
                "RGB": t.think_color,
                "text": "Recognizing...",
                "brightness": t.brightness,
            },
            State.THINKING: {
                "status": "thinking",
                "emoji": t.think_emoji,
                "RGB": t.think_color,
                "text": "Thinking...",
                "brightness": t.brightness,
            },
            State.SPEAKING: {
                "status": "speaking",
                "emoji": t.speak_emoji,
                "RGB": t.speak_color,
                "text": "Speaking...",
                "brightness": t.brightness,
            },
        }

        payload = state_map.get(new_state, {})
        if payload:
            self.display.send(payload)

        # Clear scrolling response text when leaving SPEAKING state
        if new_state != State.SPEAKING:
            self.display.set_response_text("")

        log.debug("State: %s", new_state.name)

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    # Rough char budget for the context block.
    # The LLM context window is ~1024 tokens total.  System prompt
    # (~50 tokens) + user utterance (~30-50 tokens) + response (~200-400
    # tokens) leaves ~500 tokens for context.  At ~4 chars/token that's
    # ~2000 chars.  Tool descriptions are ~250 chars, leaving ~1750 for
    # RAG chunks.  We cap the total context block to be safe.
    _CONTEXT_CHAR_BUDGET = 1500

    def _build_context_block(self, context_chunks: List[str]) -> str:
        """
        Build the dynamic context prepended to the user prompt.

        The base personality prompt and tool descriptions are baked into the
        LLM binary at startup (via --system_prompt in serve.sh).  This method
        only adds RAG memory context — tool descriptions are NOT injected
        here because the small 4B model gets confused by <tool_call> examples
        appearing in the user prompt.

        The total output is capped at _CONTEXT_CHAR_BUDGET to avoid
        overflowing the LLM's ~1024 token context window.
        """
        if not context_chunks:
            return ""

        # Only include RAG memory context, truncated to budget
        truncated: List[str] = []
        used = 0
        for chunk in context_chunks:
            if used + len(chunk) > self._CONTEXT_CHAR_BUDGET:
                break
            truncated.append(chunk)
            used += len(chunk)

        if not truncated:
            return ""

        ctx = "\n\n".join(truncated)
        return f"[Context]\n{ctx}"
