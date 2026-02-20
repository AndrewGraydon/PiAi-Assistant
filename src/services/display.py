"""
Display socket client for PiAi Assistant.

Communicates with the chatbot-ui.py sidecar (from the whisplay-ai-chatbot-llm8850
reference repo) via a TCP socket on localhost:12345. The protocol is newline-delimited JSON.

Messages TO display: JSON dicts with keys like status, emoji, text, RGB, brightness, etc.
Events FROM display: {"event": "button_pressed"} | {"event": "button_released"}

Design notes:
- send() is fire-and-forget (no ACK wait) — the chatbot-ui.py sidecar does send "OK\n"
  but we don't block on it; display updates are async
- Button debouncing is handled by chatbot-ui.py's GPIO layer; we only act on button_pressed
- The receive loop runs as a daemon thread and calls the registered button callback
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


class DisplayClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

        self._socket: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._recv_thread: Optional[threading.Thread] = None
        self._button_callback: Optional[Callable[[], None]] = None
        self._buffer = ""
        self._connected = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect_with_retry(
        self, max_retries: int = 15, retry_interval_s: float = 3.0
    ) -> None:
        """
        Try to connect to the display sidecar socket.
        Retries up to max_retries times with retry_interval_s seconds between attempts.
        Raises RuntimeError if all attempts fail.
        """
        for attempt in range(1, max_retries + 1):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((self.host, self.port))
                sock.settimeout(None)           # back to blocking for recv
                self._socket = sock
                self._connected = True
                log.info("Display socket connected (attempt %d/%d)", attempt, max_retries)
                return
            except (ConnectionRefusedError, OSError) as e:
                if attempt == max_retries:
                    raise RuntimeError(
                        f"Cannot connect to display sidecar at {self.host}:{self.port} "
                        f"after {max_retries} attempts"
                    ) from e
                log.debug(
                    "Display not ready (%s), retry %d/%d in %.1fs",
                    e, attempt, max_retries, retry_interval_s,
                )
                time.sleep(retry_interval_s)

    def disconnect(self) -> None:
        self._connected = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    # ------------------------------------------------------------------
    # Sending display updates
    # ------------------------------------------------------------------

    def send(self, payload: dict) -> None:
        """
        Send a display update message. Thread-safe, fire-and-forget.
        Silently drops messages if not connected.
        """
        if not self._connected or self._socket is None:
            return
        with self._lock:
            try:
                data = json.dumps(payload, ensure_ascii=False) + "\n"
                self._socket.sendall(data.encode("utf-8"))
            except Exception as e:
                log.warning("Display send error: %s", e)
                self._connected = False

    # ------------------------------------------------------------------
    # Button event listener
    # ------------------------------------------------------------------

    def start_event_listener(self, button_callback: Callable[[], None]) -> None:
        """
        Start a daemon thread that receives events from the display sidecar.
        button_callback is called (from the listener thread) when a button_pressed
        event arrives.
        """
        self._button_callback = button_callback
        self._recv_thread = threading.Thread(
            target=self._recv_loop, name="display-recv", daemon=True
        )
        self._recv_thread.start()

    def _recv_loop(self) -> None:
        """
        Receive newline-delimited JSON from chatbot-ui.py.
        Handles TCP fragmentation by accumulating into a buffer.
        """
        while self._connected and self._socket is not None:
            try:
                chunk = self._socket.recv(4096)
                if not chunk:
                    log.warning("Display socket closed by remote")
                    self._connected = False
                    break
                self._buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    line = line.strip()
                    if not line or line == "OK":
                        continue
                    self._handle_event(line)

            except OSError as e:
                if self._connected:
                    log.warning("Display recv error: %s", e)
                break

    def _handle_event(self, raw: str) -> None:
        """Parse a single JSON event line and dispatch."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        event = msg.get("event")
        if event == "button_pressed":
            log.debug("Display event: button_pressed")
            if self._button_callback:
                # Call in a new thread so the recv loop isn't blocked
                threading.Thread(
                    target=self._button_callback, daemon=True, name="btn-handler"
                ).start()
        elif event == "button_released":
            log.debug("Display event: button_released")
        else:
            log.debug("Display event (unhandled): %s", msg)
