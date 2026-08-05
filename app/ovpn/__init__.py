"""Isolated .ovpn collection pipeline.

Detects ``.ovpn`` document attachments in the monitored Telegram channels,
validates and deduplicates them, TCP-health-checks their ``remote`` endpoint,
stores them under ``<output_dir>/ovpn`` and publishes them to the *same*
GitHub repository under a separate ``ovpn/`` path with its own push state —
producing an independent subscription link:

    https://raw.githubusercontent.com/<repo>/<branch>/[target_dir/]ovpn/index.txt

Nothing under :mod:`app.core` is touched. Own tables (private declarative
base), own settings, own channel cursors, own GitHub push hash, own worker —
so the core collector and its ``active.txt`` subscription are unaffected, and
an ovpn change never produces a commit for the main subscription.

Both feature toggles default to **off**; the module is inert until enabled.
"""
from __future__ import annotations

from app.ovpn.api import router
from app.ovpn.service import service

__all__ = ["service", "router"]
