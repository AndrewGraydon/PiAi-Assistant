"""
LLM client for PiAi Assistant.

Talks to the Qwen3-4B inference binary (main_api_axcl_aarch64) running on the
LLM8850 NPU via port 8000.

API flow (not OpenAI-compatible):
  1. POST /api/reset       {"system_prompt": "..."}   — resets KV cache + system prompt
                           Fire-and-forget: the binary prefills the system prompt internally
                           but does NOT signal done=True when that completes. After reset,
                           the binary returns to done=True state within ~1-2s.
                           IMPORTANT: reset() is called once per conversation, NOT per turn.

  2. POST /api/generate    {"prompt": "...", "temperature": 0.7, "top-k": 40}
                           Returns 400 {"error":"llm is running"} if a generation is active.
                           Always call stop() or wait for done=True before calling generate().

  3. GET  /api/generate_provider  → {"done": bool, "response": str}
                           Streams response tokens. done=True signals completion.
                           done=True with response="" = idle (no generation in progress).

  4. POST /api/stop        — stops a running generation (no body required).
                           Returns 404 if not supported (some firmware versions).

State machine observed on the binary:
  Fresh start:             done=True,  response=""   (idle)
  After reset (prefill):   done=False, response=""   (briefly, ~1-2s)
  After reset (complete):  done=True,  response=""   (idle, ready for generate)
  During generation:       done=False, response="…"  (tokens streaming)
  After generation done:   done=True,  response="…"  (last chunk + done flag)

Key behaviours:
  - reset() is called ONCE per pipeline run to set the system prompt
  - reset() calls _stop() first, then waits for done=True (up to 8s)
  - generate() calls _stop(), waits for done=True, then starts generation
  - If _stop() returns 404 (not supported), generate() waits for natural
    completion (up to 8s) before starting — prevents 400 "llm is running"
  - generate() polls until done=True, accumulating response chunks
  - Checks interrupt_flag on every poll cycle for button-press cancellation
  - Detects "SetKVCache failed" NPU error and signals need to reset context
  - enable_thinking=False appends "/no_think" suffix to system_prompt

Note: Port 12300 (Qwen3 tokenizer) is internal to the LLM service.
The orchestrator only talks to port 8000.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_KV_CACHE_ERROR = "SetKVCache failed"

# How long to wait after reset for done=True before giving up and continuing
_RESET_WAIT_TIMEOUT_S = 8.0
# How long to wait after stop() before calling generate()
_STOP_SETTLE_S = 0.5


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

    def _wait_for_done(self, timeout_s: float = _RESET_WAIT_TIMEOUT_S) -> bool:
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

    def reset(self, system_prompt: str) -> None:
        """
        POST /api/reset — called once per pipeline run to set the system prompt
        and clear the KV cache.

        Stops any in-progress generation first, then sends the reset.
        After reset, the binary prefills the system prompt KV cache (~1-2s).
        We wait for done=True before returning so generate() can safely follow.

        If thinking is disabled, appends "/no_think" to the system prompt
        (Qwen3 convention for disabling chain-of-thought).
        """
        # Ensure LLM is idle before resetting
        self._stop()
        time.sleep(_STOP_SETTLE_S)

        prompt = system_prompt
        if not self.enable_thinking:
            prompt = prompt + "\n/no_think"

        try:
            resp = requests.post(
                f"{self.host}/api/reset",
                json={"system_prompt": prompt},
                timeout=10.0,
            )
            if resp.status_code not in (200, 204):
                log.warning("LLM reset returned status %d: %s", resp.status_code, resp.text[:100])
                return
            log.debug("LLM reset accepted, waiting for prefill to complete...")
        except requests.RequestException as e:
            log.warning("LLM reset request failed: %s", e)
            return

        # Wait for system prompt prefill to complete.
        # The binary goes done=False briefly then done=True when ready.
        if not self._wait_for_done(timeout_s=_RESET_WAIT_TIMEOUT_S):
            log.warning(
                "LLM reset prefill did not complete within %.0fs — "
                "proceeding anyway (generate may get 400 if still busy)",
                _RESET_WAIT_TIMEOUT_S,
            )
        else:
            log.debug("LLM reset complete — ready to generate")

    def generate(
        self,
        prompt: str,
        interrupt_flag: Optional[threading.Event] = None,
    ) -> str:
        """
        Start generation and poll until complete.

        If a generation is already running, stops it first.
        Returns the full accumulated response text.
        If interrupt_flag is set during polling, returns whatever has been
        accumulated so far (may be partial).

        Handles "SetKVCache failed" by logging a warning and returning the
        text accumulated before the error.
        """
        # Abort any in-progress generation before starting a new one.
        # _stop() may return 404 if not supported by this firmware — that's OK,
        # we fall back to waiting for done=True before starting our own generate.
        self._stop()
        time.sleep(_STOP_SETTLE_S)

        # If the LLM is still busy after stop (e.g. /api/stop returned 404),
        # wait up to _RESET_WAIT_TIMEOUT_S for it to finish naturally.
        if not self._wait_for_done(timeout_s=_RESET_WAIT_TIMEOUT_S):
            log.warning(
                "LLM still busy after stop+wait — proceeding anyway "
                "(generate may return 400 if still running)"
            )

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

        # Poll for results
        accumulated = ""
        poll_errors = 0

        while True:
            # Honour interrupt
            if interrupt_flag and interrupt_flag.is_set():
                log.info("LLM generation interrupted by user")
                self._stop()
                break

            time.sleep(self.poll_interval_s)

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

            # Detect NPU KV cache overflow
            if _KV_CACHE_ERROR in chunk:
                log.warning(
                    "LLM KV cache full ('%s') — context too long, truncating response",
                    _KV_CACHE_ERROR,
                )
                pre_error = chunk.split(_KV_CACHE_ERROR)[0]
                if pre_error:
                    accumulated += pre_error
                break

            if chunk:
                accumulated += chunk
                log.debug("LLM chunk: %r", chunk[:80])

            if done:
                log.debug("LLM generation complete (%d chars)", len(accumulated))
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
