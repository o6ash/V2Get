"""Local `.npvt` unlocker — replaces the external Telegram bot relay.

A ``.npvt`` file is ``NPVT1\\n`` followed by comma-separated base64 blobs, each
``base64(nonce[16] || ciphertext)`` under NapsternetV's whitebox AES-CTR. This
package decrypts the container in-process, neutralises the distributor lock and
emits standard share URIs — the same links the bot used to hand back, but with
no network round-trip, no button-clicking, no CAPTCHA and no rate limit.

The heavy lifting lives in three vendored modules (see ``VENDOR.md``):

    npvt_tables.py   whitebox AES-CTR lookup tables (generated, do not edit)
    npvt_crypto.py   core cipher: core_transform, ctr_crypt, blob codec
    npvt.py          container format: parse / unlock / to_uris / serialize

Only :func:`unlock_to_links` is meant to be imported by the rest of the app.
"""
from __future__ import annotations

import asyncio

from app.core.logbook import get_logger
from app.npvt.unlocker.npvt import Npvt, NpvtError

__all__ = ["UnlockError", "UnlockResult", "unlock_to_links", "unlock_to_links_sync"]

log = get_logger()


class UnlockError(RuntimeError):
    """The payload is not a well-formed NPVT1 container (or has no profiles).

    Mirrors the old ``RelayError`` contract so the worker's failure handling is
    unchanged: raised errors become a contained, retryable job failure and never
    reach the core collector.
    """


class UnlockResult:
    """Outcome of unlocking one file.

    ``links`` is what the pipeline consumes; the rest is diagnostics that the
    worker logs (the bot relay reported button/batch counts here instead).
    """

    __slots__ = ("links", "profiles", "was_locked")

    def __init__(self, links: list[str], profiles: int, was_locked: bool) -> None:
        self.links = links
        self.profiles = profiles
        self.was_locked = was_locked


def unlock_to_links_sync(data: bytes, file_name: str = "") -> UnlockResult:
    """Decrypt, unlock and export share URIs from raw ``.npvt`` bytes.

    Pure CPU work, no I/O. Raises :class:`UnlockError` on a malformed container
    so the caller can mark the file failed without special-casing crypto errors.
    """
    if not data:
        raise UnlockError("empty file")
    try:
        container = Npvt.parse(data)
    except NpvtError as exc:
        raise UnlockError(f"not a valid NPVT1 container: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - corrupt payloads must not escape as-is
        raise UnlockError(f"failed to decrypt {file_name or 'file'}: {exc}") from exc

    was_locked = container.is_locked()
    profiles = len(container.profiles())
    if not profiles:
        raise UnlockError("container decrypted but holds no profiles")

    try:
        links = container.unlock().to_uris()
    except Exception as exc:  # noqa: BLE001
        raise UnlockError(f"failed to export links: {exc}") from exc

    # A profile can legitimately yield no URI (e.g. shadowsocks with no real
    # cipher), but zero links out of a non-empty container means the file is
    # useless to the pipeline — surface it as a failure rather than a silent
    # success with nothing injected.
    if not links:
        raise UnlockError(f"{profiles} profile(s) decrypted but none exported a usable link")

    return UnlockResult(links=links, profiles=profiles, was_locked=was_locked)


async def unlock_to_links(data: bytes, file_name: str = "") -> UnlockResult:
    """Async wrapper around :func:`unlock_to_links_sync`.

    The whitebox cipher is pure Python and CPU-bound (tens of ms for a typical
    file, more for large ones), so it runs in a worker thread to keep the event
    loop — which is also serving the dashboard and the core collector — free.
    """
    return await asyncio.to_thread(unlock_to_links_sync, data, file_name)
