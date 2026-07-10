"""Phase U4 step-0b — the legacy promote path is RETIRED (unconditional 410).

Supersedes the step-0 human-only + deny-by-default behaviour: the legacy
FS-writing promote endpoints are now unconditionally unavailable, independent of
any flag or principal (the flag could previously re-open the ungated path). The
anti-regression inventory guard (every ``_SKILLS_LIVE`` writer routes through the
chokepoint) is retained.
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
    return auth.User(id="apikey:deadbeef", email="apikey:runner", name="runner",
                     role="admin", enabled=True)


def _assert_retired(exc: HTTPException) -> None:
    assert exc.status_code == 410
    assert exc.detail["error"] == "legacy_promotion_retired"


def test_retired_for_human_regardless_of_flag(monkeypatch):
    # Even a human with the flag explicitly ON is refused — the legacy path is
    # retired, not merely gated.
    monkeypatch.setenv(_FLAG, "true")
    with pytest.raises(HTTPException) as exc:
        interlock.assert_promotion_allowed(_human())
    _assert_retired(exc.value)


def test_retired_for_human_flag_off(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    with pytest.raises(HTTPException) as exc:
        interlock.assert_promotion_allowed(_human())
    _assert_retired(exc.value)


def test_retired_for_apikey_bot(monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
    with pytest.raises(HTTPException) as exc:
        interlock.assert_promotion_allowed(_apikey_bot())
    _assert_retired(exc.value)


def test_promotion_enabled_still_reads_env_for_future_publisher(monkeypatch):
    # The flag survives as the kill-switch for the FUTURE U4 canonical publisher
    # (freeze §G7); it just no longer gates the retired legacy endpoints.
    monkeypatch.delenv(_FLAG, raising=False)
    assert interlock.promotion_enabled() is False
    monkeypatch.setenv(_FLAG, "true")
    assert interlock.promotion_enabled() is True
    monkeypatch.setenv(_FLAG, "off")
    assert interlock.promotion_enabled() is False


def test_every_live_skills_writer_is_gated():
    """Anti-regression inventory guard: every non-test module that references
    ``_SKILLS_LIVE`` (the write surface; readers use ``_SKILLS_DIR``) MUST route
    through the interlock chokepoint, so a new ungated writer fails CI.
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
