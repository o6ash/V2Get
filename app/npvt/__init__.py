"""Isolated .npvt processing pipeline.

A fully self-contained module that detects ``.npvt`` document attachments in the
monitored Telegram channels, relays each file to an external Telegram bot
(``@DickiriptorBot`` by default), drives the bot's inline keyboard to extract
the V2Ray links it returns, and injects those links into the *existing* v2get
pipeline — reusing the parser, fingerprint, blacklist, cooldown, pool and
subscription machinery without modifying any of it.

Nothing under :mod:`app.core` is touched. The package keeps its own settings,
its own ORM tables (on a private declarative base) and its own background
worker, and every external failure is contained so the core collector keeps
running regardless of the state of this feature.

The public surface is the :data:`service` singleton (see :mod:`app.npvt.service`)
and the :data:`router` (see :mod:`app.npvt.api`).
"""
from __future__ import annotations

from app.npvt.api import router
from app.npvt.service import service

__all__ = ["service", "router"]
