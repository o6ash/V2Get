"""Clash / Stash YAML generation.

A malformed export is worse than an empty one: Clash and Stash reject the
*entire* profile when a single proxy is invalid, so bad nodes must be dropped
rather than emitted.
"""
from __future__ import annotations

import base64
import json

import yaml

from app.core.clash_export import AUTO_GROUP, SELECT_GROUP, clash_yaml, stash_yaml

VLESS = "vless://b831381d-6324-4d53-ad4f-8cda48b30811@v.example:443?type=ws&security=tls&sni=v.example#VL"
TROJAN = "trojan://password@t.example:443#TJ"
SS = "ss://aes-256-gcm:pw@s.example:8388#SS"


def _vmess(uuid: str = "b831381d-6324-4d53-ad4f-8cda48b30811") -> str:
    payload = {
        "ps": "VM", "add": "m.example", "port": "443", "id": uuid,
        "aid": "0", "net": "ws", "path": "/", "tls": "tls",
    }
    return "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()


def test_output_is_valid_yaml_with_the_expected_groups():
    doc = yaml.safe_load(clash_yaml([VLESS, TROJAN, _vmess()]))
    assert isinstance(doc, dict)
    names = [g["name"] for g in doc["proxy-groups"]]
    assert SELECT_GROUP in names and AUTO_GROUP in names


def test_every_valid_link_becomes_a_proxy():
    doc = yaml.safe_load(clash_yaml([VLESS, TROJAN, SS, _vmess()]))
    assert len(doc["proxies"]) == 4
    assert {p["type"] for p in doc["proxies"]} == {"vless", "trojan", "ss", "vmess"}


def test_iranian_and_private_traffic_is_routed_direct():
    doc = yaml.safe_load(clash_yaml([TROJAN]))
    rules = doc["rules"]
    assert any("GEOIP,IR,DIRECT" in r for r in rules)
    assert any("private" in r and "DIRECT" in r for r in rules)
    assert rules[-1].startswith("MATCH,")


def test_invalid_uuid_node_is_dropped_not_emitted():
    # Stash's Go core rejects the whole profile on "invalid UUID length", so a
    # placeholder UUID must never reach the output.
    doc = yaml.safe_load(clash_yaml([_vmess(uuid="your-uuid-here"), TROJAN]))
    assert [p["type"] for p in doc["proxies"]] == ["trojan"]


def test_unparseable_links_are_skipped_silently():
    doc = yaml.safe_load(clash_yaml(["not-a-link", "vmess://garbage", TROJAN]))
    assert len(doc["proxies"]) == 1


def test_empty_input_still_produces_a_loadable_document():
    doc = yaml.safe_load(clash_yaml([]))
    assert isinstance(doc, dict)
    assert doc.get("proxies") == [] or doc.get("proxies") is None


def test_proxy_names_are_unique():
    # Clash keys proxies by name; duplicates silently shadow each other.
    doc = yaml.safe_load(clash_yaml([TROJAN, TROJAN, TROJAN]))
    names = [p["name"] for p in doc["proxies"]]
    assert len(names) == len(set(names))


def test_group_members_all_reference_real_proxies():
    doc = yaml.safe_load(clash_yaml([VLESS, TROJAN, _vmess()]))
    known = {p["name"] for p in doc["proxies"]}
    for group in doc["proxy-groups"]:
        for member in group.get("proxies", []):
            assert member in known or member in {SELECT_GROUP, AUTO_GROUP, "DIRECT"}


def test_stash_export_excludes_shadowsocks_and_hysteria2():
    # Documented Stash incompatibilities — shipping them breaks the profile.
    doc = yaml.safe_load(stash_yaml([SS, "hysteria2://pw@h.example:443#H2", TROJAN]))
    assert {p["type"] for p in doc["proxies"]} == {"trojan"}


def test_stash_export_is_valid_yaml():
    doc = yaml.safe_load(stash_yaml([VLESS, TROJAN]))
    assert isinstance(doc, dict) and doc["proxies"]
