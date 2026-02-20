"""
Audio playback for PiAi Assistant.

Uses aplay (ALSA) to play WAV files through the WM8960 codec on the
Whisplay HAT. aplay is preferred over sounddevice for playback because it
handles the WM8960's ALSA constraints cleanly and is battle-tested on Pi.

play() blocks until playback finishes or stop() is called.
stop() terminates the current aplay subprocess immediately.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Optional

log = logging.getLogger(__name__)


class AudioPlayer:
    def __init__(self) -> None:
        self._current: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def play(self, wav_path: str) -> None:
        """
        Play a WAV file. Blocks until playback completes or stop() is called.
        Uses 'aplay -D default' so ALSA picks the correct output device.
        """
        log.debug("Playing: %s", wav_path)
        try:
            with self._lock:
                self._current = subprocess.Popen(
                    ["aplay", "-D", "default", wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._current.wait()
        except FileNotFoundError:
            log.error("aplay not found — install with: sudo apt install alsa-utils")
        except Exception as e:
            log.error("Playback error: %s", e)
        finally:
            with self._lock:
                self._current = None

    def stop(self) -> None:
        """Interrupt current playback immediately."""
        with self._lock:
            proc = self._current
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                log.debug("Playback stopped")
            except Exception as e:
                log.debug("stop() error (non-fatal): %s", e)

    def is_playing(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.poll() is None
