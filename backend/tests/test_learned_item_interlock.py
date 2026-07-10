"""Phase U4 step-0 — emergency promotion interlock.

Proves the deny-by-default + human-only gate on learned-item promotion, and
guards against a NEW ungated writer to the injected ``configs/skills`` tree
re-opening the hole (codex audit #1: inventory every runtime writer).
"""
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import auth
from backend import learned_item_interlock as interlock

_FLAG = "OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED"


def _human() -> auth.User:
    return auth.User(id="alice", email="alice@corp.example", name="Alice",
                     role="admin", enabled=True)


def _apikey_bot() -> auth.User:
    # auth.py assigns role="admin" to any api-key bearer — the exact principal
    # the hole let promote. id="apikey:*" must still be refused.
    return auth.User(id="apikey:deadbeef", email="apikey:runner", name="runner",
                     role="admin", enabled=True)


def test_promotion_enabled_reads_env(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    assert interlock.promotion_enabled() is False
    monkeypatch.setenv(_FLAG, "true")
    assert interlock.promotion_enabled() is True
    monkeypatch.setenv(_FLAG, "false")
    assert interlock.promotion_enabled() is False
    monkeypatch.setenv(_FLAG, "")
    assert interlock.promotion_enabled() is False


def test_human_allowed_only_when_flag_on(monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
    interlock.assert_promotion_allowed(_human())  # must not raise


def test_human_denied_by_default(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    with pytest.raises(HTTPException) as exc:
        interlock.assert_promotion_allowed(_human())
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "promotion_disabled"


def test_apikey_bot_denied_even_when_flag_on(monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
    with pytest.raises(HTTPException) as exc:
        interlock.assert_promotion_allowed(_apikey_bot())
    assert exc.value.status_code == 403
    # human-only check fires FIRST — a bot never reaches the flag branch.
    assert exc.value.detail["error"] == "auth_refused"


def test_apikey_bot_denied_when_flag_off(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    with pytest.raises(HTTPException) as exc:
        interlock.assert_promotion_allowed(_apikey_bot())
    assert exc.value.status_code == 403


def test_every_live_skills_writer_is_gated():
    """Anti-regression inventory guard (codex audit #1).

    Writers to the injected ``configs/skills`` tree reference ``_SKILLS_LIVE``;
    readers reference ``_SKILLS_DIR``. Any non-test module that references
    ``_SKILLS_LIVE`` MUST route through the interlock — a new ungated writer
    fails this test until it calls ``assert_promotion_allowed``.
    """
    backend_dir = Path(__file__).resolve().parents[1]
    allow = {"routers/auto_skills.py", "routers/skills.py"}
    writers = set()
    for py in backend_dir.rglob("*.py"):
        rel = py.relative_to(backend_dir).as_posix()
        if rel.startswith("tests/"):
            continue
        if "_SKILLS_LIVE" in py.read_text(encoding="utf-8"):
            writers.add(rel)
    assert writers == allow, (
        "unexpected writer(s) to the injected skills tree — every runtime "
        f"writer must go through the U4 promotion interlock. Got: {writers}"
    )
    for rel in writers:
        assert "assert_promotion_allowed" in (backend_dir / rel).read_text(encoding="utf-8"), (
            f"{rel} writes the injected skills tree but does not call "
            "learned_item_interlock.assert_promotion_allowed"
        )
