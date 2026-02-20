"""
Service health checks for PiAi Assistant.

Polls all configured HTTP services at startup until they respond or timeout.
The LLM8850 NPU services (especially LLM) can take 130+ seconds to initialise,
so a generous timeout is used.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)


def _check_llm(host: str) -> bool:
    """
    LLM health: GET /api/generate_provider.
    Expects 200 or 400 (running but no active generation). 503/connection error = not ready.
    """
    try:
        resp = requests.get(f"{host}/api/generate_provider", timeout=3.0)
        return resp.status_code in (200, 400)
    except Exception:
        return False


def _check_asr(host: str) -> bool:
    """
    ASR health: POST /recognize with empty body.
    Whisper server has no /health endpoint. An empty POST returns 400 (running)
    vs connection error / 503 (not running).
    """
    try:
        resp = requests.post(f"{host}/recognize", json={}, timeout=3.0)
        return resp.status_code in (400, 200, 429)
    except Exception:
        return False


def _check_tts(host: str) -> bool:
    """
    TTS health: GET /health.
    Kokoro server returns {"status": "ok"}.
    """
    try:
        resp = requests.get(f"{host}/health", timeout=3.0)
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def wait_for_services(
    llm_host: str,
    asr_host: str,
    tts_host: str,
    timeout_s: float = 180.0,
    poll_interval_s: float = 3.0,
) -> None:
    """
    Block until all three HTTP services respond to health checks,
    or raise RuntimeError if timeout_s is exceeded.

    Logs progress to stdout so the user can see what's happening during
    the long LLM NPU initialisation (~133s on Pi 5 with Qwen3-4B).
    """
    checks: dict[str, tuple[Callable[[str], bool], str]] = {
        "LLM  ": (_check_llm, llm_host),
        "ASR  ": (_check_asr, asr_host),
        "TTS  ": (_check_tts, tts_host),
    }

    pending = set(checks.keys())
    start = time.monotonic()
    last_log = start

    log.info("Waiting for NPU services to be ready (timeout: %ds)...", int(timeout_s))

    while pending:
        elapsed = time.monotonic() - start
        if elapsed > timeout_s:
            raise RuntimeError(
                f"Services not ready after {timeout_s:.0f}s: "
                + ", ".join(f"{name.strip()} ({checks[name][1]})" for name in pending)
            )

        for name in list(pending):
            fn, host = checks[name]
            if fn(host):
                log.info("  [OK] %s at %s (%.0fs)", name.strip(), host, elapsed)
                pending.discard(name)

        if pending:
            # Log a heartbeat every 10 seconds so the user knows we're still waiting
            if time.monotonic() - last_log >= 10.0:
                log.info(
                    "  Still waiting (%.0fs elapsed): %s",
                    elapsed,
                    ", ".join(n.strip() for n in pending),
                )
                last_log = time.monotonic()
            time.sleep(poll_interval_s)

    log.info("All services ready.")
