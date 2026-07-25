"""Deduplication identity.

The whole value of the archive rests on this: two links pointing at the same
endpoint with the same credentials must collapse to one row no matter how the
channel that posted them decorated the name, tags or query string.
"""
from __future__ import annotations

from app.core.fingerprint import fingerprint
from app.core.parser import ParsedConfig


def _cfg(**kw) -> ParsedConfig:
    base = {"protocol": "vless", "raw": "vless://x", "host": "h.com", "port": 443}
    base.update(kw)
    return ParsedConfig(**base)


def test_name_is_not_part_of_the_identity():
    a = _cfg(uuid="u1", name="🔥 Free Germany 🇩🇪")
    b = _cfg(uuid="u1", name="totally different label")
    assert fingerprint(a) == fingerprint(b)


def test_query_params_are_not_part_of_the_identity():
    a = _cfg(uuid="u1", extra={"sni": "a.com", "fp": "chrome"})
    b = _cfg(uuid="u1", extra={})
    assert fingerprint(a) == fingerprint(b)


def test_hostname_case_is_normalised():
    assert fingerprint(_cfg(host="Host.COM", uuid="u")) == fingerprint(
        _cfg(host="host.com", uuid="u")
    )


def test_different_uuid_is_a_different_config():
    assert fingerprint(_cfg(uuid="u1")) != fingerprint(_cfg(uuid="u2"))


def test_different_port_is_a_different_config():
    assert fingerprint(_cfg(uuid="u", port=443)) != fingerprint(_cfg(uuid="u", port=8443))


def test_same_endpoint_on_different_protocols_never_collides():
    a = _cfg(protocol="vless", uuid="shared")
    b = _cfg(protocol="vmess", uuid="shared")
    assert fingerprint(a) != fingerprint(b)


def test_trojan_identity_uses_the_password():
    a = _cfg(protocol="trojan", password="p1")
    b = _cfg(protocol="trojan", password="p2")
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) == fingerprint(_cfg(protocol="trojan", password="p1", name="x"))


def test_shadowsocks_identity_includes_the_cipher():
    # Same password, different cipher = a genuinely different server config.
    a = _cfg(protocol="ss", method="aes-256-gcm", password="pw")
    b = _cfg(protocol="ss", method="chacha20-ietf-poly1305", password="pw")
    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_is_prefixed_with_the_protocol_and_is_stable():
    fp = fingerprint(_cfg(uuid="u"))
    assert fp.startswith("vless:")
    assert fp == fingerprint(_cfg(uuid="u"))       # deterministic across calls
    assert len(fp) <= 80                           # fits the DB column
