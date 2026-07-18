"""Offline tests for the U6-0 GAP-5c-loop sub-leaf C wiring (no PostgreSQL)."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from backend.agents import execution_gate
from backend.agents import resume_loop
from backend.agents.execution_contract import DefinitelyNotApplied
from backend.agents.execution_contract import StoredAction
from backend.agents.resume_driver import DriveStats

_ENV = resume_loop._ENABLE_ENV
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]     # /repo/backend
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]        # /repo
_MODULE_PATH = _BACKEND_ROOT / "agents" / "resume_loop.py"      # the defining module (excluded from the caller scan)


class _BoomPool:
    def __getattr__(self, name: str) -> object:
        raise AssertionError("pool touched while the loop is disabled")


def _boom_resolver(workspace_id: str) -> int:
    raise AssertionError("resolver touched while the loop is disabled")


def _load_script():
    path = _REPO_ROOT / "scripts" / "run_u6_resume_loop.py"
    spec = importlib.util.spec_from_file_location("run_u6_resume_loop", path)
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
def test_resume_loop_enabled_flag(monkeypatch: pytest.MonkeyPatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, value)
    assert resume_loop.resume_loop_enabled() is expected


@pytest.mark.asyncio
async def test_disabled_is_a_noop_with_no_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    def _fail(*_a, **_k):
        raise AssertionError("run_resume_job called while disabled")

    def _fail_build(*_a, **_k):
        raise AssertionError("make_dispatch_executor called while disabled")

    monkeypatch.setattr(resume_loop, "run_resume_job", _fail)
    monkeypatch.setattr(resume_loop, "make_dispatch_executor", _fail_build)   # prove the executor is not even built
    stats = await resume_loop.run_supervised_resume_loop(
        _BoomPool(),
        worker_id="w",
        lease_ttl_seconds=1,
        poll_interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        drain_when_idle=True,
        should_continue=lambda: True,
        resolve_root_fd=_boom_resolver,
    )
    assert stats == DriveStats(0, 0, 0, 0, "loop_disabled")


@pytest.mark.asyncio
async def test_enabled_wires_the_exact_audited_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    seen: dict = {}

    async def _spy(pool, *, worker_id, lease_ttl_seconds, executor, authorizer, attempt_id_factory, result_of):
        seen.update(
            pool=pool,
            worker_id=worker_id,
            lease_ttl_seconds=lease_ttl_seconds,
            executor=executor,
            authorizer=authorizer,
            attempt_id_factory=attempt_id_factory,
            result_of=result_of,
        )
        return "idle"

    monkeypatch.setattr(resume_loop, "run_resume_job", _spy)
    stats = await resume_loop.run_supervised_resume_loop(
        "POOL",
        worker_id="wk",
        lease_ttl_seconds=9,
        poll_interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        drain_when_idle=True,
        should_continue=lambda: True,
        resolve_root_fd=_boom_resolver,
    )
    assert stats.stopped_reason == "idle_drained"
    assert seen["pool"] == "POOL"
    assert seen["worker_id"] == "wk"
    assert seen["lease_ttl_seconds"] == 9
    assert seen["authorizer"] is execution_gate.authorizer
    assert seen["attempt_id_factory"] is execution_gate.mint_attempt_id
    assert seen["result_of"] is execution_gate.result_of
    assert callable(seen["executor"])


@pytest.mark.asyncio
async def test_enabled_executor_is_the_dispatch_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    captured: dict = {}

    async def _spy(pool, *, executor, **_k):
        captured["executor"] = executor
        return "idle"

    monkeypatch.setattr(resume_loop, "run_resume_job", _spy)
    await resume_loop.run_supervised_resume_loop(
        "POOL",
        worker_id="wk",
        lease_ttl_seconds=9,
        poll_interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        drain_when_idle=True,
        should_continue=lambda: True,
        resolve_root_fd=_boom_resolver,
    )
    # A non-Write StoredAction falls through the dispatch executor to the inline no-op (proves it is the dispatcher).
    stored = StoredAction(
        grant_id="g",
        idempotency_key=None,
        recovery_mode="non_replayable",
        adapter_namespace="x",
        tool_name="y",
        schema_version="v1",
        canonical_target="t",
        executable_args={},
    )
    outcome = await captured["executor"](stored)
    assert isinstance(outcome, DefinitelyNotApplied)
    assert "noop" in outcome.evidence


@pytest.mark.asyncio
async def test_enabled_binds_the_injected_resolver_on_write(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proves the resolver PASSED to run_supervised_resume_loop is the one the built executor is closed over: a real
    # runner_sdk:Write StoredAction (valid args) reaches resolve_root_fd, which here records the workspace_id and
    # fails closed (None -> no_workspace_root).  The non-Write dispatch test above never exercises this binding.
    monkeypatch.setenv(_ENV, "1")
    captured: dict = {}
    seen_workspace_ids: list[str] = []

    def _recording_resolver(workspace_id: str) -> "int | None":
        seen_workspace_ids.append(workspace_id)
        return None                                     # fail closed -> DNA, no filesystem touched

    async def _spy(pool, *, executor, **_k):
        captured["executor"] = executor
        return "idle"

    monkeypatch.setattr(resume_loop, "run_resume_job", _spy)
    await resume_loop.run_supervised_resume_loop(
        "POOL",
        worker_id="wk",
        lease_ttl_seconds=9,
        poll_interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        drain_when_idle=True,
        should_continue=lambda: True,
        resolve_root_fd=_recording_resolver,
    )
    stored = StoredAction(
        grant_id="g",
        idempotency_key=None,
        recovery_mode="non_replayable",
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        canonical_target="t",
        executable_args={
            "workspace_id": "ws:sentinel-workspace-id",
            "relative_path": "sub/dir/file.txt",
            "content": "hi",
        },
    )
    outcome = await captured["executor"](stored)
    # The exact workspace_id off the StoredAction flowed, unchanged, into the injected resolver...
    assert seen_workspace_ids == ["ws:sentinel-workspace-id"]
    # ...and its fail-closed None short-circuited to DNA before any filesystem walk.
    assert isinstance(outcome, DefinitelyNotApplied)
    assert outcome.error == "no_workspace_root"


@pytest.mark.asyncio
async def test_should_continue_false_stops_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    ticks = {"n": 0}

    async def _spy(*_a, **_k):
        return "done"       # never idle; only the stop predicate ends it

    def _should_continue() -> bool:
        ticks["n"] += 1
        return ticks["n"] <= 2

    monkeypatch.setattr(resume_loop, "run_resume_job", _spy)
    stats = await resume_loop.run_supervised_resume_loop(
        "POOL",
        worker_id="wk",
        lease_ttl_seconds=9,
        poll_interval_s=1.0,
        error_backoff_s=1.0,
        max_consecutive_errors=3,
        drain_when_idle=False,
        should_continue=_should_continue,
    )
    assert stats.stopped_reason == "stop_predicate"
    assert stats.driven == 2


def test_only_the_operator_script_calls_the_loop_nothing_auto_spawns() -> None:
    # Dormancy (scoped to Python callers + import-time; a systemd/cron launcher running the operator script IS the
    # intended operation, not a violation): the loop function has exactly ONE Python caller in the code roots -- the
    # operator-run script.  Scan BOTH backend/ and scripts/ (a stray new script/daemon is the realistic auto-caller),
    # excluding the defining module itself (by exact path, not basename) + tests/versions.
    callers: list[str] = []
    for root in (_BACKEND_ROOT, _REPO_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            parts = path.parts
            if path == _MODULE_PATH or "tests" in parts or "versions" in parts:
                continue
            if "run_supervised_resume_loop" in path.read_text(encoding="utf-8"):
                callers.append(str(path.relative_to(_REPO_ROOT)))
    # Exactly one caller, the operator script (wiring exists but is operator-run, not auto-spawned).
    assert callers == ["scripts/run_u6_resume_loop.py"], f"the operator script must be the SOLE caller: {callers}"


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


def test_script_resolves_and_normalises_the_env_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    # The corrected default: OMNISIGHT_DATABASE_URL (then DATABASE_URL) is read, AND the project's canonical
    # postgresql+asyncpg:// form is normalised to the postgresql:// form asyncpg.create_pool accepts.  (The prior code
    # both used a dead env name and passed the +asyncpg URL through raw, which asyncpg rejects.)
    script = _load_script()
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert script._resolve_asyncpg_dsn("") == ""                                   # nothing set -> fail closed
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "postgresql+asyncpg://u:pw@h:5432/omni")
    assert script._resolve_asyncpg_dsn("") == "postgresql://u:pw@h:5432/omni"       # env read + +asyncpg stripped
    assert (
        script._resolve_asyncpg_dsn("postgres+asyncpg://u2:pw2@h2:5432/db2")
        == "postgresql://u2:pw2@h2:5432/db2"                                        # explicit --dsn wins + normalised
    )
    monkeypatch.delenv("OMNISIGHT_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u3:pw3@h3:5432/db3")
    assert script._resolve_asyncpg_dsn("") == "postgresql://u3:pw3@h3:5432/db3"     # DATABASE_URL is the fallback
    assert script._resolve_asyncpg_dsn("sqlite:////tmp/x.db") == ""       # non-Postgres -> fail closed -> rc 2
    assert script._resolve_asyncpg_dsn("postgresql://u:\udcff@h:5432/db") == ""  # lone surrogate -> UnicodeError caught


def test_script_dsn_whitespace_candidate_does_not_suppress_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: a whitespace-only OMNISIGHT_DATABASE_URL must fall THROUGH to DATABASE_URL, not swallow it (the prior
    # single .strip() after the `or`-chain returned '' and never consulted the fallback).  Each candidate is stripped
    # independently now.
    script = _load_script()
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", "   ")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:pw@h:5432/fallback")
    assert script._resolve_asyncpg_dsn("") == "postgresql://u:pw@h:5432/fallback"
