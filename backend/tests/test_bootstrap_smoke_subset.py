"""L6 Step 5 — ``POST /api/v1/bootstrap/smoke-subset`` tests.

Covers the wizard's Step-5 smoke runner: validates + submits the
compile-flash host_native DAG (DAG #1) and/or the cross-compile aarch64
DAG (DAG #2) from ``scripts/prod_smoke_test.py`` and verifies the
audit-log hash chain. On green (every selected DAG ok + audit intact)
the smoke_passed gate flips and the fifth gate turns green so finalize
becomes reachable.

Scenarios:

  * happy path — DAG #1 validates, workflow starts, audit chain intact →
    ``smoke_passed=True``, marker flipped, ``STEP_SMOKE`` recorded
  * audit chain broken → ``smoke_passed=False`` even when the DAG is
    perfect; marker + step row stay empty so finalize still refuses
  * DAG submit failure (workflow.start raises) → smoke reported as
    failed with the underlying error surfaced, marker untouched
  * endpoint stays wizard-scoped (unauthenticated path lives under
    ``/bootstrap/*`` — bootstrap-gate exemption tested elsewhere)
  * audit row ``bootstrap.smoke_subset`` emitted on every call
  * ``subset=both`` returns two run summaries (DAG #1 + DAG #2) so the
    wizard can display the "兩個 DAG 的 run summary" step
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend import audit as _audit
from backend import bootstrap as _boot


# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture()
def _marker_tmp():
    """Isolate the bootstrap marker file between tests."""
    tmp = tempfile.mkdtemp(prefix="omnisight_smoke_subset_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    try:
        yield
    finally:
        _boot._reset_for_tests()


async def _stub_audit_chain(monkeypatch, *, ok: bool, first_bad: int | None = None):
    """Stub out ``audit.verify_all_chains`` with a fixed outcome."""

    async def fake() -> dict:
        if ok:
            return {"tenant-a": (True, None), "tenant-b": (True, None)}
        return {"tenant-a": (True, None), "tenant-b": (False, first_bad or 42)}

    monkeypatch.setattr(_audit, "verify_all_chains", fake)


# ── happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_happy_path_flips_gate(
    client, monkeypatch, _marker_tmp,
):
    """DAG validates + audit chain intact → smoke_passed True, gate flips."""
    await _stub_audit_chain(monkeypatch, ok=True)

    # Gate starts red — the shared `client` fixture pins status to green
    # via a monkeypatch on `get_bootstrap_status`, but the marker is a
    # separate store. Confirm the marker is empty pre-run.
    assert _boot._read_marker().get("smoke_passed") is not True

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        json={"subset": "dag1"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["smoke_passed"] is True, body
    assert body["subset"] == "dag1"
    assert isinstance(body["elapsed_ms"], int) and body["elapsed_ms"] >= 0
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["ok"] is True
    assert run["dag_id"] == "smoke-compile-flash-host-native"
    assert run["task_count"] == 2
    assert run["target_platform"] == "host_native"
    assert run["plan_status"] in ("validated", "executing")
    assert run["run_id"]
    assert run["plan_id"] is not None
    assert body["audit_chain"]["ok"] is True
    assert body["audit_chain"]["first_bad_id"] is None
    assert body["audit_chain"]["tenant_count"] == 2
    assert body["audit_chain"]["bad_tenants"] == []

    # Marker + bootstrap_state both reflect the successful smoke.
    assert _boot._read_marker().get("smoke_passed") is True
    recorded = await _boot.get_bootstrap_step(_boot.STEP_SMOKE)
    assert recorded is not None
    assert recorded["metadata"]["subset"] == "dag1"
    assert recorded["metadata"]["run_id"] == run["run_id"]
    # Per-DAG summary trail preserved for post-finalize observability.
    assert recorded["metadata"]["dag_runs"][0]["dag_id"] == run["dag_id"]
    assert recorded["metadata"]["audit_tenant_count"] == 2


# ── audit chain broken ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_audit_chain_broken_keeps_gate_red(
    client, monkeypatch, _marker_tmp,
):
    """Audit chain corrupted → smoke reported as failed, marker untouched."""
    await _stub_audit_chain(monkeypatch, ok=False, first_bad=17)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        json={"subset": "dag1"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["smoke_passed"] is False
    # DAG itself still validates — the failure was on the audit side.
    assert body["runs"][0]["ok"] is True
    assert body["audit_chain"]["ok"] is False
    assert body["audit_chain"]["first_bad_id"] == 17
    assert "tenant-b" in body["audit_chain"]["detail"]

    # Marker stays empty → finalize gate stays red.
    assert _boot._read_marker().get("smoke_passed") is not True
    recorded = await _boot.get_bootstrap_step(_boot.STEP_SMOKE)
    assert recorded is None


# ── DAG submit failure ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_dag_start_failure_surfaces_error(
    client, monkeypatch, _marker_tmp,
):
    """workflow.start raising → run.ok=False, smoke_passed=False."""
    await _stub_audit_chain(monkeypatch, ok=True)

    from backend.routers import bootstrap as _br

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated workflow start failure")

    # Patch the lazily-imported workflow module inside the endpoint path.
    import backend.workflow as _wf

    monkeypatch.setattr(_wf, "start", boom)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        json={"subset": "dag1"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()

    assert body["smoke_passed"] is False
    run = body["runs"][0]
    assert run["ok"] is False
    assert run["run_id"] is None
    assert run["plan_id"] is None
    # Error captured in validation_errors with our synthetic message.
    assert any(
        e["rule"] == "workflow_start"
        and "simulated workflow start failure" in e["message"]
        for e in run["validation_errors"]
    )
    # Marker untouched.
    assert _boot._read_marker().get("smoke_passed") is not True
    assert await _boot.get_bootstrap_step(_boot.STEP_SMOKE) is None

    # Defensive ref so the import above isn't dropped by the linter.
    assert _br is not None


# ── default body accepted ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_accepts_empty_body(
    client, monkeypatch, _marker_tmp,
):
    """Bare POST (no body) defaults to subset=dag1."""
    await _stub_audit_chain(monkeypatch, ok=True)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subset"] == "dag1"
    assert body["smoke_passed"] is True


# ── subset=both runs DAG #1 + DAG #2 ─────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_both_runs_two_dags(
    client, monkeypatch, _marker_tmp,
):
    """``subset=both`` returns a run summary for each DAG shipped in
    ``scripts/prod_smoke_test.py`` so the wizard's Step-5 pane can
    display the "兩個 DAG 的 run summary" required by L6 Step 5."""
    await _stub_audit_chain(monkeypatch, ok=True)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        json={"subset": "both"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["subset"] == "both"
    assert body["smoke_passed"] is True
    assert len(body["runs"]) == 2, body["runs"]

    dag_ids = sorted(r["dag_id"] for r in body["runs"])
    assert dag_ids == [
        "smoke-compile-flash-host-native",
        "smoke-cross-compile-aarch64",
    ]
    for run in body["runs"]:
        assert run["ok"] is True, run
        assert run["plan_status"] in ("validated", "executing")
        assert run["run_id"]
        assert run["plan_id"] is not None
        assert run["key"] in ("dag1", "dag2")
        assert run["task_count"] == 2

    # Persisted per-DAG trail matches the runs returned in the response.
    recorded = await _boot.get_bootstrap_step(_boot.STEP_SMOKE)
    assert recorded is not None
    persisted_ids = sorted(
        entry["dag_id"] for entry in recorded["metadata"]["dag_runs"]
    )
    assert persisted_ids == dag_ids


# ── subset=dag2 runs only DAG #2 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_dag2_runs_only_aarch64(
    client, monkeypatch, _marker_tmp,
):
    """``subset=dag2`` runs the aarch64 cross-compile DAG alone."""
    await _stub_audit_chain(monkeypatch, ok=True)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        json={"subset": "dag2"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["subset"] == "dag2"
    assert body["smoke_passed"] is True
    assert len(body["runs"]) == 1
    assert body["runs"][0]["dag_id"] == "smoke-cross-compile-aarch64"
    assert body["runs"][0]["target_platform"] == "aarch64"
    assert body["runs"][0]["key"] == "dag2"


# ── unknown subset rejected ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_rejects_unknown_subset(
    client, monkeypatch, _marker_tmp,
):
    """Only ``dag1`` / ``dag2`` / ``both`` are accepted — anything else 422s."""
    await _stub_audit_chain(monkeypatch, ok=True)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        json={"subset": "dag3"},
        follow_redirects=False,
    )
    assert r.status_code == 422, r.text


# ── audit row emitted ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_subset_emits_audit_row(
    client, monkeypatch, _marker_tmp,
):
    """Every call logs a ``bootstrap.smoke_subset`` audit row."""
    await _stub_audit_chain(monkeypatch, ok=True)

    captured: list[dict] = []

    async def fake_log(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(_audit, "log", fake_log)

    r = await client.post(
        "/api/v1/bootstrap/smoke-subset",
        follow_redirects=False,
    )
    assert r.status_code == 200
    rows = [c for c in captured if c.get("action") == "bootstrap.smoke_subset"]
    assert rows, f"expected bootstrap.smoke_subset audit, got: {captured}"
    row = rows[-1]
    assert row["entity_kind"] == "bootstrap"
    assert row["entity_id"] == _boot.STEP_SMOKE
    assert row["after"]["smoke_passed"] is True
    assert row["after"]["subset"] == "dag1"
    assert row["after"]["dag_runs"][0]["dag_id"] == (
        "smoke-compile-flash-host-native"
    )
    assert row["after"]["audit_chain"]["ok"] is True
    assert row["after"]["audit_chain"]["tenant_count"] == 2
    assert row["actor"] == "wizard"
