"""β-3c (leg-2) — runner-plane: endpoint gate, refresh loop inertness,
ticketless revert join."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_endpoint_respects_dual_flag(monkeypatch):
    from backend.routers.learned_items import learned_items_block

    class _U:  # any principal; the loader gate is the guard here
        id = "u"; email = "u@x"; name = "u"; role = "admin"

    monkeypatch.delenv("OMNISIGHT_LEARNED_ITEM_READ", raising=False)
    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED", "1")
    out = await learned_items_block(tenant="t", context="", user=_U())
    assert out == {"block": "", "result": "kill_switch_off"}


@pytest.mark.asyncio
async def test_refresh_loop_inert_when_read_off(monkeypatch):
    from backend import learned_item_refresh as lir

    monkeypatch.delenv("OMNISIGHT_LEARNED_ITEM_READ", raising=False)

    def _boom():
        raise AssertionError("pool must not be touched while read is OFF")

    ticks = await lir.run_learned_item_refresh_loop(
        get_pool=_boom, max_ticks=2, interval_s=5.0,
        sleep=lambda _s: _noop(),
    )
    assert ticks == 2  # iterations counted; pool untouched (read OFF)


async def _noop():
    return None


@pytest.mark.asyncio
async def test_reverted_later_by_change_join():
    from backend.agents.worker_loop_distiller import reverted_later_by_change

    class _Conn:
        def __init__(self, subject, hit):
            self._subject = subject; self._hit = hit

        async def fetchrow(self, *_a):
            return {"canonical_subject": self._subject} if self._subject else None

        async def fetchval(self, sql, *args):
            assert "strpos" in sql
            return self._hit

    assert await reverted_later_by_change(_Conn("[OP-1] fix x", True), 7) is True
    assert await reverted_later_by_change(_Conn("[OP-1] fix x", False), 7) is False
    assert await reverted_later_by_change(_Conn(None, True), 7) is False  # no original row
