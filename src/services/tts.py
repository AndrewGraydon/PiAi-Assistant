"""
TTS (Text-to-Speech) client for PiAi Assistant.

Talks to the Kokoro TTS server (kokoro_svr.py) on port 8803.
API: POST /synthesize → {success, base64?, path?}
Health: GET /health → {"status": "ok"}

Strategy: pass outputPath in the request so Kokoro saves the WAV file
directly — avoids base64 encode/decode overhead for audio data (~200KB/sentence).
Falls back to decoding base64 if the server doesn't honour outputPath.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class TTSClient:
    def __init__(
        self,
        host: str,
        voice: str,
        speed: float,
        language: str,
        sample_rate: int,
        output_dir: str,
    ) -> None:
        self.host = host.rstrip("/")
        self.voice = voice
        self.speed = speed
        self.language = language
        self.sample_rate = sample_rate
        self.output_dir = output_dir

    def synthesize(self, text: str) -> Optional[str]:
        """
        Synthesize text to speech.
        Returns the local path of the generated WAV file, or None on failure.
        """
        if not text or not text.strip():
            return None

        ts = int(time.time() * 1000)
        out_path = os.path.join(self.output_dir, f"tts_{ts}.wav")

        payload = {
            "sentence": text.strip(),
            "voice": self.voice,
            "speed": self.speed,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "outputPath": out_path,
        }

        try:
            resp = requests.post(
                f"{self.host}/synthesize",
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error("TTS request failed: %s", e)
            return None
        except ValueError as e:
            log.error("TTS response parse error: %s", e)
            return None

        if not data.get("success"):
            log.error("TTS server error: %s", data.get("error", "unknown"))
            return None

        # Prefer server-saved file
        if data.get("path") and os.path.exists(data["path"]):
            log.debug("TTS saved to: %s", data["path"])
            return data["path"]

        # Fall back to base64 decode
        if data.get("base64"):
            try:
                wav_bytes = base64.b64decode(data["base64"])
                with open(out_path, "wb") as f:
                    f.write(wav_bytes)
                log.debug("TTS base64 decoded to: %s", out_path)
                return out_path
            except Exception as e:
                log.error("TTS base64 decode error: %s", e)
                return None

        log.error("TTS response had neither path nor base64")
        return None

    def health_check(self) -> bool:
        """GET /health → {"status": "ok"}"""
        try:
            resp = requests.get(f"{self.host}/health", timeout=3.0)
            return resp.status_code == 200 and resp.json().get("status") == "ok"
        except Exception:
            return False
