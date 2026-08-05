"""The ovpn subscription is the profiles themselves, not a list of links.

A link list is unusable: V2ray-family apps parse each subscription line as a
config URI and read ``https://host:443`` as an HTTP proxy, and OpenVPN clients
have no notion of a subscription at all. So the published file must carry the
full profile text, delimited so a single profile can be lifted back out.
"""
from __future__ import annotations

import hashlib

from app.ovpn import publisher

PROFILE_A = "client\ndev tun\nproto tcp\nremote a.example 443\n"
PROFILE_B = "client\ndev tun\nproto udp\nremote b.example 1194\n"


def test_index_inlines_profile_contents_not_urls() -> None:
    text, included = publisher.build_index(
        [("chan-1-a.ovpn", PROFILE_A), ("chan-2-b.ovpn", PROFILE_B)], budget=1_000_000
    )
    assert included == ["chan-1-a.ovpn", "chan-2-b.ovpn"]
    assert "remote a.example 443" in text
    assert "remote b.example 1194" in text
    assert "https://" not in text


def test_index_blocks_are_delimited_and_extractable() -> None:
    text, _ = publisher.build_index(
        [("chan-1-a.ovpn", PROFILE_A), ("chan-2-b.ovpn", PROFILE_B)], budget=1_000_000
    )
    begin = publisher.BEGIN.format(name="chan-1-a")
    end = publisher.END.format(name="chan-1-a")
    block = text.split(begin, 1)[1].split(end, 1)[0]
    assert block.strip() == PROFILE_A.strip()
    # Delimiters are '#' comments, i.e. valid OpenVPN syntax on their own.
    assert begin.startswith("#") and end.startswith("#")


def test_index_stops_at_byte_budget_but_never_returns_empty() -> None:
    text, included = publisher.build_index(
        [("chan-1-a.ovpn", PROFILE_A), ("chan-2-b.ovpn", PROFILE_B)], budget=10
    )
    assert included == ["chan-1-a.ovpn"]
    assert len(text.encode("utf-8")) > 10  # the first profile is always kept


def test_profiles_are_published_as_txt() -> None:
    assert publisher.published_name("chan-1-a.ovpn") == "chan-1-a.txt"
    assert publisher.published_name("already.txt") == "already.txt"


def test_blob_sha_matches_git_object_id() -> None:
    data = PROFILE_A.encode("utf-8")
    expected = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324
    assert publisher.blob_sha(PROFILE_A) == expected
