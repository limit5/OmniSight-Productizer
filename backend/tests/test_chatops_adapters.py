"""R1 (#307) — per-adapter payload-shape + verify tests.

Verifies Discord/Teams/Line payload construction + inbound parsing +
signature verification without hitting any real network endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from backend.chatops import discord as d
from backend.chatops import teams as t
from backend.chatops import line as l
from backend.chatops_bridge import Button


def test_discord_parse_button_click():
    payload = {
        "type": 3,
        "id": "xyz",
        "data": {"component_type": 2, "custom_id": "pep_approve:pep-42"},
        "member": {"user": {"id": "u-1", "username": "alice"}},
    }
    inbound = d.parse_inbound(payload)
    assert inbound.kind == "button"
    assert inbound.button_id == "pep_approve"
    assert inbound.button_value == "pep-42"
    assert inbound.author == "alice"


def test_discord_parse_application_command():
    payload = {
        "type": 2,
        "id": "xyz",
        "data": {
            "name": "omnisight",
            "options": [
                {"name": "action", "value": "status"},
            ],
        },
        "member": {"user": {"id": "u-1", "username": "alice"}},
    }
    inbound = d.parse_inbound(payload)
    assert inbound.kind == "command"
    assert inbound.command == "omnisight"
    assert "status" in inbound.command_args


def test_discord_verify_requires_public_key(monkeypatch):
    monkeypatch.setattr(d.settings, "chatops_discord_public_key", "")
    with pytest.raises(PermissionError):
        d.verify({}, b"")


def test_discord_verify_rejects_bad_signature(monkeypatch):
    pytest.importorskip("nacl")
    # Use a deterministic keypair.
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    vk_hex = sk.verify_key.encode().hex()
    monkeypatch.setattr(d.settings, "chatops_discord_public_key", vk_hex)
    # Missing headers -> reject.
    with pytest.raises(PermissionError):
        d.verify({}, b"hello")
    # Valid signature passes.
    msg = b"hello"
    ts = "1700000000"
    sig = sk.sign(ts.encode() + msg).signature.hex()
    d.verify({"x-signature-ed25519": sig, "x-signature-timestamp": ts}, msg)


def test_teams_build_card_has_actions():
    card = t._build_adaptive_card("title", "body", [Button(id="ok", label="OK")])
    att = card["attachments"][0]["content"]
    assert att["actions"][0]["data"]["buttonId"] == "ok"


def test_teams_parse_button_click():
    payload = {
        "id": "m1",
        "from": {"id": "u1", "name": "bob"},
        "value": {"buttonId": "pep_reject", "buttonValue": "pep-7"},
    }
    inbound = t.parse_inbound(payload)
    assert inbound.kind == "button"
    assert inbound.button_id == "pep_reject"
    assert inbound.button_value == "pep-7"
    assert inbound.author == "bob"


def test_teams_parse_command():
    payload = {
        "id": "m2",
        "from": {"id": "u1", "name": "bob"},
        "text": "/omnisight status",
    }
    inbound = t.parse_inbound(payload)
    assert inbound.kind == "command"
    assert inbound.command == "omnisight"
    assert inbound.command_args == "status"


def test_teams_verify_rejects_bad_hmac(monkeypatch):
    monkeypatch.setattr(t.settings, "chatops_teams_secret", "s3cr3t")
    with pytest.raises(PermissionError):
        t.verify({"authorization": "HMAC abcd"}, b"body")
    body = b"body"
    digest = hmac.new(b"s3cr3t", body, hashlib.sha256).digest()
    t.verify({"authorization": digest.hex()}, body)
    t.verify({"authorization": base64.b64encode(digest).decode()}, body)


def test_line_build_flex_has_postback():
    flex = l._build_flex("title", "body", [Button(id="ok", label="Approve")])
    footer = flex["contents"]["footer"]
    assert "buttonId=ok" in footer["contents"][0]["action"]["data"]


def test_line_parse_postback():
    payload = {
        "events": [{
            "type": "postback",
            "source": {"userId": "U1234"},
            "replyToken": "tok",
            "postback": {"data": "buttonId=pep_approve&value=pep-9"},
        }],
    }
    inbound = l.parse_inbound(payload)
    assert inbound.kind == "button"
    assert inbound.button_id == "pep_approve"
    assert inbound.button_value == "pep-9"


def test_line_parse_command():
    payload = {
        "events": [{
            "type": "message",
            "source": {"userId": "U1"},
            "replyToken": "tok",
            "message": {"text": "/omnisight inspect agent-1"},
        }],
    }
    inbound = l.parse_inbound(payload)
    assert inbound.kind == "command"
    assert inbound.command == "omnisight"
    assert inbound.command_args == "inspect agent-1"


def test_line_verify_good_and_bad_signature(monkeypatch):
    monkeypatch.setattr(l.settings, "chatops_line_channel_secret", "sec")
    body = b'{"events":[]}'
    digest = hmac.new(b"sec", body, hashlib.sha256).digest()
    good = base64.b64encode(digest).decode()
    l.verify({"x-line-signature": good}, body)
    with pytest.raises(PermissionError):
        l.verify({"x-line-signature": "deadbeef"}, body)
