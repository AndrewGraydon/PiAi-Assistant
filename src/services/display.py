"""
Display client for PiAi Assistant.

Uses the WhisPlayBoard driver (services/display/WhisPlay.py) directly — no
sidecar process, no TCP socket. The WhisPlayBoard controls the LCD, RGB LED,
and physical button via GPIO/SPI on the Whisplay HAT.

On non-Pi environments (dev machines without RPi.GPIO / spidev), the driver
import fails gracefully and a stub is used so the rest of the code still runs.

Public interface (same as before so orchestrator.py is unchanged):
    display.connect_with_retry()       — initialises hardware
    display.send(payload: dict)        — updates LCD/LED based on status/emoji/RGB/text
    display.start_event_listener(cb)   — registers button press callback (GPIO interrupt)
    display.disconnect()               — cleanup GPIO

Payload keys understood by send():
    status      — one of: idle | recording | transcribing | thinking | speaking
    emoji       — displayed as large text in the centre of the LCD
    text        — smaller status text shown below the emoji
    RGB         — hex colour string e.g. "#ff6800" for the RGB LED
    brightness  — backlight brightness 0–100
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate the WhisPlay driver relative to this file:
#   src/services/display.py  →  ../../services/display/WhisPlay.py
# ---------------------------------------------------------------------------
_DRIVER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "display")
)

_WHISPLAY_AVAILABLE = False
WhisPlayBoard = None  # type: ignore

if os.path.isdir(_DRIVER_DIR):
    if _DRIVER_DIR not in sys.path:
        sys.path.insert(0, _DRIVER_DIR)
    try:
        from WhisPlay import WhisPlayBoard  # type: ignore
        _WHISPLAY_AVAILABLE = True
        log.debug("WhisPlayBoard driver loaded from %s", _DRIVER_DIR)
    except ImportError as e:
        log.warning("WhisPlayBoard not available (running off-Pi?): %s", e)


def _hex_to_rgb(hex_colour: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' to (r, g, b) tuple."""
    hex_colour = hex_colour.lstrip("#")
    r = int(hex_colour[0:2], 16)
    g = int(hex_colour[2:4], 16)
    b = int(hex_colour[4:6], 16)
    return r, g, b


def _render_lcd(board: "WhisPlayBoard", emoji: str, text: str) -> None:  # type: ignore
    """
    Draw emoji + status text onto the 240×280 LCD using Pillow.
    Falls back to fill_screen if Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore

        img = Image.new("RGB", (240, 280), color=(0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Emoji — large, centred in upper portion
        try:
            emoji_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72)
        except Exception:
            emoji_font = ImageFont.load_default()

        # Text — smaller, centred in lower portion
        try:
            text_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        except Exception:
            text_font = ImageFont.load_default()

        # Draw emoji
        bbox = draw.textbbox((0, 0), emoji, font=emoji_font)
        ex = (240 - (bbox[2] - bbox[0])) // 2
        draw.text((ex, 60), emoji, font=emoji_font, fill=(255, 255, 255))

        # Draw status text (word-wrap at 22 chars)
        if text:
            words = text.split()
            lines: list[str] = []
            current = ""
            for word in words:
                if len(current) + len(word) + 1 <= 22:
                    current = (current + " " + word).strip()
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)

            y = 175
            for line in lines[:3]:
                bbox = draw.textbbox((0, 0), line, font=text_font)
                tx = (240 - (bbox[2] - bbox[0])) // 2
                draw.text((tx, y), line, font=text_font, fill=(200, 200, 200))
                y += 30

        # Convert to RGB565
        img_rgb = img.convert("RGB")
        pixel_data: list[int] = []
        for py in range(280):
            for px in range(240):
                r, g, b = img_rgb.getpixel((px, py))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                pixel_data.extend([(rgb565 >> 8) & 0xFF, rgb565 & 0xFF])

        board.draw_image(0, 0, 240, 280, pixel_data)

    except Exception as exc:
        log.debug("LCD render failed (%s), using fill_screen fallback", exc)
        board.fill_screen(0x0000)


class DisplayClient:
    """
    Controls the Whisplay HAT LCD, RGB LED, and button directly via
    WhisPlayBoard. Presents the same interface as the old TCP socket client.
    """

    def __init__(self, host: str = "", port: int = 0) -> None:
        # host/port kept for API compatibility but not used
        self._board: Optional["WhisPlayBoard"] = None  # type: ignore
        self._button_callback: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()
        self._connected = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect_with_retry(
        self, max_retries: int = 3, retry_interval_s: float = 1.0
    ) -> None:
        """Initialise the WhisPlayBoard hardware."""
        if not _WHISPLAY_AVAILABLE:
            log.warning(
                "WhisPlayBoard not available — display will be no-op (off-Pi mode)"
            )
            self._connected = False
            return

        for attempt in range(1, max_retries + 1):
            try:
                self._board = WhisPlayBoard()
                self._board.set_backlight(80)
                self._connected = True
                log.info("WhisPlayBoard initialised (attempt %d/%d)", attempt, max_retries)
                return
            except Exception as e:
                if attempt == max_retries:
                    log.error(
                        "Failed to initialise WhisPlayBoard after %d attempts: %s",
                        max_retries, e,
                    )
                    self._connected = False
                    return
                log.debug(
                    "WhisPlayBoard init failed (%s), retry %d/%d",
                    e, attempt, max_retries,
                )
                import time
                time.sleep(retry_interval_s)

    def disconnect(self) -> None:
        """Clean up GPIO resources."""
        self._connected = False
        if self._board:
            try:
                self._board.set_backlight(0)
                self._board.fill_screen(0x0000)
                self._board.set_rgb(0, 0, 0)
                self._board.cleanup()
            except Exception as e:
                log.debug("Display cleanup error: %s", e)
            self._board = None

    # ------------------------------------------------------------------
    # Display updates
    # ------------------------------------------------------------------

    def send(self, payload: dict) -> None:
        """
        Update the display based on the payload. Thread-safe.

        Understood keys:
            status     — idle | recording | transcribing | thinking | speaking
            emoji      — unicode emoji string
            text       — status text
            RGB        — hex colour string for RGB LED
            brightness — backlight brightness 0–100
        """
        if not self._connected or self._board is None:
            log.debug("Display send (no-op): %s", payload)
            return

        with self._lock:
            try:
                brightness = payload.get("brightness", 80)
                self._board.set_backlight(int(brightness))

                rgb_hex = payload.get("RGB", "#000000")
                r, g, b = _hex_to_rgb(rgb_hex)
                self._board.set_rgb(r, g, b)

                emoji = payload.get("emoji", "")
                text = payload.get("text", "")
                _render_lcd(self._board, emoji, text)

            except Exception as e:
                log.warning("Display send error: %s", e)

    # ------------------------------------------------------------------
    # Button event listener
    # ------------------------------------------------------------------

    def start_event_listener(self, button_callback: Callable[[], None]) -> None:
        """
        Register the button press callback. The WhisPlayBoard uses GPIO
        interrupts so no polling thread is needed — the callback is fired
        directly from the GPIO interrupt handler in a new thread.
        """
        self._button_callback = button_callback

        if not self._connected or self._board is None:
            log.warning(
                "Display not connected — button events will not be received"
            )
            return

        def _on_press() -> None:
            log.debug("Button pressed (GPIO interrupt)")
            if self._button_callback:
                # Run in a new thread to avoid blocking the GPIO interrupt handler
                threading.Thread(
                    target=self._button_callback,
                    daemon=True,
                    name="btn-handler",
                ).start()

        self._board.on_button_press(_on_press)
        log.info("Button event listener registered (GPIO interrupt mode)")
