"""
LLM client for PiAi Assistant.

Talks to the Qwen3-4B inference binary (main_axcl_aarch64) running on the
LLM8850 NPU via port 8000.

API flow (not OpenAI-compatible):
  1. POST /api/reset       {"system_prompt": "..."}   — resets context + sets system prompt
  2. POST /api/generate    {"prompt": "...", "temperature": 0.7, "top-k": 40}
  3. GET  /api/generate_provider  (poll every 500ms)  → {"done": bool, "response": str}

Key behaviours:
  - generate() polls until done=True, accumulating response chunks
  - Checks interrupt_flag on every poll cycle for button-press cancellation
  - Detects "SetKVCache failed" NPU error and truncates gracefully
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

    def reset(self, system_prompt: str) -> None:
        """
        POST /api/reset — must be called before each generate() call.
        Resets the LLM's internal KV cache and sets the system prompt.

        If thinking is disabled, appends "/no_think" to the system prompt
        (Qwen3 convention for disabling chain-of-thought).
        """
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
                log.warning("LLM reset returned status %d", resp.status_code)
        except requests.RequestException as e:
            log.warning("LLM reset failed (non-fatal): %s", e)

    def generate(
        self,
        prompt: str,
        interrupt_flag: Optional[threading.Event] = None,
    ) -> str:
        """
        Start generation and poll until complete.

        Returns the full accumulated response text.
        If interrupt_flag is set during polling, returns whatever has been
        accumulated so far (may be partial).

        Handles "SetKVCache failed" by logging a warning and returning the
        text accumulated before the error.
        """
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
                log.error("LLM generate start returned status %d", resp.status_code)
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
                # Keep text before the error marker
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
        """GET /api/generate_provider — expect 200 or 400 when idle."""
        try:
            resp = requests.get(
                f"{self.host}/api/generate_provider", timeout=3.0
            )
            return resp.status_code in (200, 400)
        except Exception:
            return False
