"""
Battery monitor for PiAi Assistant.

Polls the pisugar-server daemon over TCP (port 8423) to read battery level.
The pisugar-server protocol is simple:
    Send:    "get battery\\n"
    Receive: "battery: 84.601006\\n"

The monitor runs in a daemon thread and calls a callback whenever the battery
level changes by more than UPDATE_THRESHOLD percentage points, and also on
startup so the display always shows the current level.

Gracefully handles the case where pisugar-server is not running (e.g. dev
machine without PiSugar HAT) — logs a warning and stops trying after
MAX_CONNECT_ATTEMPTS consecutive failures.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# How often to poll pisugar-server (seconds)
_DEFAULT_POLL_INTERVAL_S = 30

# Stop retrying after this many consecutive connection failures
_MAX_CONNECT_ATTEMPTS = 5

# Only fire the callback if the level changes by this many percentage points
_UPDATE_THRESHOLD = 2

# TCP timeout for each connection to pisugar-server
_SOCKET_TIMEOUT_S = 5.0


def _get_battery_level(host: str, port: int) -> Optional[float]:
    """
    Open a TCP connection to pisugar-server, send "get battery\\n",
    parse the "battery: N.N" response, and return the level as a float.

    Returns None on any error.
    """
    try:
        with socket.create_connection((host, port), timeout=_SOCKET_TIMEOUT_S) as s:
            s.sendall(b"get battery\n")
            data = s.recv(256).decode("utf-8", errors="replace").strip()
        # Expected format: "battery: 84.601006"
        if data.startswith("battery:"):
            raw = data.split(":", 1)[1].strip()
            return float(raw)
        log.debug("Unexpected pisugar response: %r", data)
        return None
    except (OSError, ValueError) as e:
        log.debug("pisugar battery read failed: %s", e)
        return None


class BatteryMonitor:
    """
    Background thread that polls pisugar-server and fires a callback with
    the current battery level (integer percentage 0-100).

    Usage:
        monitor = BatteryMonitor(
            callback=lambda level: display.update_battery(level),
            host="127.0.0.1",
            port=8423,
            poll_interval_s=30,
        )
        monitor.start()
        # ... later:
        monitor.stop()
    """

    def __init__(
        self,
        callback: Callable[[int], None],
        host: str = "127.0.0.1",
        port: int = 8423,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._callback = callback
        self._host = host
        self._port = port
        self._poll_interval_s = poll_interval_s

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_reported: Optional[int] = None

    def start(self) -> None:
        """Start the background polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="battery-monitor",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "Battery monitor started (polling %s:%d every %ds)",
            self._host, self._port, self._poll_interval_s,
        )

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()

    def _run(self) -> None:
        """Main polling loop."""
        consecutive_failures = 0
        first_poll = True

        while not self._stop_event.is_set():
            raw = _get_battery_level(self._host, self._port)

            if raw is None:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    log.warning(
                        "Cannot reach pisugar-server at %s:%d — "
                        "battery gauge unavailable (will retry)",
                        self._host, self._port,
                    )
                if consecutive_failures >= _MAX_CONNECT_ATTEMPTS:
                    log.warning(
                        "pisugar-server unreachable after %d attempts — "
                        "battery monitor giving up",
                        consecutive_failures,
                    )
                    return
            else:
                consecutive_failures = 0
                level = max(0, min(100, int(round(raw))))

                # Always fire on first successful read; thereafter only on change
                if first_poll or (
                    self._last_reported is None
                    or abs(level - self._last_reported) >= _UPDATE_THRESHOLD
                ):
                    self._last_reported = level
                    first_poll = False
                    log.debug("Battery level: %d%%", level)
                    try:
                        self._callback(level)
                    except Exception:
                        log.exception("Battery callback raised an exception")

            self._stop_event.wait(timeout=self._poll_interval_s)
