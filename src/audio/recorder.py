"""
Button-triggered audio recorder for PiAi Assistant.

Uses sounddevice for capture (PortAudio → ALSA) and webrtcvad for
Voice Activity Detection to stop recording automatically after silence.

ALSA / sounddevice device notes:
  - sounddevice (PortAudio) does NOT accept "plughw:N,0" strings — those are
    raw ALSA device names. sounddevice matches by device name substring or index.
  - WM8960 on Whisplay HAT registers as "wm8960soundcard" in /proc/asound/cards
    and as "wm8960-soundcard" in sounddevice's device list.
  - device_name: null in config.yaml triggers auto-detection of the WM8960 card
    by name substring match against sounddevice's device list.

webrtcvad frame size constraint:
  At 16000 Hz with 30ms frames: blocksize = 16000 * 30 / 1000 = 480 samples
  Each sample is 2 bytes (int16) → frame = 960 bytes
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import numpy as np
import soundfile as sf

from src.utils.vad import VAD

log = logging.getLogger(__name__)

# Frame duration in ms — must be 10, 20, or 30 for webrtcvad
_FRAME_MS = 30


def _detect_wm8960_device() -> Optional[str]:
    """
    Auto-detect the WM8960 sound card by searching sounddevice's device list
    for a name containing 'wm8960'.

    sounddevice (PortAudio) does not accept raw ALSA strings like "plughw:N,0".
    We match by name substring and return the sounddevice device name string,
    which PortAudio will resolve correctly. Returns None to use the system
    default if not found.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if "wm8960" in d["name"].lower() and d["max_input_channels"] > 0:
                log.info(
                    "Auto-detected WM8960 audio device: [%d] %s", i, d["name"]
                )
                return d["name"]
    except Exception as e:
        log.debug("sounddevice device query failed: %s", e)
    log.debug("WM8960 not found in sounddevice list (normal on dev machine) — using default")
    return None


class AudioRecorder:
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        device_name: Optional[str],
        vad_aggressiveness: int,
        silence_timeout_s: float,
        max_record_s: float,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.silence_timeout_s = silence_timeout_s
        self.max_record_s = max_record_s

        # Resolve device name
        # If config specifies a device, use it as-is.
        # Otherwise auto-detect WM8960 by sounddevice name (not ALSA plughw string).
        if device_name:
            self.device_name = device_name
        else:
            self.device_name = _detect_wm8960_device()   # may be None (uses system default)

        # Frame size for webrtcvad
        self.frame_size = int(sample_rate * _FRAME_MS / 1000)   # 480 at 16kHz

        self._vad = VAD(aggressiveness=vad_aggressiveness)

    def record(
        self, output_path: str, stop_event: Optional["threading.Event"] = None
    ) -> Optional[str]:
        """
        Record audio from the microphone until:
          - stop_event is set (push-to-talk: second button press), OR
          - max_record_s total is reached

        Writes a WAV file to output_path.
        Returns output_path on success, or None if no speech was detected / error occurred.
        """
        try:
            import sounddevice as sd
        except ImportError as e:
            log.error("sounddevice not installed: %s", e)
            return None

        max_frames = int(self.max_record_s * 1000 / _FRAME_MS)

        frames: list[bytes] = []

        log.debug(
            "Recording (push-to-talk): device=%s rate=%d frame_size=%d",
            self.device_name, self.sample_rate, self.frame_size,
        )

        try:
            with sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=self.device_name,
                blocksize=self.frame_size,
            ) as stream:
                for _ in range(max_frames):
                    if stop_event and stop_event.is_set():
                        log.debug(
                            "Stop event received after %d frames", len(frames)
                        )
                        break

                    frame_bytes, overflowed = stream.read(self.frame_size)
                    if overflowed:
                        log.debug("Audio input overflow (non-fatal)")

                    frames.append(bytes(frame_bytes))

        except Exception as e:
            log.error("Recording error: %s", e)
            return None

        if len(frames) < 5:
            log.info("Recording too short — discarding")
            return None

        # Write WAV
        try:
            audio_data = np.frombuffer(b"".join(frames), dtype=np.int16)
            sf.write(output_path, audio_data, self.sample_rate)
            duration_s = len(audio_data) / self.sample_rate
            log.info("Recorded %.1fs of audio → %s", duration_s, output_path)
            return output_path
        except Exception as e:
            log.error("WAV write error: %s", e)
            return None
