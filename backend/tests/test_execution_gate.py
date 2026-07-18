"""OP-2673 — default-safe dormant execution-gate tests."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.agents import execution_gate
from backend.agents.execution_contract import (
    Applied,
    DefinitelyNotApplied,
    StoredAction,
    resolve_terminal,
)
from backend.agents.execution_service import run_resume_job
from backend.tests.test_resume_worker import (
    _clear_resume_queue,
    _grant_state,
    _resume_state,
    _seed_case,
)


IDENTITY = {
    "tenant_id": "tenant-1",
    "principal_type": "service",
    "actor_id": "actor-1",
    "adapter_namespace": "workspace",
    "tool_name": "write_file",
}


def _enable_matching_pair(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_EXECUTE", "1")
    monkeypatch.setenv(
        "OMNISIGHT_U6_EXECUTE_ALLOWLIST",
        "workspace:write_file",
    )


def _stored_action() -> StoredAction:
    return StoredAction(
        grant_id="grant-1",
        idempotency_key=None,
        recovery_mode="non_replayable",
        adapter_namespace="workspace",
        tool_name="write_file",
        schema_version="v1",
        canonical_target="/workspace/result.txt",
        executable_args={"content": "unchanged"},
    )


@pytest.mark.asyncio
async def test_authorizer_default_denies_allowlisted_pair(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_U6_EXECUTE", raising=False)
    monkeypatch.setenv(
        "OMNISIGHT_U6_EXECUTE_ALLOWLIST",
        "workspace:write_file",
    )

    assert await execution_gate.authorizer(IDENTITY) is False


@pytest.mark.asyncio
async def test_authorizer_flag_on_pair_not_allowlisted_denies(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_EXECUTE", "true")
    monkeypatch.setenv(
        "OMNISIGHT_U6_EXECUTE_ALLOWLIST",
        "workspace:read_file",
    )

    assert await execution_gate.authorizer(IDENTITY) is False


@pytest.mark.asyncio
async def test_authorizer_flag_on_pair_allowlisted_allows(monkeypatch) -> None:
    _enable_matching_pair(monkeypatch)

    assert await execution_gate.authorizer(IDENTITY) is True


@pytest.mark.asyncio
async def test_authorizer_flag_on_empty_allowlist_denies(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_EXECUTE", "yes")
    monkeypatch.delenv("OMNISIGHT_U6_EXECUTE_ALLOWLIST", raising=False)

    assert await execution_gate.authorizer(IDENTITY) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("key", IDENTITY)
@pytest.mark.parametrize("bad_value", [None, "", 7])
async def test_authorizer_denies_invalid_any_identity_field(
    monkeypatch,
    key: str,
    bad_value: object,
) -> None:
    _enable_matching_pair(monkeypatch)
    identity = dict(IDENTITY)
    if bad_value is None:
        identity.pop(key)
    else:
        identity[key] = bad_value

    assert await execution_gate.authorizer(identity) is False


@pytest.mark.asyncio
async def test_authorizer_denies_colon_in_adapter_namespace(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_EXECUTE", "on")
    monkeypatch.setenv(
        "OMNISIGHT_U6_EXECUTE_ALLOWLIST",
        "work:space:write_file",
    )
    identity = {**IDENTITY, "adapter_namespace": "work:space"}

    assert await execution_gate.authorizer(identity) is False


class _RaisingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: object = None) -> object:
        raise RuntimeError(f"cannot read {key}")


@pytest.mark.asyncio
async def test_authorizer_fails_closed_when_mapping_get_raises(monkeypatch) -> None:
    _enable_matching_pair(monkeypatch)

    assert await execution_gate.authorizer(_RaisingMapping()) is False


def test_execute_allowlist_parses_and_rejects_malformed_entries(monkeypatch) -> None:
    monkeypatch.setenv(
        "OMNISIGHT_U6_EXECUTE_ALLOWLIST",
        " workspace:write_file, -bad:tool, colonless, mail:send, --flag ",
    )

    assert execution_gate.execute_allowlist() == frozenset({
        "workspace:write_file",
        "mail:send",
    })


class _CustomResult:
    def __str__(self) -> str:
        return "custom-result"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"bytes", "b'bytes'"),
        (datetime(2026, 7, 17, tzinfo=UTC), "2026-07-17 00:00:00+00:00"),
        (_CustomResult(), "custom-result"),
    ],
)
def test_result_of_serializes_non_json_values_without_raising(
    value: object,
    expected: str,
) -> None:
    source = {"value": value}
    outcome = Applied(result=source, evidence="sink receipt")

    assert dict(outcome.result) == source
    receipt = execution_gate.result_of(outcome)

    assert isinstance(receipt, str)
    assert json.loads(receipt)["result"]["value"] == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_result_of_nan_and_infinity_use_bounded_fallback(value: float) -> None:
    receipt = execution_gate.result_of(
        Applied(result={"value": value}, evidence="sink receipt"),
    )

    assert isinstance(receipt, str)
    assert json.loads(receipt) == {
        "result_serialization": "unavailable",
        "evidence": "sink receipt",
    }


def test_result_of_oversize_result_uses_bounded_fallback() -> None:
    receipt = execution_gate.result_of(
        Applied(result={"value": "x" * 70_000}, evidence="sink receipt"),
    )

    assert len(receipt.encode("utf-8")) <= execution_gate._MAX_RECEIPT_BYTES
    assert json.loads(receipt) == {
        "result_serialization": "oversize",
        "evidence": "sink receipt",
    }


def test_result_of_normal_result_is_deterministic_parseable_json() -> None:
    outcome = Applied(
        result={"z": 2, "a": 1},
        evidence="sink receipt",
    )

    first = execution_gate.result_of(outcome)
    second = execution_gate.result_of(outcome)

    assert first == second
    assert first == '{"evidence":"sink receipt","result":{"a":1,"z":2}}'
    assert json.loads(first) == {
        "evidence": "sink receipt",
        "result": {"a": 1, "z": 2},
    }


@pytest.mark.asyncio
async def test_noop_executor_returns_definitely_not_applied_without_mutation() -> None:
    stored = _stored_action()

    outcome = await execution_gate.noop_executor(stored)

    assert outcome == DefinitelyNotApplied(
        error="",
        evidence="noop: real execution not implemented (GAP-5d)",
    )
    assert not isinstance(outcome, Applied)
    assert stored == _stored_action()


def test_noop_outcome_resolves_to_failed_without_result_write() -> None:
    outcome = DefinitelyNotApplied(error="", evidence="noop")

    plan = resolve_terminal(outcome, "non_replayable")

    assert plan.grant_next == "failed"
    assert plan.resume_next == "failed"
    assert plan.record_attempt is True
    assert plan.write_result is False


def test_mint_attempt_id_has_prefix_hex_shape_and_is_unique() -> None:
    first = execution_gate.mint_attempt_id()
    second = execution_gate.mint_attempt_id()

    assert re.fullmatch(r"exec-[0-9a-f]{32}", first)
    assert re.fullmatch(r"exec-[0-9a-f]{32}", second)
    assert first != second


@pytest.mark.asyncio
async def test_default_deny_resume_loop_fails_without_calling_executor(
    pg_test_pool,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OMNISIGHT_U6_EXECUTE", raising=False)
    monkeypatch.setenv(
        "OMNISIGHT_U6_EXECUTE_ALLOWLIST",
        "workspace:write_file",
    )
    await _clear_resume_queue(pg_test_pool)
    case = await _seed_case(pg_test_pool)

    async def executor(_stored: StoredAction):
        pytest.fail("default-deny claim path must not call the executor")

    result = await run_resume_job(
        pg_test_pool,
        worker_id="worker-default-deny",
        lease_ttl_seconds=60,
        executor=executor,
        authorizer=execution_gate.authorizer,
        attempt_id_factory=execution_gate.mint_attempt_id,
        result_of=execution_gate.result_of,
    )

    assert result == "failed"
    assert await _grant_state(pg_test_pool, case) == "failed"
    assert await _resume_state(pg_test_pool, case) == "failed"


def test_execution_gate_has_no_production_caller() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    defining_module = backend_root / "agents" / "execution_gate.py"
    # resume_loop.py (default-OFF, OMNISIGHT_U6_RESUME_LOOP_ENABLED) wires the
    # gate's authorizer/mint/result_of into the supervised loop (GAP-5c-loop-C);
    # workspace_root_resolver.py only cites it as a docstring precedent. Both
    # keep the gate dormant-by-gate. Any OTHER caller is a wiring smell.
    allowed = {"resume_loop.py", "workspace_root_resolver.py"}
    offenders: list[str] = []

    for py in backend_root.rglob("*.py"):
        rel = py.relative_to(backend_root)
        if py == defining_module:
            continue
        if rel.parts[0] == "tests" or rel.parts[:2] == ("alembic", "versions"):
            continue
        if py.name in allowed:
            continue
        if "execution_gate" in py.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(rel))

    assert offenders == [], (
        f"execution_gate reachable only via the default-OFF loop; "
        f"unexpected caller: {offenders}"
    )
