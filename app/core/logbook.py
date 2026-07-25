"""Structured, human-readable run logging.

Two streams:
  * a rotating text log on the volume (``logs/collector.log``) for live view,
    search and download from the dashboard;
  * an in-memory ring buffer for cheap "tail" access in the API.

The Python ``logging`` module is also configured so library/diagnostic output
lands in the same file.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from threading import Lock

from app.config import config

_LOG_FILE = "collector.log"
_RING_MAX = 1000
_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _Ring:
    def __init__(self, maxlen: int = _RING_MAX) -> None:
        self._buf: deque[str] = deque(maxlen=maxlen)
        self._lock = Lock()

    def add(self, line: str) -> None:
        with self._lock:
            self._buf.append(line)

    def tail(self, n: int = 200, query: str | None = None) -> list[str]:
        with self._lock:
            items = list(self._buf)
        if query:
            q = query.lower()
            items = [ln for ln in items if q in ln.lower()]
        return items[-n:]


class _RingHandler(logging.Handler):
    def __init__(self, ring: _Ring) -> None:
        super().__init__()
        self.ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ring.add(self.format(record))
        except Exception:  # pragma: no cover - logging must never crash callers
            pass


ring = _Ring()
_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    config.ensure_dirs()
    logger = logging.getLogger("v2get")
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    fileh = RotatingFileHandler(
        config.logs_dir / _LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    logger.addHandler(fileh)

    streamh = logging.StreamHandler()
    streamh.setFormatter(fmt)
    logger.addHandler(streamh)

    ringh = _RingHandler(ring)
    ringh.setFormatter(fmt)
    logger.addHandler(ringh)

    logger.propagate = False
    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return setup_logging()


def log_run_summary(summary: dict) -> None:
    """Emit the block-style run summary described in the spec."""
    ts = datetime.now(timezone.utc).strftime("[%Y-%m-%d %H:%M]")
    lines = [
        ts,
        f"Channels Scanned: {summary.get('channels_scanned', 0)}",
        f"Messages Read: {summary.get('messages_read', 0)}",
        f"Configs Found: {summary.get('configs_found', 0)}",
        f"Duplicates Removed: {summary.get('duplicates_removed', 0)}",
        f"TCP Failed: {summary.get('tcp_failed', 0)}",
        f"Cooldown Skipped: {summary.get('cooldown_skipped', 0)}",
        f"Added: {summary.get('added', 0)}",
        f"Removed: {summary.get('removed', 0)}",
        f"Active Pool: {summary.get('active_pool', 0)}",
        f"GitHub Push: {summary.get('github_push', 'skipped')}",
    ]
    get_logger().info("Run summary\n" + "\n".join(lines))


def read_log_file() -> str:
    p = config.logs_dir / _LOG_FILE
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="ignore")


def search_log_file(query: str, limit: int = 500) -> list[str]:
    q = query.lower()
    lines = read_log_file().splitlines()
    matched = [ln for ln in lines if q in ln.lower()]
    return matched[-limit:]
