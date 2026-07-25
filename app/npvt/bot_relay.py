"""Drive the external Telegram bot that turns a .npvt file into V2Ray links.

Flow (all over the *shared* user session, so no second login is needed):

    send .npvt  ->  wait for the bot's inline keyboard  ->  click the button
    whose text matches a configured pattern (fallback: the 2nd button)  ->
    collect every V2Ray link the bot returns, across however many message
    batches it sends, until a quiet period or the collection window elapses.

Everything is polling-based (``get_messages``) rather than event/conversation
based: it is robust to whatever order the bot replies in, tolerant of edits vs
new messages, and safe to run concurrently with the core collector on the same
Telethon client. Every failure raises :class:`RelayError`; the worker turns
that into a contained, retryable job failure — the core system is unaffected.
"""
from __future__ import annotations

import asyncio
import io
import unicodedata
from dataclasses import dataclass, field

from app.core.logbook import get_logger
from app.core.parser import extract_links
from app.core.telegram_client import collector_client
from app.npvt.config import npvt_settings

log = get_logger()

try:
    from telethon.tl.types import DocumentAttributeFilename
    _TELETHON = True
except ImportError:  # pragma: no cover
    _TELETHON = False

try:
    # Raised by ``button.click()`` when the bot is slow to *answer* the callback
    # query — the click itself still went through, so we must not treat it as a
    # failure (the links typically arrive moments later).
    from telethon.errors import BotResponseTimeoutError as _BotClickTimeout
except Exception:  # pragma: no cover - telethon absent or symbol moved
    class _BotClickTimeout(Exception):
        ...


class RelayError(RuntimeError):
    """A recoverable failure during bot relay (timeout, no buttons, etc.)."""


class RelayUnavailable(RelayError):
    """The Telegram session is not configured/authorized — relay can't run."""


class CaptchaRequired(RelayError):
    """The bot issued a CAPTCHA challenge. We do not solve it — the worker
    trips its circuit breaker and backs off (see app.npvt.worker)."""


@dataclass(slots=True)
class RelayResult:
    links: list[str] = field(default_factory=list)
    button_clicked: str = ""
    strategy: str = ""          # "text-match" | "fallback-index"
    batches: int = 0
    captcha: bool = False       # a CAPTCHA appeared (after some links were collected)


# ── text normalisation for button matching ───────────────────────────────────--
_PERSIAN_FOLD = {
    "ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا",
    "‌": " ", "‏": "", "‎": "",  # ZWNJ / RTL/LTR marks
}


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for src, dst in _PERSIAN_FOLD.items():
        text = text.replace(src, dst)
    return " ".join(text.casefold().split())


def _looks_like_captcha(message) -> bool:
    """True if a bot message is a CAPTCHA prompt (text-pattern based).

    The challenge arrives as an image with a prompt like "What number is this?".
    We match on the configurable text patterns; a false positive only causes a
    conservative back-off, never a wrong answer (we never solve them).
    """
    if not npvt_settings.captcha_detection_enabled:
        return False
    patterns = npvt_settings.captcha_text_patterns
    if not patterns:
        return False
    text = _normalise(getattr(message, "message", "") or getattr(message, "text", "") or "")
    if not text:
        return False
    return any(_normalise(p) in text for p in patterns if p.strip())


def _flatten_buttons(message) -> list:
    """Return inline buttons as a flat, row-major list (or [] if none)."""
    rows = getattr(message, "buttons", None)
    if not rows:
        return []
    flat = []
    for row in rows:
        # ``buttons`` is a 2D list; a single button can also appear bare.
        if isinstance(row, (list, tuple)):
            flat.extend(row)
        else:
            flat.append(row)
    return flat


def _select_button(buttons: list, patterns: list[str], fallback_index: int):
    """Choose a button by text match; fall back to a fixed index.

    Returns ``(button, strategy)``. Raises if the list is empty.
    """
    if not buttons:
        raise RelayError("bot reply had no inline buttons to click")

    norm_patterns = [_normalise(p) for p in patterns if p.strip()]
    for btn in buttons:
        label = _normalise(getattr(btn, "text", ""))
        if label and any(p and p in label for p in norm_patterns):
            return btn, "text-match"

    # No text match: the spec says the target is "usually the second button".
    idx = fallback_index if fallback_index < len(buttons) else len(buttons) - 1
    log.warning(
        "npvt: no button text matched %s — falling back to button #%d (%r)",
        patterns, idx, getattr(buttons[idx], "text", ""),
    )
    return buttons[idx], "fallback-index"


# ── relay ─────────────────────────────────────────────────────────────────────
class BotRelay:
    async def _client(self):
        # Reuse the collector's authorised user session (single connection per
        # account is the Telegram-correct approach; opening a second client with
        # the same StringSession risks AUTH_KEY_DUPLICATED).
        client = await collector_client._ensure_client()
        if client is None or not _TELETHON:
            raise RelayUnavailable("Telegram session not configured/authorized")
        return client

    async def relay(self, data: bytes, file_name: str) -> RelayResult:
        """Send ``data`` to the bot and return the V2Ray links it produces."""
        cfg = npvt_settings
        client = await self._client()
        bot = cfg.bot_username
        if not bot:
            raise RelayError("no bot_username configured")

        async def _run() -> RelayResult:
            entity = await client.get_entity(bot)

            buf = io.BytesIO(data)
            buf.name = file_name or "config.npvt"
            attrs = [DocumentAttributeFilename(buf.name)] if _TELETHON else None
            sent = await client.send_file(
                entity, buf, force_document=True, attributes=attrs,
            )
            log.info("npvt: sent %s (%d bytes) to @%s", buf.name, len(data), bot)

            buttons_msg = await self._await_buttons(
                client, entity, after_id=sent.id,
                timeout=cfg.button_response_timeout_seconds,
            )
            buttons = _flatten_buttons(buttons_msg)
            btn, strategy = _select_button(
                buttons, cfg.button_text_patterns, cfg.button_fallback_index,
            )
            label = getattr(btn, "text", "")
            log.info("npvt: clicking button %r via %s", label, strategy)
            try:
                await btn.click()
            except _BotClickTimeout:
                # Click delivered; the bot just didn't answer the callback in
                # time. Proceed to collection — the links usually still arrive.
                log.info("npvt: callback answer timed out — collecting anyway")
            except Exception as exc:  # noqa: BLE001 - many telethon click errors
                raise RelayError(f"button click failed: {exc}") from exc

            links, batches, captcha = await self._collect_links(
                client, entity, anchor_id=buttons_msg.id, seed_msg=buttons_msg,
            )
            return RelayResult(
                links=links, button_clicked=label, strategy=strategy,
                batches=batches, captcha=captcha,
            )

        try:
            return await asyncio.wait_for(_run(), timeout=cfg.relay_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RelayError(
                f"relay exceeded {cfg.relay_timeout_seconds}s overall timeout"
            ) from exc

    async def _await_buttons(self, client, entity, after_id: int, timeout: float):
        """Poll until a message newer than ``after_id`` carries inline buttons."""
        deadline = asyncio.get_event_loop().time() + timeout
        seen = after_id
        while asyncio.get_event_loop().time() < deadline:
            msgs = await client.get_messages(entity, min_id=seen, limit=20)
            # newest-first; inspect oldest-first so we click the first prompt
            for msg in reversed(msgs):
                seen = max(seen, msg.id)
                if _looks_like_captcha(msg):
                    raise CaptchaRequired("bot issued a captcha before the keyboard")
                if _flatten_buttons(msg):
                    return msg
            await asyncio.sleep(1.5)
        raise RelayError("timed out waiting for the bot's button keyboard")

    async def _collect_links(self, client, entity, anchor_id: int, seed_msg):
        """Gather links from new messages and edits until quiet/window elapses.

        Captures three sources of links: the (possibly edited) message that held
        the buttons, any new messages the bot sends, and small text documents
        the bot may attach. De-duplicates while preserving discovery order.
        """
        cfg = npvt_settings
        loop = asyncio.get_event_loop()
        window_end = loop.time() + cfg.collect_window_seconds
        quiet = cfg.collect_quiet_seconds

        ordered: list[str] = []
        seen_links: set[str] = set()
        seen_msg = anchor_id - 1  # so we re-read the anchor (edited) message too
        batches = 0
        last_new = loop.time()

        def _absorb(text: str) -> int:
            added = 0
            for link in extract_links(text or ""):
                if link not in seen_links:
                    seen_links.add(link)
                    ordered.append(link)
                    added += 1
            return added

        # Seed from the button message itself (it may already list links).
        _absorb(getattr(seed_msg, "message", "") or getattr(seed_msg, "text", ""))

        captcha = False
        while loop.time() < window_end:
            msgs = await client.get_messages(entity, min_id=seen_msg, limit=50)
            new_here = 0
            for msg in reversed(msgs):
                seen_msg = max(seen_msg, msg.id)
                if _looks_like_captcha(msg):
                    # Stop here: keep whatever links we already gathered and let
                    # the worker back off. We never answer the challenge.
                    captcha = True
                    break
                new_here += _absorb(getattr(msg, "message", "") or "")
                new_here += await self._absorb_document(client, msg, _absorb)
            if captcha:
                break
            if new_here:
                batches += 1
                last_new = loop.time()
            elif ordered and (loop.time() - last_new) >= quiet:
                break  # nothing new for a full quiet period — the bot is done
            await asyncio.sleep(1.5)

        return ordered, batches, captcha

    async def _absorb_document(self, client, msg, absorb) -> int:
        """If a message carries a small text document, parse links from it."""
        doc_file = getattr(msg, "file", None)
        if not doc_file:
            return 0
        ext = (getattr(doc_file, "ext", "") or "").lower()
        name = (getattr(doc_file, "name", "") or "").lower()
        size = getattr(doc_file, "size", 0) or 0
        looks_textual = ext in (".txt", ".text", "") or name.endswith((".txt", ".text"))
        if not looks_textual or size > 1_000_000:
            return 0
        try:
            blob = await client.download_media(msg, file=bytes)
            if not blob:
                return 0
            return absorb(blob.decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            log.debug("npvt: could not read attached document: %s", exc)
            return 0


relay = BotRelay()
