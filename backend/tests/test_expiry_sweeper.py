"""Offline tests for the U6-0 GAP-6a expire-stale sweeper (no PostgreSQL)."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from backend.agents import expiry_sweeper
from backend.agents.expiry_sweeper import SweepStats

_ENV = expiry_sweeper._ENABLE_ENV
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]     # /repo/backend
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]        # /repo
_MODULE_PATH = _BACKEND_ROOT / "agents" / "expiry_sweeper.py"   # the defining module (excluded from the caller scan)


class _BoomPool:
    def __getattr__(self, name: str) -> object:
        raise AssertionError("pool touched while the sweep is disabled")


class _FakePool:
    """A minimal pool whose acquire() is an async CM yielding a sentinel connection."""

    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self):
                outer.acquired += 1
                return "CONN"

            async def __aexit__(self, *_a):
                return False

        return _CM()


async def _nosleep(_seconds: float) -> None:
    return None


def _load_script():
    path = _REPO_ROOT / "scripts" / "run_u6_expiry_sweep.py"
    spec = importlib.util.spec_from_file_location("run_u6_expiry_sweep", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False), ("", False), ("0", False), ("false", False),
        ("1", True), ("true", True), ("YES", True), ("on", True),
    ],
)
def test_expiry_sweep_enabled_flag(monkeypatch: pytest.MonkeyPatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, value)
    assert expiry_sweeper.expiry_sweep_enabled() is expected


@pytest.mark.asyncio
async def test_disabled_is_a_noop_with_no_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    async def _fail_expire(_conn):
        raise AssertionError("db.expire_stale called while disabled")

    monkeypatch.setattr(expiry_sweeper.db, "expire_stale", _fail_expire)
    stats = await expiry_sweeper.run_expiry_sweep_loop(
        _BoomPool(),
        interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        should_continue=lambda: True,
    )
    assert stats == SweepStats(0, 0, 0, 0, "sweep_disabled")


@pytest.mark.asyncio
async def test_enabled_drives_expire_stale_and_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    seen_conns: list = []
    swept: list = []

    async def _spy_expire(conn):
        seen_conns.append(conn)
        return {"challenges": 2, "grants": 3}

    monkeypatch.setattr(expiry_sweeper.db, "expire_stale", _spy_expire)
    pool = _FakePool()
    stats = await expiry_sweeper.run_expiry_sweep_loop(
        pool,
        interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        should_continue=lambda: True,
        max_ticks=3,
        on_sweep=lambda c, g: swept.append((c, g)),
        sleep=_nosleep,
    )
    # Exactly max_ticks sweeps, each over a freshly-acquired connection, counts accumulated.
    assert stats == SweepStats(3, 6, 9, 0, "max_ticks")
    assert seen_conns == ["CONN", "CONN", "CONN"]
    assert pool.acquired == 3
    assert swept == [(2, 3), (2, 3), (2, 3)]


@pytest.mark.asyncio
async def test_stop_predicate_ends_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    ticks = {"n": 0}

    async def _spy_expire(_conn):
        return {"challenges": 0, "grants": 0}

    def _should_continue() -> bool:
        ticks["n"] += 1
        return ticks["n"] <= 2       # true for 2 checks, then stop

    monkeypatch.setattr(expiry_sweeper.db, "expire_stale", _spy_expire)
    stats = await expiry_sweeper.run_expiry_sweep_loop(
        _FakePool(),
        interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        should_continue=_should_continue,
        sleep=_nosleep,
    )
    assert stats.stopped_reason == "stop_predicate"
    assert stats.ticks == 2


@pytest.mark.asyncio
async def test_consecutive_errors_trip_the_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")

    async def _boom_expire(_conn):
        raise RuntimeError("sweep boom")

    monkeypatch.setattr(expiry_sweeper.db, "expire_stale", _boom_expire)
    stats = await expiry_sweeper.run_expiry_sweep_loop(
        _FakePool(),
        interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        should_continue=lambda: True,
        sleep=_nosleep,
    )
    assert stats.stopped_reason == "circuit_open"
    assert stats.errors == 3
    assert stats.challenges_expired == 0


@pytest.mark.asyncio
async def test_a_success_resets_the_consecutive_error_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    calls = {"n": 0}

    async def _flaky_expire(_conn):
        calls["n"] += 1
        if calls["n"] in (1, 3):     # fail, succeed, fail -> never 2 consecutive with max=2
            raise RuntimeError("flaky")
        return {"challenges": 1, "grants": 0}

    monkeypatch.setattr(expiry_sweeper.db, "expire_stale", _flaky_expire)
    stats = await expiry_sweeper.run_expiry_sweep_loop(
        _FakePool(),
        interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=2,
        should_continue=lambda: True,
        max_ticks=4,
        sleep=_nosleep,
    )
    assert stats.stopped_reason == "max_ticks"     # the interleaved success reset the counter -> no circuit trip
    assert stats.errors == 2
    assert stats.challenges_expired == 2            # ticks 2 and 4 each expired 1 challenge


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"interval_s": 0.0}, "interval_s"),
        ({"error_backoff_s": 0.0}, "error_backoff_s"),
        ({"max_consecutive_errors": 0}, "max_consecutive_errors"),
        ({"max_ticks": -1}, "max_ticks"),
    ],
)
async def test_invalid_config_raises_when_enabled(monkeypatch: pytest.MonkeyPatch, kwargs, message) -> None:
    monkeypatch.setenv(_ENV, "1")
    base = dict(interval_s=1.0, error_backoff_s=1.0, max_consecutive_errors=3, should_continue=lambda: True)
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        await expiry_sweeper.run_expiry_sweep_loop(_FakePool(), **base)


def test_only_the_operator_script_calls_the_sweeper_nothing_auto_spawns() -> None:
    # Dormancy (scoped to Python callers + import-time; an operator systemd/cron running the script IS intended): the
    # sweeper loop has exactly ONE Python caller in the code roots -- the operator-run script.
    callers: list[str] = []
    for root in (_BACKEND_ROOT, _REPO_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            parts = path.parts
            if path == _MODULE_PATH or "tests" in parts or "versions" in parts:
                continue
            if "run_expiry_sweep_loop" in path.read_text(encoding="utf-8"):
                callers.append(str(path.relative_to(_REPO_ROOT)))
    assert callers == ["scripts/run_u6_expiry_sweep.py"], f"the operator script must be the SOLE caller: {callers}"


def test_script_disabled_returns_zero_without_touching_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    script = _load_script()

    def _fail(*_a, **_k):
        raise AssertionError("init_pool called while disabled")

    monkeypatch.setattr(script.db_pool, "init_pool", _fail)
    assert script.main([]) == 0


def test_script_enabled_without_dsn_returns_two_without_touching_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    script = _load_script()

    def _fail(*_a, **_k):
        raise AssertionError("init_pool called without a dsn")

    monkeypatch.setattr(script.db_pool, "init_pool", _fail)
    assert script.main(["--dsn", ""]) == 2
