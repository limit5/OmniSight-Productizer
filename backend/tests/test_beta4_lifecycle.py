"""β-4 (leg-2) — lifecycle: served sink, citation gating, C3 human-only
utility invariant, rollup flag, revoke exposure."""
from __future__ import annotations

import pathlib

import pytest

_B = pathlib.Path(__file__).resolve().parents[1]


def test_served_sink_populated_only_on_non_empty(monkeypatch):
    from backend import learned_item_loader as lil

    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED", "1")
    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_READ", "1")
    sink: list = []
    # cache empty -> empty_degraded, sink untouched
    lil._reset_for_tests()
    block, result = lil.get_learned_items_block(
        tenant_id="t", context="", served_sink=sink)
    assert result == "empty_degraded" and sink == []


def test_citation_ticket_shape_gate():
    from backend.routers.learned_items import _TICKET_RE

    assert _TICKET_RE.match("OP-2717")
    assert not _TICKET_RE.match("change-99")
    assert not _TICKET_RE.match("op-1")
    assert not _TICKET_RE.match("OP-1; DROP TABLE x")


def test_c3_utility_is_human_only_signal():
    """Audit C3: no read/gate path may query the utility rollup."""
    for mod in ("learned_item_loader.py", "learned_item_publisher.py",
                "learned_item_approval.py", "memory_promotion_eval.py"):
        src = (_B / mod).read_text()
        assert "learned_item_utility" not in src, mod


def test_rollup_flag_default_off(monkeypatch):
    from backend.agents import memory_promotion_scheduler as mps

    monkeypatch.delenv("OMNISIGHT_MEMORY_UTILITY_ROLLUP", raising=False)
    assert mps.utility_rollup_enabled() is False
    monkeypatch.setenv("OMNISIGHT_MEMORY_UTILITY_ROLLUP", "1")
    assert mps.utility_rollup_enabled() is True


def test_revoke_endpoint_exists_and_requires_reason():
    from backend.routers.memory_promotion import RevokeBody, router

    paths = {r.path for r in router.routes}
    assert "/memory-promotions/{version_id}/revoke" in paths
    with pytest.raises(Exception):
        RevokeBody()  # reason required
