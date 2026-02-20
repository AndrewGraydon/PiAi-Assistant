"""
Button-triggered audio recorder for PiAi Assistant.

Uses sounddevice for capture (PortAudio → ALSA) and webrtcvad for
Voice Activity Detection to stop recording automatically after silence.

ALSA device notes:
  - Use "plughw:N,0" (not "hw:N,0") to enable ALSA rate conversion
  - WM8960 on Whisplay HAT registers as "wm8960soundcard" in /proc/asound/cards
  - device_name: null in config.yaml triggers auto-detection of the WM8960 card

webrtcvad frame size constraint:
  At 16000 Hz with 30ms frames: blocksize = 16000 * 30 / 1000 = 480 samples
  Each sample is 2 bytes (int16) → frame = 960 bytes
"""

from __future__ import annotations

import logging
import os
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
    Auto-detect the WM8960 sound card index from /proc/asound/cards.
    Returns a plughw device string like "plughw:1,0" or None if not found.
    """
    try:
        with open("/proc/asound/cards", "r") as f:
            for line in f:
                if "wm8960soundcard" in line.lower():
                    card_num = line.strip().split()[0]
                    device = f"plughw:{card_num},0"
                    log.info("Auto-detected WM8960 audio device: %s", device)
                    return device
    except FileNotFoundError:
        pass
    log.debug("WM8960 not found in /proc/asound/cards (normal on dev machine)")
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
        if device_name:
            self.device_name = device_name
        else:
            self.device_name = _detect_wm8960_device()   # may be None (uses system default)

        # Frame size for webrtcvad
        self.frame_size = int(sample_rate * _FRAME_MS / 1000)   # 480 at 16kHz

        self._vad = VAD(aggressiveness=vad_aggressiveness)

    def record(self, output_path: str) -> Optional[str]:
        """
        Record audio from the microphone until:
          - silence_timeout_s of continuous silence is detected (after speech starts), OR
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
        max_silent_frames = int(self.silence_timeout_s * 1000 / _FRAME_MS)

        frames: list[bytes] = []
        speech_started = False
        silent_frames = 0

        log.debug(
            "Recording: device=%s rate=%d frame_size=%d",
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
                    frame_bytes, overflowed = stream.read(self.frame_size)
                    if overflowed:
                        log.debug("Audio input overflow (non-fatal)")

                    raw = bytes(frame_bytes)
                    frames.append(raw)

                    is_speech = self._vad.is_speech(raw, self.sample_rate)

                    if is_speech:
                        speech_started = True
                        silent_frames = 0
                    elif speech_started:
                        silent_frames += 1
                        if silent_frames >= max_silent_frames:
                            log.debug(
                                "Silence detected after %d frames — stopping",
                                len(frames),
                            )
                            break

        except Exception as e:
            log.error("Recording error: %s", e)
            return None

        # Need at least a few frames of actual speech
        if not speech_started or len(frames) < 5:
            log.info("No speech detected — discarding recording")
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
