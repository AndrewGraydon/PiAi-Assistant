"""
ASR (Automatic Speech Recognition) client for PiAi Assistant.

Talks to the whisper.axcl server (port 8801) running on the LLM8850 NPU.
API: POST /recognize with {filePath, base64} → {recognition: string}

Error codes:
  429 = service busy (retry)
  503 = service not running
  504 = timeout
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class ASRClient:
    def __init__(self, host: str, language: str, timeout_s: int) -> None:
        self.host = host.rstrip("/")
        self.language = language
        self.timeout_s = timeout_s

    def recognize(self, wav_path: str) -> str:
        """
        Send a WAV file to the Whisper ASR server.
        Returns the transcription string, or "" on failure.

        Sends both filePath and base64 to match the reference implementation.
        Retries up to 3 times on 429 (busy).
        """
        try:
            with open(wav_path, "rb") as f:
                audio_bytes = f.read()
        except OSError as e:
            log.error("Cannot read WAV file %s: %s", wav_path, e)
            return ""

        b64 = base64.b64encode(audio_bytes).decode("ascii")
        payload = {
            "filePath": wav_path,
            "base64": b64,
            "language": self.language,
        }

        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    f"{self.host}/recognize",
                    json=payload,
                    timeout=self.timeout_s,
                )

                if resp.status_code == 200:
                    text = resp.json().get("recognition", "").strip()
                    log.info("ASR result: %r", text)
                    return text

                elif resp.status_code == 429:
                    log.warning("ASR busy (attempt %d/3) — waiting 1s", attempt)
                    time.sleep(1.0)
                    continue

                elif resp.status_code == 503:
                    log.error("ASR service is not running (503)")
                    return ""

                elif resp.status_code == 504:
                    log.warning("ASR timeout (attempt %d/3)", attempt)
                    continue

                else:
                    log.error("ASR unexpected status %d: %s", resp.status_code, resp.text[:200])
                    return ""

            except requests.Timeout:
                log.warning("ASR request timed out (attempt %d/3)", attempt)
            except requests.RequestException as e:
                log.error("ASR request error: %s", e)
                return ""

        log.error("ASR failed after 3 attempts")
        return ""

    def health_check(self) -> bool:
        """
        POST /recognize with empty body.
        400 = server running (bad request), 503 = not running.
        """
        try:
            resp = requests.post(
                f"{self.host}/recognize", json={}, timeout=3.0
            )
            return resp.status_code in (200, 400, 429)
        except Exception:
            return False
