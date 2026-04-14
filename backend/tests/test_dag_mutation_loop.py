"""Phase 56-DAG-C S2 — run_mutation_loop + DE exhausted."""

from __future__ import annotations

import pytest

from backend import dag_planner as dp
from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _t(task_id="T1", required_tier="t1", toolchain="cmake",
       expected_output="build/a.bin", **kw):
    return Task(task_id=task_id, description=f"t {task_id}",
                required_tier=required_tier, toolchain=toolchain,
                expected_output=expected_output, **kw)


def _good_dag(dag_id="REQ-good") -> DAG:
    return DAG(dag_id=dag_id, tasks=[
        _t(task_id="A", expected_output="build/a.bin"),
        _t(task_id="B", required_tier="t3", toolchain="flash_board",
           expected_output="logs/b.log",
           inputs=["build/a.bin"], depends_on=["A"]),
    ])


def _bad_dag(dag_id="REQ-bad") -> DAG:
    """One tier-violation — easy to fix by flipping required_tier."""
    return DAG(dag_id=dag_id, tasks=[
        _t(task_id="A", required_tier="t1", toolchain="flash_board",
           expected_output="logs/a.log"),
    ])


def _valid_response_for(dag: DAG) -> str:
    """Serialise a DAG as the orchestrator would — strict JSON."""
    return dag.model_dump_json()


@pytest.fixture(autouse=True)
def _reset_de():
    from backend import decision_engine as de
    de._reset_for_tests()
    yield
    de._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_initial_valid_dag_no_ask_call():
    """Pre-validated DAG returns immediately; ask_fn is NOT invoked."""
    calls = {"n": 0}

    async def ask(s, u):
        calls["n"] += 1
        return (_valid_response_for(_good_dag()), 10)

    res = await dp.run_mutation_loop(
        _good_dag(), ask_fn=ask, file_exhausted_proposal=False,
    )
    assert res.status == "validated"
    assert res.attempts == []
    assert res.total_tokens == 0
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_fix_in_first_round():
    """Orchestrator returns a valid DAG; loop exits after 1 round."""
    async def ask(s, u):
        return (_valid_response_for(_good_dag(dag_id="REQ-fixed")), 256)

    # Initial bad dag has a different id → orchestrator's dag_id is
    # forced back to the original.
    bad = _bad_dag(dag_id="REQ-fixed")
    res = await dp.run_mutation_loop(
        bad, ask_fn=ask, file_exhausted_proposal=False,
    )
    assert res.status == "validated"
    assert len(res.attempts) == 1
    assert res.attempts[0].tokens_used == 256
    assert res.total_tokens == 256
    assert res.final_dag.dag_id == "REQ-fixed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exhaustion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_exhausts_when_orchestrator_keeps_returning_bad_dags():
    """Every round the orchestrator hands back the same failing DAG →
    after 3 rounds we exhaust."""
    bad = _bad_dag(dag_id="REQ-stuck")

    async def ask(s, u):
        return (_valid_response_for(_bad_dag(dag_id="REQ-stuck")), 100)

    res = await dp.run_mutation_loop(
        bad, ask_fn=ask, file_exhausted_proposal=False,
    )
    assert res.status == "exhausted"
    assert len(res.attempts) == dp.MAX_MUTATION_ROUNDS
    # Every attempt spent tokens (parse succeeded but validator failed).
    assert all(a.tokens_used == 100 for a in res.attempts)
    # Budget still tallied.
    assert res.total_tokens == 300


@pytest.mark.asyncio
async def test_orchestrator_error_every_round_status_is_orchestrator_error():
    async def ask(s, u):
        return ("this is not json", 0)

    res = await dp.run_mutation_loop(
        _bad_dag(), ask_fn=ask, file_exhausted_proposal=False,
    )
    assert res.status == "orchestrator_error"
    assert all(a.dag_after is None for a in res.attempts)
    assert all(a.orchestrator_error for a in res.attempts)


@pytest.mark.asyncio
async def test_mixed_failures_then_exhausted_not_orchestrator_error():
    """If at least ONE round produced a (still-failing) DAG, status is
    exhausted, not orchestrator_error."""
    responses = iter([
        "not json",                                   # r1: parse fail
        _valid_response_for(_bad_dag(dag_id="REQ-1")),  # r2: still bad
        _valid_response_for(_bad_dag(dag_id="REQ-1")),  # r3: still bad
    ])

    async def ask(s, u):
        return (next(responses), 50)

    res = await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-1"), ask_fn=ask,
        file_exhausted_proposal=False,
    )
    assert res.status == "exhausted"
    assert len(res.attempts) == 3
    assert res.attempts[0].dag_after is None
    assert res.attempts[1].dag_after is not None
    assert res.attempts[2].dag_after is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Progressive convergence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_recovers_in_round_2():
    responses = iter([
        _valid_response_for(_bad_dag(dag_id="REQ-prog")),
        _valid_response_for(_good_dag(dag_id="REQ-prog")),
    ])

    async def ask(s, u):
        return (next(responses), 100)

    res = await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-prog"), ask_fn=ask,
        file_exhausted_proposal=False,
    )
    assert res.status == "validated"
    assert len(res.attempts) == 2
    # The failed round has errors_after; the successful one has none.
    assert res.attempts[0].errors_after
    assert res.attempts[1].errors_after == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision Engine exhausted proposal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_exhaustion_files_decision_engine_proposal():
    from backend import decision_engine as de

    async def ask(s, u):
        return (_valid_response_for(_bad_dag(dag_id="REQ-de")), 50)

    await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-de"),
        ask_fn=ask,
        file_exhausted_proposal=True,
    )
    # Look at both pending and history — Decision Engine may auto-
    # resolve destructive proposals depending on mode.
    all_decisions = de.list_pending() + de.list_history(limit=5)
    matched = [d for d in all_decisions if d.kind == "dag/exhausted"]
    assert len(matched) == 1
    dec = matched[0]
    assert dec.severity == de.DecisionSeverity.destructive
    assert dec.default_option_id == "abort"
    assert {o["id"] for o in dec.options} == {"abort", "accept_failed"}
    assert "REQ-de" in dec.title


@pytest.mark.asyncio
async def test_no_proposal_when_validated():
    from backend import decision_engine as de

    async def ask(s, u):
        return (_valid_response_for(_good_dag(dag_id="REQ-ok")), 10)

    await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-ok"),
        ask_fn=ask, file_exhausted_proposal=True,
    )
    all_decisions = de.list_pending() + de.list_history(limit=5)
    assert not any(d.kind == "dag/exhausted" for d in all_decisions)


@pytest.mark.asyncio
async def test_exhausted_proposal_best_effort_does_not_raise(monkeypatch):
    """DE failure during exhaustion must not take down the mutation
    loop's caller."""
    from backend import decision_engine as de

    def boom(*a, **kw):
        raise RuntimeError("DE offline")

    monkeypatch.setattr(de, "propose", boom)

    async def ask(s, u):
        return (_valid_response_for(_bad_dag(dag_id="REQ-swallow")), 50)

    # Must not raise.
    res = await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-swallow"),
        ask_fn=ask, file_exhausted_proposal=True,
    )
    assert res.status == "exhausted"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Budget bound
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_max_rounds_is_honoured_exactly():
    """A caller-supplied max_rounds=1 must stop after 1 attempt even
    if the orchestrator would eventually succeed."""
    responses = iter([
        _valid_response_for(_bad_dag(dag_id="REQ-cap")),
        _valid_response_for(_good_dag(dag_id="REQ-cap")),
    ])

    async def ask(s, u):
        return (next(responses), 10)

    res = await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-cap"),
        ask_fn=ask, max_rounds=1, file_exhausted_proposal=False,
    )
    assert res.status == "exhausted"
    assert len(res.attempts) == 1


@pytest.mark.asyncio
async def test_result_ok_property():
    async def ask(s, u):
        return (_valid_response_for(_good_dag(dag_id="REQ-ok2")), 1)
    res = await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-ok2"), ask_fn=ask,
        file_exhausted_proposal=False,
    )
    assert res.ok is True

    async def ask2(s, u):
        return (_valid_response_for(_bad_dag(dag_id="REQ-no")), 1)
    res2 = await dp.run_mutation_loop(
        _bad_dag(dag_id="REQ-no"), ask_fn=ask2,
        file_exhausted_proposal=False,
    )
    assert res2.ok is False
