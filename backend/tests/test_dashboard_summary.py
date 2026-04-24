"""Phase 4-1 — ``GET /api/v1/dashboard/summary`` aggregator tests.

Pins the contract for the server-side fan-out that replaces the
frontend ``useEngine`` hook's 11-endpoint ``Promise.allSettled`` loop
(see ``hooks/use-engine.ts::fetchSystemData``).

Happy path: all 11 sub-queries succeed → every key of the response
carries ``{"ok": True, "data": ...}``.

Partial failure: a single sub-query raising must not take down the
rest — the failing key returns ``{"ok": False, "error": "..."}`` and
the envelope stays HTTP 200. This is the whole point of pushing the
``Promise.allSettled`` pattern to the server; if one sub-call fails
the other 10 panels should still render live data.

The underlying sub-endpoints are monkeypatched because:
  - Many of them shell out (``/proc/cpuinfo`` reads, ``lsusb``, git
    subprocess) and exercising the real implementations here would
    make the test both slow and environment-dependent.
  - The aggregator's correctness is about *fan-out + envelope
    shape*, not about what each sub-endpoint returns. Those are
    covered by their own tests.
"""

from __future__ import annotations

import pytest

from backend.routers import dashboard as _dash
from backend.routers import simulations as _simulations_router
from backend.routers import system as _system_router


_SUB_KEYS = [
    "systemStatus",
    "systemInfo",
    "devices",
    "spec",
    "repos",
    "logs",
    "tokenUsage",
    "tokenBudget",
    "notificationsUnread",
    "compression",
    "simulations",
]


def _stub_all(monkeypatch):
    """Replace the 11 sub-endpoint fns with awaitables returning sentinel
    values that are cheap to assert on.

    The stubs ignore whatever kwargs the aggregator passes (limit, conn,
    task_id, ...) because the aggregator is not responsible for which
    args each sub-fn accepts — it just fan-outs. Using ``**_kwargs``
    keeps the stubs robust against future sub-fn signature changes.
    """
    async def _status(*_a, **_k):
        return {"tasks_total": 3, "agents_running": 1}
    async def _info(*_a, **_k):
        return {"hostname": "test-host", "cpu_cores": 8}
    async def _devices(*_a, **_k):
        return [{"id": "usb-0-1", "name": "Test camera", "type": "camera"}]
    async def _spec(*_a, **_k):
        return [{"key": "sensor", "value": "IMX335"}]
    async def _repos(*_a, **_k):
        return [{"id": "main-repo", "branch": "master"}]
    async def _logs(*_a, **_k):
        return [{"timestamp": "00:00:00", "message": "online", "level": "info"}]
    async def _tokens(*_a, **_k):
        return [{"model": "claude-opus-4-7", "total_tokens": 150}]
    async def _budget(*_a, **_k):
        return {"budget": 100.0, "usage": 1.23, "ratio": 0.01, "frozen": False}
    async def _unread(*_a, **_k):
        return {"count": 2}
    async def _compression(*_a, **_k):
        return {"total_original_bytes": 0, "estimated_tokens_saved": 0}
    async def _sims(*_a, **_k):
        return [{"id": "sim-1", "status": "pass"}]

    monkeypatch.setattr(_system_router, "get_system_status", _status)
    monkeypatch.setattr(_system_router, "get_system_info", _info)
    monkeypatch.setattr(_system_router, "get_devices", _devices)
    monkeypatch.setattr(_system_router, "get_spec", _spec)
    monkeypatch.setattr(_system_router, "get_repos", _repos)
    monkeypatch.setattr(_system_router, "get_logs", _logs)
    monkeypatch.setattr(_system_router, "get_token_usage", _tokens)
    monkeypatch.setattr(_system_router, "get_token_budget", _budget)
    monkeypatch.setattr(_system_router, "unread_count", _unread)
    monkeypatch.setattr(_system_router, "get_compression_stats", _compression)
    monkeypatch.setattr(_simulations_router, "list_simulations", _sims)


def _stub_pool_conn_passthrough(monkeypatch):
    """Short-circuit ``_run_with_conn`` so the aggregator does not need a
    real asyncpg pool during pure unit tests.

    The real ``get_pool().acquire()`` is a production-only codepath —
    replacing the tiny helper with a direct-call passthrough keeps the
    test surface narrow to the aggregator's fan-out semantics. We still
    pass a sentinel ``conn`` kwarg so the stubbed sub-fn sees the same
    call shape as production (even though our stubs ignore it via
    ``**_k``).
    """
    async def _passthrough(fn, /, **kwargs):
        return await fn(conn=object(), **kwargs)
    monkeypatch.setattr(_dash, "_run_with_conn", _passthrough)


# ─── happy path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_happy_path_returns_all_11_ok(monkeypatch):
    """Every sub-query succeeds → every key is ``{ok: True, data: ...}``."""
    _stub_all(monkeypatch)
    _stub_pool_conn_passthrough(monkeypatch)

    result = await _dash.get_dashboard_summary()

    # All 11 keys present, in the contract order.
    assert list(result.keys()) == _SUB_KEYS

    # Every sub-key carries the ok envelope.
    for key in _SUB_KEYS:
        envelope = result[key]
        assert "ok" in envelope, f"{key} missing 'ok'"
        assert envelope["ok"] is True, f"{key} should be ok=True, got {envelope!r}"
        assert "data" in envelope, f"{key} missing 'data'"
        assert "error" not in envelope, f"{key} should not carry 'error' on happy path"


@pytest.mark.asyncio
async def test_summary_happy_path_preserves_subquery_payloads(monkeypatch):
    """Aggregator must not mutate / reshape sub-query payloads — the
    frontend demux (Phase 4-2) relies on passing them straight through
    to the existing state setters.
    """
    _stub_all(monkeypatch)
    _stub_pool_conn_passthrough(monkeypatch)

    result = await _dash.get_dashboard_summary()

    assert result["systemStatus"]["data"] == {"tasks_total": 3, "agents_running": 1}
    assert result["systemInfo"]["data"] == {"hostname": "test-host", "cpu_cores": 8}
    assert result["devices"]["data"] == [
        {"id": "usb-0-1", "name": "Test camera", "type": "camera"},
    ]
    assert result["tokenUsage"]["data"] == [
        {"model": "claude-opus-4-7", "total_tokens": 150},
    ]
    assert result["notificationsUnread"]["data"] == {"count": 2}
    assert result["simulations"]["data"] == [{"id": "sim-1", "status": "pass"}]


# ─── partial failure ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_partial_failure_keeps_200_and_marks_failed_key(monkeypatch):
    """One sub-query raising → that key carries ``{ok: False, error:
    "..."}`` while the other 10 still succeed.

    This is the contract that makes the aggregator safe to adopt — if
    a single failure could take down the whole response, the frontend
    would lose every panel on any transient DB blip, which is worse
    than the current per-panel fault-tolerance.
    """
    _stub_all(monkeypatch)
    _stub_pool_conn_passthrough(monkeypatch)

    # Swap the token-usage fn for one that raises; leave the other 10
    # stubs intact.
    async def _boom(*_a, **_k):
        raise RuntimeError("token provider offline")

    monkeypatch.setattr(_system_router, "get_token_usage", _boom)

    result = await _dash.get_dashboard_summary()

    # Failing sub-key carries the error envelope.
    assert result["tokenUsage"]["ok"] is False
    assert "data" not in result["tokenUsage"]
    error_msg = result["tokenUsage"]["error"]
    assert "RuntimeError" in error_msg
    assert "token provider offline" in error_msg

    # Other 10 keys remain ok.
    for key in _SUB_KEYS:
        if key == "tokenUsage":
            continue
        assert result[key]["ok"] is True, (
            f"{key} should still be ok=True when a sibling fails, "
            f"got {result[key]!r}"
        )


@pytest.mark.asyncio
async def test_summary_partial_failure_on_pg_subquery(monkeypatch):
    """The two PG-bound sub-queries (``unread_count``, ``list_simulations``)
    share the same failure path as the non-PG ones. A raise inside the
    pool-acquire wrapper must still surface as an envelope error, not
    bubble out of the aggregator as a 500.
    """
    _stub_all(monkeypatch)
    _stub_pool_conn_passthrough(monkeypatch)

    async def _boom(*_a, **_k):
        raise ConnectionError("PG pool exhausted")

    monkeypatch.setattr(_simulations_router, "list_simulations", _boom)

    result = await _dash.get_dashboard_summary()

    assert result["simulations"]["ok"] is False
    assert "ConnectionError" in result["simulations"]["error"]
    assert "PG pool exhausted" in result["simulations"]["error"]

    # Unrelated PG sub-query (``notificationsUnread``) keeps working —
    # pool acquisition is per-subquery, not aggregator-scoped.
    assert result["notificationsUnread"]["ok"] is True
    assert result["notificationsUnread"]["data"] == {"count": 2}


@pytest.mark.asyncio
async def test_summary_all_eleven_failing_still_returns_200_envelope(monkeypatch):
    """Pathological case: every sub-query raises. Aggregator should
    still return an HTTP 200 response (here: return normally, no
    exception) with all 11 keys carrying error envelopes.

    The frontend renders empty-state panels per-subkey; a 500 would
    blank the whole dashboard, which is what Phase 4 is trying to
    avoid.
    """
    async def _boom(*_a, **_k):
        raise ValueError("everything is down")

    for fn_name in (
        "get_system_status", "get_system_info", "get_devices", "get_spec",
        "get_repos", "get_logs", "get_token_usage", "get_token_budget",
        "unread_count", "get_compression_stats",
    ):
        monkeypatch.setattr(_system_router, fn_name, _boom)
    monkeypatch.setattr(_simulations_router, "list_simulations", _boom)
    _stub_pool_conn_passthrough(monkeypatch)

    result = await _dash.get_dashboard_summary()

    assert list(result.keys()) == _SUB_KEYS
    for key in _SUB_KEYS:
        envelope = result[key]
        assert envelope["ok"] is False, f"{key} should be ok=False"
        assert "ValueError" in envelope["error"]
        assert "everything is down" in envelope["error"]
