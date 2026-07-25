"""Golden tests for the local `.npvt` unlocker that replaced the bot relay.

These assert the three properties that made it safe to drop the external bot:

1. **Fidelity** — unlocking a locked container reproduces the reference
   unlocked file byte-for-byte (the cipher round-trips exactly).
2. **Interoperability** — every exported URI is accepted by v2get's own
   parser and yields a stable fingerprint, so injected links flow through the
   existing dedup/TCP/pool machinery unchanged.
3. **Containment** — malformed input raises :class:`UnlockError` rather than
   leaking a crypto/JSON exception into the worker.

Fixtures in ``tests/fixtures/`` are real `.npvt` files shipped with the
upstream toolkit (locked + reference-unlocked pairs).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.fingerprint import fingerprint
from app.core.parser import parse_link
from app.npvt.unlocker import UnlockError, unlock_to_links, unlock_to_links_sync
from app.npvt.unlocker.npvt import Npvt

FIXTURES = Path(__file__).parent / "fixtures"

LOCKED_PAIRS = [
    ("locked_sample.npvt", "unlocked_sample.npvt"),
    ("locked_small.npvt", "unlocked_small.npvt"),
]


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ── fidelity ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("locked_name,unlocked_name", LOCKED_PAIRS)
def test_unlock_reproduces_reference_file_byte_for_byte(locked_name, unlocked_name):
    """The re-encrypted container must match the toolkit's own output exactly."""
    produced = Npvt.parse(_read(locked_name)).unlock().serialize()
    assert produced == _read(unlocked_name)


@pytest.mark.parametrize("locked_name,unlocked_name", LOCKED_PAIRS)
def test_locked_and_unlocked_fixtures_yield_identical_links(locked_name, unlocked_name):
    """Unlocking must not perturb the profiles — only the lock fields."""
    from_locked = unlock_to_links_sync(_read(locked_name)).links
    from_unlocked = unlock_to_links_sync(_read(unlocked_name)).links
    assert from_locked == from_unlocked
    assert from_locked  # and it actually produced something


def test_locked_fixture_is_reported_as_locked():
    result = unlock_to_links_sync(_read("locked_sample.npvt"))
    assert result.was_locked is True
    assert result.profiles == 5
    assert len(result.links) == 5


def test_already_unlocked_fixture_is_reported_as_unlocked():
    result = unlock_to_links_sync(_read("unlocked_sample.npvt"))
    assert result.was_locked is False


# ── interoperability with the core pipeline ───────────────────────────────────

@pytest.mark.parametrize("locked_name,_unlocked", LOCKED_PAIRS)
def test_every_exported_link_is_accepted_by_the_core_parser(locked_name, _unlocked):
    """The whole point of the module: links must survive ingest.inject()."""
    links = unlock_to_links_sync(_read(locked_name)).links
    assert links
    for link in links:
        cfg = parse_link(link)
        assert cfg is not None, f"core parser rejected: {link[:80]}"
        assert cfg.valid, f"core parser produced an invalid config: {link[:80]}"
        assert cfg.host and cfg.port
        # A fingerprint is what dedup/cooldown key on — it must be derivable.
        assert fingerprint(cfg)


def test_exported_links_have_unique_fingerprints():
    links = unlock_to_links_sync(_read("locked_sample.npvt")).links
    fps = {fingerprint(parse_link(link)) for link in links}
    assert len(fps) == len(links)


# ── containment: bad input must raise UnlockError, never something else ───────

@pytest.mark.parametrize("payload,reason", [
    (b"", "empty file"),
    (b"not an npvt file at all", "missing header line"),
    (b"NPVT9\nAAAA", "wrong header version"),
    (b"NPVT1\n", "no blobs"),
    (b"NPVT1\n!!!not-base64!!!", "undecodable blob"),
    (b"NPVT1\nAAAA", "blob shorter than the nonce"),
])
def test_malformed_input_raises_unlock_error(payload, reason):
    with pytest.raises(UnlockError):
        unlock_to_links_sync(payload, "bad.npvt")


def test_truncated_real_file_raises_unlock_error():
    """A half-downloaded file must fail cleanly, not crash the worker."""
    data = _read("locked_sample.npvt")
    with pytest.raises(UnlockError):
        unlock_to_links_sync(data[: len(data) // 3], "truncated.npvt")


# ── async wrapper ─────────────────────────────────────────────────────────────

def test_async_wrapper_matches_sync_result():
    data = _read("locked_sample.npvt")
    sync_links = unlock_to_links_sync(data).links
    async_links = asyncio.run(unlock_to_links(data, "sample.npvt")).links
    assert async_links == sync_links


def test_async_wrapper_propagates_unlock_error():
    with pytest.raises(UnlockError):
        asyncio.run(unlock_to_links(b"garbage", "bad.npvt"))
