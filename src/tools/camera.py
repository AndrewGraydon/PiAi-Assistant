"""
Camera capture tool for PiAi Assistant.

Captures a JPEG from the Freenove FNK0085 CSI camera using picamera2,
then passes it to the configured VisionProvider for analysis.

picamera2 is Pi-specific. On a dev machine, the import is guarded and
a graceful error string is returned.

Picamera2 instance lifecycle:
  Create → configure → start → (0.5s warm-up) → capture_array → stop → close
  A new instance is created per capture call (safest approach for infrequent calls).
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Config
    from src.services.vision import VisionProvider

log = logging.getLogger(__name__)


class CameraTool:
    def __init__(self, captures_dir: str, vision_provider: "VisionProvider") -> None:
        self.captures_dir = captures_dir
        self.vision = vision_provider

    def __call__(self, question: str = "What do you see?") -> str:
        """
        Capture an image and analyze it.
        Returns a natural-language description string.
        """
        ts = int(time.time())
        image_path = os.path.join(self.captures_dir, f"capture_{ts}.jpg")

        log.info("Camera capture requested: question=%r", question)
        captured = self._capture(image_path)
        if not captured:
            return "I tried to capture an image, but the camera is not available."

        log.info("Image captured: %s — sending to vision provider", image_path)
        return self.vision.analyze_image(image_path, question)

    def _capture(self, output_path: str) -> bool:
        """
        Capture a still image and save as JPEG.
        Returns True on success, False on failure.
        """
        try:
            from picamera2 import Picamera2
        except ImportError:
            log.warning("picamera2 not available (install with: sudo apt install python3-picamera2)")
            return False

        picam2 = None
        try:
            picam2 = Picamera2()
            config = picam2.create_still_configuration(
                main={"size": (1920, 1080)}
            )
            picam2.configure(config)
            picam2.start()
            time.sleep(0.5)     # Allow sensor auto-exposure to stabilise

            frame = picam2.capture_array()

            from PIL import Image
            img = Image.fromarray(frame)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(output_path, format="JPEG", quality=85)
            log.info("Camera saved JPEG: %s", output_path)
            return True

        except Exception as e:
            log.error("Camera capture failed: %s", e)
            return False
        finally:
            if picam2 is not None:
                try:
                    picam2.stop()
                    picam2.close()
                except Exception:
                    pass
