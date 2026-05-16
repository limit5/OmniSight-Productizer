"""OP-953 H8 — Re-runnable smoke test for the L3 synthetic dry-run + load test.

Exercises ``scripts/spike_l3_load_test.py`` end to end so that:

* the three acceptance cases (synthetic happy run, 5-parallel load, latency
  under target) stay green — if a future H-track change breaks the L3 event
  router / worker / state machine / approval API, one of these flips to FAIL
  and forces ``docs/research/h8-l3-load-test-2026-05.md`` to be re-reviewed;
* the harness's environment isolation does not leak — the side-effect handler
  stubs (``operator.approval.*``, ``gerrit/hotfix-label-added``) and the test
  engines are restored after each ``isolated_l3_env()`` block;
* the report renderer produces well-formed markdown from a measured run.

Manual re-run: ``pytest tests/integration/test_l3_load_test.py -q`` or
``python3 scripts/spike_l3_load_test.py``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_PATH = REPO_ROOT / "scripts" / "spike_l3_load_test.py"


@pytest.fixture(scope="module")
def spike():
    """Import the spike harness as a module so we can call its case
    runners directly instead of shelling out."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("spike_l3_load_test", SPIKE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["spike_l3_load_test"] = module
    spec.loader.exec_module(module)
    return module


# ─── Test plan case 1 — synthetic happy run ──────────────────────────────
def test_case1_synthetic_release_walks_full_pipeline(spike):
    result = spike.run_case_1()
    assert result.passed, result.notes
    m = result.metrics
    assert m["final_state"] == spike.state_machine.STATE_DONE
    assert m["operator_clicks"] == 1
    assert m["jira_touches_by_operator"] == 0
    # every emitted event reached the terminal 'done' status
    assert all(r.status == spike.event_router.STATUS_DONE for r in result.events)
    # the canary stage edges advanced the state machine (ADR-0018 row 9)
    assert m["outcomes"].get("state_advanced") == 3
    assert m["ledger_chain_ok"] is True


# ─── Test plan case 2 — 5-parallel load ──────────────────────────────────
def test_case2_five_parallel_releases_no_loss_no_dlq(spike):
    result = spike.run_case_2()
    assert result.passed, result.notes
    m = result.metrics
    assert m["release_count"] == 5
    # AC #4: no event loss
    assert m["events_emitted"] == m["events_done"] == m["events_dispatched"]
    # AC #4: no DLQ
    assert m["events_dlq"] == 0
    # all five releases (rc + 2 customer + 2 hotfix) completed
    assert set(m["final_states"].values()) == {spike.state_machine.STATE_DONE}


# ─── Test plan case 3 — latency under target ─────────────────────────────
def test_case3_latency_under_target(spike):
    case1 = spike.run_case_1()
    case2 = spike.run_case_2()
    result = spike.run_case_3(case1, case2)
    assert result.passed, result.notes
    assert result.metrics["p95_ms"] < spike.LATENCY_TARGET_MS
    assert result.metrics["sample_count"] >= 1


# ─── Harness hygiene ─────────────────────────────────────────────────────
def test_isolated_env_restores_dispatch_table_and_engines(spike):
    before = dict(spike.event_handlers.HANDLER_TABLE)
    with spike.isolated_l3_env():
        # while inside: the side-effect handlers are stubbed
        assert spike.event_handlers.HANDLER_TABLE[("operator", "operator.approval.granted")] is \
            spike._operator_decision_stub
        assert spike.event_handlers.HANDLER_TABLE[("gerrit", "hotfix-label-added")] is \
            spike._hotfix_label_stub
        assert spike.event_router._test_engine is not None
    # after: the original table + engine wiring are back
    assert dict(spike.event_handlers.HANDLER_TABLE) == before
    assert spike.event_router._test_engine is None
    assert spike.state_machine._test_engine is None
    assert spike.compliance_ledger._test_engine is None


def test_render_report_emits_markdown(spike):
    case1 = spike.run_case_1()
    case2 = spike.run_case_2()
    case3 = spike.run_case_3(case1, case2)
    report = spike.render_report(case1, case2, case3, generated_at="2026-05-12")
    assert report.startswith("# Spike Report — H8 L3 event-driven release conductor")
    assert "## 5. Findings" in report
    assert "Finding F1" in report or "**F1 —" in report
    # go/no-go line is present and reflects the case results
    assert ("**GO**" in report) == (case1.passed and case2.passed and case3.passed)


def test_main_exits_zero_on_pass(spike):
    assert spike.main(["--json"]) == 0
