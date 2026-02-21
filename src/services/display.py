"""
Display client for PiAi Assistant.

Uses the WhisPlayBoard driver (services/display/WhisPlay.py) directly — no
sidecar process, no TCP socket. The WhisPlayBoard controls the LCD, RGB LED,
and physical button via GPIO/SPI on the Whisplay HAT.

On non-Pi environments (dev machines without RPi.GPIO / spidev), the driver
import fails gracefully and a stub is used so the rest of the code still runs.

Public interface (same as before so orchestrator.py is unchanged):
    display.connect_with_retry()          — initialises hardware
    display.send(payload: dict)           — updates LCD/LED based on status/emoji/RGB/text
    display.update_battery_level(level)   — update battery percentage shown in header (0-100)
    display.start_event_listener(cb)      — registers button press callback (GPIO interrupt)
    display.disconnect()                  — cleanup GPIO

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


def _battery_colour(level: int) -> tuple[int, int, int]:
    """
    Return an RGB colour for the battery fill based on charge level.
    Green (#55FF00) above 40%, yellow above 20%, red below 20%.
    """
    if level > 40:
        return (0x55, 0xFF, 0x00)   # Green
    elif level > 20:
        return (0xFF, 0xAA, 0x00)   # Amber
    else:
        return (0xFF, 0x22, 0x22)   # Red


def _render_battery(draw: "ImageDraw.ImageDraw", battery_level: int, image_width: int) -> None:  # type: ignore
    """
    Draw a battery icon in the top-right corner of the LCD image.
    Replicates render_battery() from the reference chatbot-ui.py.

    Icon is 26×15 px with a 3px corner radius.  A small "head" nub sits to
    the right.  The interior fill colour reflects charge level.
    """
    battery_width = 26
    battery_height = 15
    battery_margin_right = 20
    corner_radius = 3
    line_width = 2

    battery_x = image_width - battery_width - battery_margin_right
    # Align vertically with the top of the header (same as chatbot-ui.py: status_font_size // 2)
    battery_y = 8

    outline_color: tuple[int, int, int] = (255, 255, 255)
    fill_color = _battery_colour(battery_level)

    # Rounded-corner outline (4 arcs + 4 straight lines)
    draw.arc(
        (battery_x, battery_y,
         battery_x + 2 * corner_radius, battery_y + 2 * corner_radius),
        180, 270, fill=outline_color, width=line_width,
    )
    draw.arc(
        (battery_x + battery_width - 2 * corner_radius, battery_y,
         battery_x + battery_width, battery_y + 2 * corner_radius),
        270, 0, fill=outline_color, width=line_width,
    )
    draw.arc(
        (battery_x, battery_y + battery_height - 2 * corner_radius,
         battery_x + 2 * corner_radius, battery_y + battery_height),
        90, 180, fill=outline_color, width=line_width,
    )
    draw.arc(
        (battery_x + battery_width - 2 * corner_radius,
         battery_y + battery_height - 2 * corner_radius,
         battery_x + battery_width, battery_y + battery_height),
        0, 90, fill=outline_color, width=line_width,
    )

    draw.line(
        [(battery_x + corner_radius, battery_y),
         (battery_x + battery_width - corner_radius, battery_y)],
        fill=outline_color, width=line_width,
    )
    draw.line(
        [(battery_x + corner_radius, battery_y + battery_height),
         (battery_x + battery_width - corner_radius, battery_y + battery_height)],
        fill=outline_color, width=line_width,
    )
    draw.line(
        [(battery_x, battery_y + corner_radius),
         (battery_x, battery_y + battery_height - corner_radius)],
        fill=outline_color, width=line_width,
    )
    draw.line(
        [(battery_x + battery_width, battery_y + corner_radius),
         (battery_x + battery_width, battery_y + battery_height - corner_radius)],
        fill=outline_color, width=line_width,
    )

    # Fill interior with charge colour
    draw.rectangle(
        [battery_x + line_width // 2, battery_y + line_width // 2,
         battery_x + battery_width - line_width // 2,
         battery_y + battery_height - line_width // 2],
        fill=fill_color,
    )

    # Battery "head" nub on the right
    head_width = 2
    head_height = 5
    head_x = battery_x + battery_width
    head_y = battery_y + (battery_height - head_height) // 2
    draw.rectangle(
        [head_x, head_y, head_x + head_width, head_y + head_height],
        fill=(255, 255, 255),
    )

    # Battery percentage text (just the number) centred inside icon
    try:
        from PIL import ImageFont  # type: ignore
        try:
            batt_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10
            )
        except Exception:
            batt_font = ImageFont.load_default()
    except ImportError:
        return

    battery_text = str(battery_level)
    text_bbox = batt_font.getbbox(battery_text)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    text_x = battery_x + (battery_width - text_w) // 2
    text_y = battery_y + (battery_height - text_h) // 2

    # Text colour: dark on bright fill, light on dark fill
    lum = (fill_color[0] * 299 + fill_color[1] * 587 + fill_color[2] * 114) // 1000
    text_fill: tuple[int, int, int] = (0, 0, 0) if lum > 128 else (255, 255, 255)
    draw.text((text_x, text_y), battery_text, font=batt_font, fill=text_fill)


def _render_lcd(
    board: "WhisPlayBoard",  # type: ignore
    emoji: str,
    text: str,
    battery_level: Optional[int] = None,
) -> None:
    """
    Draw emoji + status text onto the 240×280 LCD using Pillow.
    If battery_level is not None, a battery icon is drawn in the top-right.
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

        # Draw battery icon in top-right (before emoji so it appears under nothing)
        if battery_level is not None:
            _render_battery(draw, battery_level, 240)

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

        # Battery level (None until first reading from BatteryMonitor)
        self._battery_level: Optional[int] = None

        # Last payload sent via send() — stored so we can re-render when battery updates
        self._last_emoji: str = ""
        self._last_text: str = ""

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

                # Remember last content so battery updates can re-render
                self._last_emoji = emoji
                self._last_text = text

                _render_lcd(self._board, emoji, text, battery_level=self._battery_level)

            except Exception as e:
                log.warning("Display send error: %s", e)

    def update_battery_level(self, level: int) -> None:
        """
        Update the battery level shown in the LCD header.
        Called by BatteryMonitor on each poll cycle.
        Thread-safe — re-renders the current LCD frame with the new level.
        """
        with self._lock:
            self._battery_level = level
            if not self._connected or self._board is None:
                return
            try:
                _render_lcd(
                    self._board,
                    self._last_emoji,
                    self._last_text,
                    battery_level=level,
                )
                log.debug("Battery icon updated: %d%%", level)
            except Exception as e:
                log.warning("Battery display update error: %s", e)

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
