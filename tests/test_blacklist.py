"""File-backed blacklist matching and hot reload."""
from __future__ import annotations

import pytest

from app.core.blacklist import blacklist
from app.core.parser import ParsedConfig


@pytest.fixture(autouse=True)
def _clean_blacklists():
    """Start every test from an empty blacklist and restore afterwards."""
    blacklist.ensure_files()
    for kind in ("domains", "ips", "keywords"):
        blacklist.replace(kind, [])
    yield
    for kind in ("domains", "ips", "keywords"):
        blacklist.replace(kind, [])


def _cfg(host: str = "example.com", raw: str = "vless://x", name: str = "") -> ParsedConfig:
    return ParsedConfig(protocol="vless", raw=raw, host=host, port=443, name=name)


def test_nothing_is_blocked_by_default():
    blocked, reason = blacklist.is_blocked(_cfg())
    assert not blocked and reason == ""


def test_exact_domain_is_blocked():
    blacklist.add("domains", "bad.com")
    blocked, reason = blacklist.is_blocked(_cfg(host="bad.com"))
    assert blocked and reason == "domain:bad.com"


def test_subdomains_of_a_blocked_domain_are_blocked():
    blacklist.add("domains", "bad.com")
    assert blacklist.is_blocked(_cfg(host="node1.bad.com"))[0]


def test_lookalike_domain_is_not_blocked():
    # "notbad.com" merely ends with the string "bad.com" — it must NOT match,
    # otherwise a suffix check would blacklist unrelated third parties.
    blacklist.add("domains", "bad.com")
    assert not blacklist.is_blocked(_cfg(host="notbad.com"))[0]


def test_domain_matching_is_case_insensitive():
    blacklist.add("domains", "Bad.COM")
    assert blacklist.is_blocked(_cfg(host="BAD.com"))[0]


def test_ip_is_blocked():
    blacklist.add("ips", "10.0.0.1")
    blocked, reason = blacklist.is_blocked(_cfg(host="10.0.0.1"))
    assert blocked and reason == "ip:10.0.0.1"


def test_keyword_matches_anywhere_in_the_raw_link_or_name():
    blacklist.add("keywords", "spamnet")
    assert blacklist.is_blocked(_cfg(raw="vless://u@h.com:443?sni=spamnet.io"))[0]
    assert blacklist.is_blocked(_cfg(name="cheap spamnet node"))[0]


def test_remove_unblocks():
    blacklist.add("domains", "bad.com")
    blacklist.remove("domains", "bad.com")
    assert not blacklist.is_blocked(_cfg(host="bad.com"))[0]


def test_replace_swaps_the_whole_list():
    blacklist.add("domains", "old.com")
    blacklist.replace("domains", ["new.com", "other.com"])
    assert blacklist.get("domains") == ["new.com", "other.com"]


def test_entries_are_normalised_and_blanks_dropped():
    blacklist.replace("domains", ["  MiXeD.CoM  ", "", "   "])
    assert blacklist.get("domains") == ["mixed.com"]


def test_edits_are_visible_immediately_without_a_restart():
    assert not blacklist.is_blocked(_cfg(host="late.com"))[0]
    blacklist.add("domains", "late.com")
    assert blacklist.is_blocked(_cfg(host="late.com"))[0]


def test_writing_an_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        blacklist.replace("not-a-kind", ["x"])
