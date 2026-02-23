"""
LLM client for PiAi Assistant.

Talks to the Qwen3-4B inference binary (main_api_axcl_aarch64) running on the
LLM8850 NPU via port 8000.

API flow (not OpenAI-compatible):
  1. POST /api/reset       {} — clears the KV cache; system prompt is
                           re-prefilled from --system_prompt CLI arg.
                           IMPORTANT: /api/reset does NOT accept a system_prompt
                           JSON field — the system prompt is baked in at binary
                           startup via --system_prompt.  reset() should only be
                           called to recover from KV cache errors, NOT per turn.

  2. POST /api/generate    {"prompt": "...", "temperature": 0.7, "top-k": 40}
                           Returns 400 {"error":"llm is running"} if active.
                           Always wait for done=True before calling generate().

  3. GET  /api/generate_provider  → {"done": bool, "response": str}
                           Streams response tokens. done=True signals completion.
                           done=True with response="" = idle (no generation).

  4. POST /api/stop        — stops a running generation (no body required).
                           Returns 404 if not supported (some firmware versions).

State machine observed on the binary:
  Fresh start:             done=True,  response=""   (idle)
  After reset (prefill):   done=False, response=""   (briefly, ~1-2s)
  After reset (complete):  done=True,  response=""   (idle, ready for generate)
  During generation:       done=False, response="…"  (tokens streaming)
  After generation done:   done=True,  response="…"  (last chunk + done flag)

Key behaviours:
  - System prompt is set ONCE at binary startup via --system_prompt CLI arg
  - reset() only used for KV cache recovery (SetKVCache errors), not per-turn
  - generate() waits for done=True, then starts generation and polls to completion
  - Checks interrupt_flag on every poll cycle for button-press cancellation
  - Detects "SetKVCache failed" NPU error and triggers automatic reset recovery
  - Context window is ~1024 tokens total (including system prompt)

Note: Port 12300 (Qwen3 tokenizer) is internal to the LLM service.
The orchestrator only talks to port 8000.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

_KV_CACHE_ERROR = "SetKVCache failed"

# How long to wait for done=True before giving up
_IDLE_WAIT_TIMEOUT_S = 8.0
# How long to wait after stop() before next API call
_STOP_SETTLE_S = 0.3


class LLMClient:
    def __init__(
        self,
        host: str,
        temperature: float,
        top_k: int,
        enable_thinking: bool,
        poll_interval_ms: int,
    ) -> None:
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_k = top_k
        self.enable_thinking = enable_thinking
        self.poll_interval_s = poll_interval_ms / 1000.0

    def _wait_for_done(self, timeout_s: float = _IDLE_WAIT_TIMEOUT_S) -> bool:
        """
        Poll /api/generate_provider until done=True or timeout.
        Returns True if done=True received within timeout, False otherwise.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{self.host}/api/generate_provider", timeout=3.0
                )
                data = resp.json()
                if data.get("done", False):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _stop(self) -> None:
        """
        POST /api/stop — abort any in-progress generation.
        Silently ignored if not supported by this firmware version (404).
        """
        try:
            requests.post(f"{self.host}/api/stop", timeout=3.0)
        except Exception:
            pass

    def reset(self) -> None:
        """
        POST /api/reset — clears the KV cache and re-prefills the system
        prompt from the binary's --system_prompt CLI arg.

        Only call this to recover from KV cache errors, NOT per-conversation.
        The system prompt is baked into the binary at startup; /api/reset does
        NOT accept a new system_prompt via JSON body.
        """
        self._stop()
        time.sleep(_STOP_SETTLE_S)

        try:
            resp = requests.post(
                f"{self.host}/api/reset",
                json={},
                timeout=10.0,
            )
            if resp.status_code not in (200, 204):
                log.warning("LLM reset returned status %d: %s", resp.status_code, resp.text[:100])
                return
            log.info("LLM reset accepted, waiting for prefill to complete...")
        except requests.RequestException as e:
            log.warning("LLM reset request failed: %s", e)
            return

        # Wait for system prompt prefill to complete.
        if not self._wait_for_done(timeout_s=_IDLE_WAIT_TIMEOUT_S):
            log.warning(
                "LLM reset prefill did not complete within %.0fs — "
                "proceeding anyway",
                _IDLE_WAIT_TIMEOUT_S,
            )
        else:
            log.info("LLM reset complete — ready to generate")

    def generate(
        self,
        prompt: str,
        interrupt_flag: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Start generation and poll until complete.

        Waits for any in-progress generation to finish first.
        Returns the full accumulated response text.
        If interrupt_flag is set during polling, returns whatever has been
        accumulated so far (may be partial).

        on_progress: optional callback invoked with accumulated text on each
        poll cycle, enabling real-time streaming to the display.

        Handles "SetKVCache failed" by resetting the KV cache and returning
        whatever was accumulated before the error.
        """
        # Wait for LLM to be idle before starting
        log.info("LLM waiting for idle...")
        if not self._wait_for_done(timeout_s=_IDLE_WAIT_TIMEOUT_S):
            # Try to force-stop
            self._stop()
            time.sleep(_STOP_SETTLE_S)
            if not self._wait_for_done(timeout_s=3.0):
                log.warning("LLM still busy — attempting generate anyway")

        log.info("LLM idle — sending generate request")

        # Start generation
        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json={
                    "prompt": prompt,
                    "temperature": self.temperature,
                    "top-k": self.top_k,
                },
                timeout=15.0,
            )
            if resp.status_code not in (200, 204):
                log.error(
                    "LLM generate start returned status %d: %s",
                    resp.status_code, resp.text[:100],
                )
                return ""
        except requests.RequestException as e:
            log.error("LLM generate start failed: %s", e)
            return ""

        log.info("LLM generate POST accepted (status %d)", resp.status_code)

        # Poll for results
        accumulated = ""
        poll_errors = 0
        poll_count = 0
        poll_start = time.time()
        _POLL_TIMEOUT_S = 60.0  # hard cap to prevent infinite loop

        while True:
            # Honour interrupt
            if interrupt_flag and interrupt_flag.is_set():
                log.info("LLM generation interrupted by user")
                self._stop()
                break

            # Hard timeout to prevent infinite polling
            if time.time() - poll_start > _POLL_TIMEOUT_S:
                log.error(
                    "LLM poll timeout after %.0fs (%d polls, %d chars accumulated)",
                    _POLL_TIMEOUT_S, poll_count, len(accumulated),
                )
                break

            # Sleep AFTER first poll — don't waste 500ms before checking
            if poll_count > 0:
                time.sleep(self.poll_interval_s)
            poll_count += 1

            try:
                resp = requests.get(
                    f"{self.host}/api/generate_provider",
                    timeout=5.0,
                )
                data = resp.json()
                poll_errors = 0
            except requests.RequestException as e:
                poll_errors += 1
                log.warning("LLM poll error #%d: %s", poll_errors, e)
                if poll_errors >= 5:
                    log.error("Too many poll errors — aborting generation")
                    break
                continue

            chunk: str = data.get("response", "")
            done: bool = data.get("done", False)

            if poll_count == 1:
                log.info("LLM first poll: done=%s, response_len=%d", done, len(chunk))

            # Detect NPU KV cache overflow
            if _KV_CACHE_ERROR in chunk:
                log.warning(
                    "LLM KV cache full ('%s') — resetting context",
                    _KV_CACHE_ERROR,
                )
                pre_error = chunk.split(_KV_CACHE_ERROR)[0]
                if pre_error:
                    accumulated += pre_error
                # Recover: reset KV cache so next call works
                self.reset()
                break

            if chunk:
                accumulated += chunk
                log.debug("LLM chunk: %r", chunk[:80])
                if on_progress and accumulated:
                    try:
                        on_progress(accumulated)
                    except Exception:
                        pass  # display errors must not break generation

            if done:
                log.info(
                    "LLM generation complete (%d chars, %d polls, %.1fs)",
                    len(accumulated), poll_count, time.time() - poll_start,
                )
                break

        return accumulated

    def health_check(self) -> bool:
        """GET /api/generate_provider — expect 200 when idle (done=True)."""
        try:
            resp = requests.get(
                f"{self.host}/api/generate_provider", timeout=3.0
            )
            return resp.status_code == 200
        except Exception:
            return False
