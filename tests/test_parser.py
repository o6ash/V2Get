"""Link extraction and per-protocol parsing.

The parser is the widest attack surface for malformed input: it consumes
arbitrary Telegram message text written by strangers, so the contract is
"extract what is valid, silently drop what is not, never raise".
"""
from __future__ import annotations

import base64
import json

from app.core.parser import (
    SUPPORTED,
    extract_links,
    parse_link,
    parse_text,
    rename,
)


def _vmess_link(**overrides) -> str:
    payload = {
        "v": "2", "ps": "example", "add": "1.2.3.4", "port": "443",
        "id": "b831381d-6324-4d53-ad4f-8cda48b30811", "aid": "0",
        "net": "ws", "type": "none", "host": "cdn.example.com",
        "path": "/ws", "tls": "tls",
    }
    payload.update(overrides)
    blob = base64.b64encode(json.dumps(payload).encode()).decode()
    return f"vmess://{blob}"


# ── extraction ───────────────────────────────────────────────────────────────

def test_extracts_link_embedded_in_prose():
    text = "🔥 New config! trojan://pass@example.com:443#Node here you go"
    assert extract_links(text) == ["trojan://pass@example.com:443#Node"]


def test_extracts_multiple_links_from_one_message():
    text = (
        "vless://uuid@a.com:443?type=tcp#A\n"
        "some words\n"
        "trojan://pw@b.com:8443#B"
    )
    assert len(extract_links(text)) == 2


def test_trailing_punctuation_is_trimmed():
    assert extract_links("see trojan://p@h.io:443#N.") == ["trojan://p@h.io:443#N"]


def test_empty_and_linkless_text_yields_nothing():
    assert extract_links("") == []
    assert extract_links("just a normal sentence") == []


def test_parse_text_drops_invalid_configs():
    # Valid link plus one with a nonsense port — only the good one survives.
    text = "trojan://p@good.com:443#ok trojan://p@bad.com:0#nope"
    parsed = parse_text(text)
    assert [c.host for c in parsed] == ["good.com"]


# ── per-protocol parsing ─────────────────────────────────────────────────────

def test_parse_vmess():
    cfg = parse_link(_vmess_link())
    assert cfg is not None
    assert cfg.protocol == "vmess"
    assert (cfg.host, cfg.port) == ("1.2.3.4", 443)
    assert cfg.uuid == "b831381d-6324-4d53-ad4f-8cda48b30811"
    assert cfg.name == "example"
    assert cfg.valid


def test_parse_vless_keeps_query_params_in_extra():
    cfg = parse_link("vless://uuid-1@host.net:8443?type=ws&security=tls&sni=x.com#My%20Node")
    assert cfg is not None
    assert cfg.protocol == "vless"
    assert (cfg.host, cfg.port, cfg.uuid) == ("host.net", 8443, "uuid-1")
    assert cfg.name == "My Node"           # fragment is URL-decoded
    assert cfg.extra["security"] == "tls"


def test_parse_trojan():
    cfg = parse_link("trojan://secret@t.example:443#Trojan")
    assert cfg is not None
    assert (cfg.protocol, cfg.host, cfg.port, cfg.password) == (
        "trojan", "t.example", 443, "secret",
    )


def test_parse_ss_plain_userinfo():
    cfg = parse_link("ss://aes-256-gcm:pw@ss.example:8388#SS")
    assert cfg is not None
    assert (cfg.protocol, cfg.host, cfg.port) == ("ss", "ss.example", 8388)
    assert (cfg.method, cfg.password) == ("aes-256-gcm", "pw")


def test_parse_ss_fully_base64_encoded():
    blob = base64.b64encode(b"aes-128-gcm:secret@1.1.1.1:9000").decode()
    cfg = parse_link(f"ss://{blob}")
    assert cfg is not None
    assert (cfg.host, cfg.port, cfg.method) == ("1.1.1.1", 9000, "aes-128-gcm")


def test_parse_hysteria2_and_hy2_alias_normalise_together():
    a = parse_link("hysteria2://pw@h2.example:443#H2")
    b = parse_link("hy2://pw@h2.example:443#H2")
    assert a is not None and b is not None
    # `hy2` is an alias — both must normalise to the same protocol name so they
    # deduplicate against each other rather than counting as two configs.
    assert a.protocol == b.protocol == "hysteria2"


def test_parse_tuic_splits_uuid_and_password():
    cfg = parse_link("tuic://uuid-x:pass-y@tu.example:443?alpn=h3#T")
    assert cfg is not None
    assert (cfg.uuid, cfg.password) == ("uuid-x", "pass-y")


def test_parse_ssr():
    inner = base64.urlsafe_b64encode(b"pw").decode().rstrip("=")
    body = f"ssr.example:8080:origin:aes-256-cfb:plain:{inner}"
    blob = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    cfg = parse_link(f"ssr://{blob}")
    assert cfg is not None
    assert (cfg.protocol, cfg.host, cfg.port) == ("ssr", "ssr.example", 8080)


def test_every_supported_scheme_is_recognised_by_the_extractor():
    for scheme in SUPPORTED:
        assert extract_links(f"{scheme}://x@h.com:1#n"), scheme


# ── malformed input must degrade, never raise ────────────────────────────────

def test_garbage_links_return_none_instead_of_raising():
    for bad in (
        "vmess://!!!not-base64!!!",
        "vmess://" + base64.b64encode(b"not json").decode(),
        "ssr://" + base64.b64encode(b"too:few:fields").decode(),
        "unknown://whatever",
        "trojan://",
    ):
        assert parse_link(bad) is None or not parse_link(bad).valid


def test_port_out_of_range_is_invalid():
    cfg = parse_link("trojan://p@h.com:99999#x")
    assert cfg is None or not cfg.valid


# ── rename ───────────────────────────────────────────────────────────────────

def test_rename_vmess_rewrites_the_ps_field():
    renamed = rename(_vmess_link(), "NewName")
    cfg = parse_link(renamed)
    assert cfg is not None
    assert cfg.name == "NewName"
    # The connection identity must survive a rename untouched.
    assert (cfg.host, cfg.port, cfg.uuid) == (
        "1.2.3.4", 443, "b831381d-6324-4d53-ad4f-8cda48b30811",
    )


def test_rename_url_scheme_replaces_the_fragment():
    renamed = rename("trojan://pw@h.com:443#Old", "New Name")
    assert renamed.startswith("trojan://pw@h.com:443#")
    cfg = parse_link(renamed)
    assert cfg is not None and cfg.name == "New Name"


def test_rename_never_corrupts_an_unparseable_config():
    # A rename must be lossless-or-noop: returning a broken link would silently
    # publish a config that no client can use.
    broken = "vmess://%%%not-valid%%%"
    assert rename(broken, "X") == broken
    assert rename("ssr://whatever", "X") == "ssr://whatever"
