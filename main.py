"""
PiAi Assistant — main entry point.

Usage:
    python3 main.py
    python3 main.py --config /path/to/config.yaml
    python3 main.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PiAi Voice Assistant")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml in CWD)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level (default: INFO, or LOG_LEVEL env var)",
    )
    return parser.parse_args()


def _setup_logging(logs_dir: str, log_level: str) -> None:
    """Configure root logger: console + rotating file."""
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "assistant.log")

    level = getattr(logging, log_level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler (10 MB × 3 files)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)


def _ensure_data_dirs(paths) -> None:
    """Create runtime data directories if they don't exist."""
    for d in [paths.recordings_dir, paths.tts_dir, paths.captures_dir, paths.logs_dir]:
        os.makedirs(d, exist_ok=True)


def main() -> None:
    args = _parse_args()

    # Resolve log level: CLI flag > env var > default INFO
    log_level = (
        args.log_level
        or os.environ.get("LOG_LEVEL", "INFO")
    ).upper()

    # --- Load config (also loads .env) ---
    # Import here so dotenv is sourced before any other module reads env vars
    from src.config import load_config, ConfigError

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"[ERROR] Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Logging (needs paths.logs_dir from config) ---
    _setup_logging(config.paths.logs_dir, log_level)
    log = logging.getLogger(__name__)
    log.info("PiAi Assistant starting up (config: %s)", os.path.abspath(args.config))

    # --- Create runtime directories ---
    _ensure_data_dirs(config.paths)

    # --- Wait for NPU services ---
    from src.utils.health import wait_for_services

    try:
        wait_for_services(
            llm_host=config.llm.host,
            asr_host=config.asr.host,
            tts_host=config.tts.host,
            timeout_s=180.0,
        )
    except RuntimeError as e:
        log.error("Startup failed: %s", e)
        sys.exit(1)

    # --- Start orchestrator ---
    from src.orchestrator import Orchestrator

    orchestrator = Orchestrator(config)

    def _shutdown(signum, frame):
        log.info("Received signal %s — shutting down...", signum)
        orchestrator.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        orchestrator.run()          # blocks until shutdown
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
