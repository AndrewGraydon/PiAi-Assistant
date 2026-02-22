"""
Display client for PiAi Assistant.

Uses the WhisPlayBoard driver (services/display/WhisPlay.py) directly -- no
sidecar process, no TCP socket. The WhisPlayBoard controls the LCD, RGB LED,
and physical button via GPIO/SPI on the Whisplay HAT.

On non-Pi environments (dev machines without RPi.GPIO / spidev), the driver
import fails gracefully and a stub is used so the rest of the code still runs.

LCD layout (240x280, with 20px corner inset):
    Header (98px):  status text (24pt) + battery icon (row 1, inset for corners)
                    emoji (40pt) centered (row 2)
    Text area (182px): word-wrapped response text (20pt), auto-scrolling

Public interface:
    display.connect_with_retry()          -- initialises hardware + render thread
    display.send(payload: dict)           -- updates header (emoji/status/RGB/LED)
    display.set_response_text(text, speed, follow_tail) -- set scrolling response text
    display.update_battery_level(level)   -- update battery percentage (0-100)
    display.start_event_listener(cb)      -- registers button press callback
    display.disconnect()                  -- cleanup GPIO + stop render thread
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate the WhisPlay driver relative to this file:
#   src/services/display.py  ->  ../../services/display/WhisPlay.py
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

# ---------------------------------------------------------------------------
# Try to import Pillow and numpy at module level
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants — matched to original chatbot-ui.py aesthetics
# ---------------------------------------------------------------------------
LCD_WIDTH = 240
LCD_HEIGHT = 280
CORNER_INSET = 20           # LCD has rounded corners; inset content from edges
HEADER_HEIGHT = 98           # 88px content + 10px bottom margin (matches original)
TEXT_AREA_Y = HEADER_HEIGHT
TEXT_AREA_HEIGHT = LCD_HEIGHT - HEADER_HEIGHT  # 182px

# Font preference: NotoSans-Bold (matches original chatbot) > DejaVuSans-Bold > DejaVuSans
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_STATUS_FONT_SIZE = 24       # matches original
_EMOJI_FONT_SIZE = 40        # matches original
_TEXT_FONT_SIZE = 20          # matches original
_TEXT_PADDING_X = 10
_TEXT_MAX_WIDTH = LCD_WIDTH - 2 * _TEXT_PADDING_X  # 220px

_RENDER_FPS = 30
_RENDER_INTERVAL = 1.0 / _RENDER_FPS


# ---------------------------------------------------------------------------
# Font cache (loaded once)
# ---------------------------------------------------------------------------
_resolved_font_path: Optional[str] = None


def _find_font_path() -> str:
    """Find the best available font on this system."""
    global _resolved_font_path
    if _resolved_font_path:
        return _resolved_font_path
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            _resolved_font_path = path
            log.info("Display font: %s", path)
            return path
    _resolved_font_path = _FONT_CANDIDATES[-1]  # fallback
    return _resolved_font_path


def _load_font(size: int) -> "ImageFont.FreeTypeFont":
    try:
        return ImageFont.truetype(_find_font_path(), size)
    except Exception:
        return ImageFont.load_default()


_fonts: dict[int, "ImageFont.FreeTypeFont"] = {}


def _get_font(size: int) -> "ImageFont.FreeTypeFont":
    if size not in _fonts:
        _fonts[size] = _load_font(size)
    return _fonts[size]


# Separate font for emoji glyphs (DejaVuSans has emoji support; NotoSans-Bold does not)
_EMOJI_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_emoji_fonts: dict[int, "ImageFont.FreeTypeFont"] = {}


def _get_emoji_font(size: int) -> "ImageFont.FreeTypeFont":
    if size not in _emoji_fonts:
        try:
            _emoji_fonts[size] = ImageFont.truetype(_EMOJI_FONT_PATH, size)
        except Exception:
            _emoji_fonts[size] = _get_font(size)
    return _emoji_fonts[size]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_colour: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' to (r, g, b) tuple."""
    hex_colour = hex_colour.lstrip("#")
    r = int(hex_colour[0:2], 16)
    g = int(hex_colour[2:4], 16)
    b = int(hex_colour[4:6], 16)
    return r, g, b


def _battery_colour(level: int) -> tuple[int, int, int]:
    if level > 40:
        return (0x55, 0xFF, 0x00)
    elif level > 20:
        return (0xFF, 0xAA, 0x00)
    else:
        return (0xFF, 0x22, 0x22)


def _image_to_rgb565(img: "Image.Image") -> list[int]:
    """Convert a Pillow RGB image to RGB565 byte list using numpy."""
    rgb = img.convert("RGB")
    if _NUMPY_AVAILABLE:
        arr = np.array(rgb, dtype=np.uint16)
        r = (arr[:, :, 0] & 0xF8) << 8
        g = (arr[:, :, 1] & 0xFC) << 3
        b = arr[:, :, 2] >> 3
        rgb565 = r | g | b
        high = (rgb565 >> 8).astype(np.uint8)
        low = (rgb565 & 0xFF).astype(np.uint8)
        return np.dstack((high, low)).flatten().tolist()
    else:
        w, h = rgb.size
        pixel_data: list[int] = []
        for py in range(h):
            for px in range(w):
                rv, gv, bv = rgb.getpixel((px, py))
                val = ((rv & 0xF8) << 8) | ((gv & 0xFC) << 3) | (bv >> 3)
                pixel_data.extend([(val >> 8) & 0xFF, val & 0xFF])
        return pixel_data


def _wrap_text(text: str, font: "ImageFont.FreeTypeFont", max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels, using font metrics."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        test = current + " " + word
        bbox = font.getbbox(test)
        if (bbox[2] - bbox[0]) <= max_width:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Rendering functions
# ---------------------------------------------------------------------------

def _render_battery(draw: "ImageDraw.ImageDraw", battery_level: int, image_width: int) -> None:
    battery_width = 26
    battery_height = 15
    battery_margin_right = CORNER_INSET
    corner_radius = 3
    line_width = 2

    battery_x = image_width - battery_width - battery_margin_right
    battery_y = _STATUS_FONT_SIZE // 2

    outline_color: tuple[int, int, int] = (255, 255, 255)
    fill_color = _battery_colour(battery_level)

    # Rounded-corner outline
    draw.arc((battery_x, battery_y, battery_x + 2 * corner_radius, battery_y + 2 * corner_radius), 180, 270, fill=outline_color, width=line_width)
    draw.arc((battery_x + battery_width - 2 * corner_radius, battery_y, battery_x + battery_width, battery_y + 2 * corner_radius), 270, 0, fill=outline_color, width=line_width)
    draw.arc((battery_x, battery_y + battery_height - 2 * corner_radius, battery_x + 2 * corner_radius, battery_y + battery_height), 90, 180, fill=outline_color, width=line_width)
    draw.arc((battery_x + battery_width - 2 * corner_radius, battery_y + battery_height - 2 * corner_radius, battery_x + battery_width, battery_y + battery_height), 0, 90, fill=outline_color, width=line_width)

    draw.line([(battery_x + corner_radius, battery_y), (battery_x + battery_width - corner_radius, battery_y)], fill=outline_color, width=line_width)
    draw.line([(battery_x + corner_radius, battery_y + battery_height), (battery_x + battery_width - corner_radius, battery_y + battery_height)], fill=outline_color, width=line_width)
    draw.line([(battery_x, battery_y + corner_radius), (battery_x, battery_y + battery_height - corner_radius)], fill=outline_color, width=line_width)
    draw.line([(battery_x + battery_width, battery_y + corner_radius), (battery_x + battery_width, battery_y + battery_height - corner_radius)], fill=outline_color, width=line_width)

    # Fill interior
    draw.rectangle([battery_x + line_width // 2, battery_y + line_width // 2, battery_x + battery_width - line_width // 2, battery_y + battery_height - line_width // 2], fill=fill_color)

    # Battery head nub
    head_width = 2
    head_height = 5
    head_x = battery_x + battery_width
    head_y = battery_y + (battery_height - head_height) // 2
    draw.rectangle([head_x, head_y, head_x + head_width, head_y + head_height], fill=(255, 255, 255))

    # Battery percentage text
    batt_font = _get_font(13)
    battery_text = str(battery_level)
    text_bbox = batt_font.getbbox(battery_text)
    text_w = text_bbox[2] - text_bbox[0]
    ascent, descent = batt_font.getmetrics()
    text_x = battery_x + (battery_width - text_w) // 2
    text_y = battery_y + (battery_height - (ascent + descent)) // 2

    lum = (fill_color[0] * 299 + fill_color[1] * 587 + fill_color[2] * 114) // 1000
    text_fill: tuple[int, int, int] = (0, 0, 0) if lum > 128 else (255, 255, 255)
    draw.text((text_x, text_y), battery_text, font=batt_font, fill=text_fill)


def _render_header(
    board: "WhisPlayBoard",  # type: ignore
    emoji: str,
    status_text: str,
    battery_level: Optional[int],
) -> None:
    """Draw header: row 1 = status text + battery, row 2 = emoji centered."""
    if not _PIL_AVAILABLE:
        return

    img = Image.new("RGB", (LCD_WIDTH, HEADER_HEIGHT), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    status_font = _get_font(_STATUS_FONT_SIZE)
    emoji_font = _get_emoji_font(_EMOJI_FONT_SIZE)

    # Row 1: status text inset from rounded corner, battery top-right
    # Clamp width so text doesn't overlap the battery icon area
    if status_text:
        # battery_width(26) + margin_right(20) + gap(8) = 54px reserved
        max_status_w = LCD_WIDTH - CORNER_INSET - 54
        bbox = status_font.getbbox(status_text)
        if (bbox[2] - bbox[0]) > max_status_w:
            while len(status_text) > 1:
                status_text = status_text[:-1]
                bbox = status_font.getbbox(status_text + "...")
                if (bbox[2] - bbox[0]) <= max_status_w:
                    status_text += "..."
                    break
        draw.text((CORNER_INSET, 0), status_text, font=status_font, fill=(255, 255, 255))

    if battery_level is not None:
        _render_battery(draw, battery_level, LCD_WIDTH)

    # Row 2: emoji centered, below status text (DejaVuSans for emoji glyphs)
    if emoji:
        emoji_y = _STATUS_FONT_SIZE + 8
        bbox = draw.textbbox((0, 0), emoji, font=emoji_font)
        emoji_w = bbox[2] - bbox[0]
        ex = (LCD_WIDTH - emoji_w) // 2
        draw.text((ex, emoji_y), emoji, font=emoji_font, fill=(255, 255, 255))

    board.draw_image(0, 0, LCD_WIDTH, HEADER_HEIGHT, _image_to_rgb565(img))


def _render_text_area(
    board: "WhisPlayBoard",  # type: ignore
    text: str,
    scroll_offset: int = 0,
) -> None:
    """Draw word-wrapped text into the text area region, offset by scroll_offset."""
    if not _PIL_AVAILABLE:
        return

    img = Image.new("RGB", (LCD_WIDTH, TEXT_AREA_HEIGHT), color=(0, 0, 0))

    if not text:
        board.draw_image(0, TEXT_AREA_Y, LCD_WIDTH, TEXT_AREA_HEIGHT, _image_to_rgb565(img))
        return

    draw = ImageDraw.Draw(img)
    text_font = _get_font(_TEXT_FONT_SIZE)

    lines = _wrap_text(text, text_font, _TEXT_MAX_WIDTH)
    ascent, descent = text_font.getmetrics()
    line_height = ascent + descent + 4

    y = -scroll_offset
    for line in lines:
        if y + line_height > 0 and y < TEXT_AREA_HEIGHT:
            draw.text((_TEXT_PADDING_X, y), line, font=text_font, fill=(255, 255, 255))
        y += line_height
        if y >= TEXT_AREA_HEIGHT + line_height:
            break

    board.draw_image(0, TEXT_AREA_Y, LCD_WIDTH, TEXT_AREA_HEIGHT, _image_to_rgb565(img))


# ---------------------------------------------------------------------------
# DisplayClient
# ---------------------------------------------------------------------------

class DisplayClient:
    """
    Controls the Whisplay HAT LCD, RGB LED, and button directly via
    WhisPlayBoard.
    """

    def __init__(self, host: str = "", port: int = 0) -> None:
        self._board: Optional["WhisPlayBoard"] = None  # type: ignore
        self._button_callback: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()
        self._connected = False

        # Battery level (None until first reading from BatteryMonitor)
        self._battery_level: Optional[int] = None

        # Header state
        self._last_emoji: str = ""
        self._last_status_text: str = ""

        # Scrolling text state
        self._response_text: str = ""
        self._scroll_offset: int = 0
        self._scroll_speed: int = 0  # pixels per frame (0 = no scrolling)
        self._follow_tail: bool = False  # True during streaming — snap to bottom
        self._total_text_height: int = 0

        # Render thread
        self._render_thread: Optional[threading.Thread] = None
        self._render_stop = threading.Event()
        self._render_wake = threading.Event()
        self._header_dirty = False
        self._text_dirty = False  # text area needs immediate redraw

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect_with_retry(
        self, max_retries: int = 3, retry_interval_s: float = 1.0
    ) -> None:
        """Initialise the WhisPlayBoard hardware and start render thread."""
        if not _WHISPLAY_AVAILABLE:
            log.warning(
                "WhisPlayBoard not available -- display will be no-op (off-Pi mode)"
            )
            self._connected = False
            return

        for attempt in range(1, max_retries + 1):
            try:
                self._board = WhisPlayBoard()
                self._board.set_backlight(80)
                self._connected = True
                log.info("WhisPlayBoard initialised (attempt %d/%d)", attempt, max_retries)

                # Start background render thread
                self._render_stop.clear()
                self._render_thread = threading.Thread(
                    target=self._render_loop, daemon=True, name="lcd-render"
                )
                self._render_thread.start()
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
                time.sleep(retry_interval_s)

    def disconnect(self) -> None:
        """Clean up GPIO resources and stop render thread."""
        self._connected = False

        self._render_stop.set()
        self._render_wake.set()
        if self._render_thread and self._render_thread.is_alive():
            self._render_thread.join(timeout=2.0)

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
    # Render thread
    # ------------------------------------------------------------------

    def _render_loop(self) -> None:
        """Background render loop at 30fps. Sleeps when nothing to animate."""
        while not self._render_stop.is_set():
            needs_animation = self._scroll_speed > 0 or self._text_dirty
            self._render_wake.wait(timeout=_RENDER_INTERVAL if needs_animation else 5.0)
            self._render_wake.clear()

            if self._render_stop.is_set():
                break
            if not self._connected or self._board is None:
                continue

            with self._lock:
                try:
                    # Redraw header if battery changed
                    if self._header_dirty:
                        _render_header(
                            self._board,
                            self._last_emoji,
                            self._last_status_text,
                            self._battery_level,
                        )
                        self._header_dirty = False

                    # Redraw text area if dirty (streaming update) or scrolling
                    if self._text_dirty:
                        _render_text_area(
                            self._board,
                            self._response_text,
                            self._scroll_offset,
                        )
                        self._text_dirty = False

                    elif self._scroll_speed > 0 and self._response_text:
                        max_scroll = max(0, self._total_text_height - TEXT_AREA_HEIGHT)
                        if self._scroll_offset < max_scroll:
                            self._scroll_offset += self._scroll_speed
                            if self._scroll_offset > max_scroll:
                                self._scroll_offset = max_scroll
                            _render_text_area(
                                self._board,
                                self._response_text,
                                self._scroll_offset,
                            )
                            self._render_wake.set()  # keep scrolling

                except Exception as e:
                    log.debug("Render loop error: %s", e)

    def _compute_text_height(self, text: str) -> int:
        """Compute total pixel height of word-wrapped text."""
        if not _PIL_AVAILABLE or not text:
            return 0
        font = _get_font(_TEXT_FONT_SIZE)
        lines = _wrap_text(text, font, _TEXT_MAX_WIDTH)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + 4
        return len(lines) * line_height

    # ------------------------------------------------------------------
    # Display updates
    # ------------------------------------------------------------------

    def send(self, payload: dict) -> None:
        """
        Update header display and RGB LED based on payload. Thread-safe.

        Understood keys:
            status     -- idle | recording | transcribing | thinking | speaking
            emoji      -- unicode emoji string
            text       -- status text shown in header row 1
            RGB        -- hex colour string for RGB LED
            brightness -- backlight brightness 0-100
        """
        if not self._connected or self._board is None:
            log.debug("Display send (no-op): %s", payload)
            return

        with self._lock:
            try:
                brightness = payload.get("brightness", 80)
                self._board.set_backlight(int(brightness))

                # RGB LED -- fade in background thread to avoid blocking
                rgb_hex = payload.get("RGB")
                if rgb_hex:
                    r, g, b = _hex_to_rgb(rgb_hex)
                    threading.Thread(
                        target=self._fade_rgb, args=(r, g, b),
                        daemon=True, name="rgb-fade",
                    ).start()

                emoji = payload.get("emoji", "")
                text = payload.get("text", "")

                if emoji:
                    self._last_emoji = emoji
                if text:
                    self._last_status_text = text

                _render_header(
                    self._board,
                    self._last_emoji,
                    self._last_status_text,
                    self._battery_level,
                )

                # If no response text, clear the text area
                if not self._response_text:
                    _render_text_area(self._board, "", 0)

            except Exception as e:
                log.warning("Display send error: %s", e)

    def set_response_text(
        self, text: str, scroll_speed: int = 3, follow_tail: bool = False
    ) -> None:
        """
        Set the response text shown in the text area.

        follow_tail=True: streaming mode — auto-scroll to show the latest text
                          at the bottom (used during LLM generation).
        follow_tail=False: normal mode — scroll from top to bottom after
                           generation is complete (used during TTS playback).
        Call with empty string to clear the text area.
        """
        if not self._connected or self._board is None:
            return

        with self._lock:
            self._response_text = text
            self._follow_tail = follow_tail

            if text:
                self._total_text_height = self._compute_text_height(text)

                if follow_tail:
                    # Streaming: snap to bottom so latest text is visible
                    self._scroll_speed = 0
                    max_scroll = max(0, self._total_text_height - TEXT_AREA_HEIGHT)
                    self._scroll_offset = max_scroll
                else:
                    # Normal: start from top, scroll if text overflows
                    self._scroll_offset = 0
                    self._scroll_speed = scroll_speed if self._total_text_height > TEXT_AREA_HEIGHT else 0

                # Mark text area as dirty so render thread redraws it
                self._text_dirty = True
                self._render_wake.set()
            else:
                self._scroll_speed = 0
                self._total_text_height = 0
                self._scroll_offset = 0
                self._follow_tail = False
                _render_text_area(self._board, "", 0)

    def update_battery_level(self, level: int) -> None:
        """
        Update the battery level shown in the LCD header.
        Called by BatteryMonitor on each poll cycle.
        """
        with self._lock:
            self._battery_level = level
            self._header_dirty = True
        self._render_wake.set()

    # ------------------------------------------------------------------
    # RGB LED
    # ------------------------------------------------------------------

    def _fade_rgb(self, r: int, g: int, b: int) -> None:
        """Fade the RGB LED to a target colour. Runs in a background thread."""
        if not self._board:
            return
        try:
            self._board.set_rgb_fade(r, g, b, duration_ms=300)
        except Exception:
            try:
                self._board.set_rgb(r, g, b)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Button event listener
    # ------------------------------------------------------------------

    def start_event_listener(self, button_callback: Callable[[], None]) -> None:
        """
        Register the button press callback. The WhisPlayBoard uses GPIO
        interrupts so no polling thread is needed.
        """
        self._button_callback = button_callback

        if not self._connected or self._board is None:
            log.warning(
                "Display not connected -- button events will not be received"
            )
            return

        def _on_press() -> None:
            log.debug("Button pressed (GPIO interrupt)")
            if self._button_callback:
                threading.Thread(
                    target=self._button_callback,
                    daemon=True,
                    name="btn-handler",
                ).start()

        self._board.on_button_press(_on_press)
        log.info("Button event listener registered (GPIO interrupt mode)")
