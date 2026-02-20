"""
Voice Activity Detection (VAD) wrapper for PiAi Assistant.

Wraps webrtcvad.Vad. webrtcvad requirements:
  - Audio: 16-bit signed PCM, mono
  - Sample rates: 8000, 16000, 32000, or 48000 Hz
  - Frame sizes: EXACTLY 10ms, 20ms, or 30ms of audio
    e.g. at 16000 Hz, 30ms = 480 samples = 960 bytes

Aggressiveness levels:
  0 = least aggressive (keeps more audio as speech)
  3 = most aggressive (more silence detection)
  2 is recommended for typical indoor Pi environments
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class VAD:
    def __init__(self, aggressiveness: int = 2) -> None:
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(aggressiveness)
            log.debug("VAD initialised (aggressiveness=%d)", aggressiveness)
        except ImportError as e:
            raise ImportError(
                "webrtcvad not installed. Run: pip install webrtcvad"
            ) from e

    def is_speech(self, frame_bytes: bytes, sample_rate: int) -> bool:
        """
        Returns True if the frame is classified as speech.

        frame_bytes must be exactly 10ms, 20ms, or 30ms of 16-bit PCM audio
        at the given sample_rate. Incorrect sizes raise ValueError from webrtcvad.
        """
        try:
            return self._vad.is_speech(frame_bytes, sample_rate)
        except Exception as e:
            log.debug("VAD frame error (non-fatal): %s", e)
            return False
