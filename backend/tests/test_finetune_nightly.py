"""Phase 65 S4 — nightly orchestrator + DE gate."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend import finetune_nightly as fn
from backend import finetune_backend as fb
from backend import finetune_eval as fe
from backend import finetune_export as fx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def fresh_db(monkeypatch):
    """Real sqlite — needed for DE.propose to write audit rows even
    though we don't seed any workflow_runs (export will return 0)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


@pytest.fixture(autouse=True)
def _reset_de():
    from backend import decision_engine as de
    de._reset_for_tests()
    yield
    de._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("level,expected", [
    (None, False), ("", False), ("off", False),
    ("l1", False), ("l3", False),
    ("l4", True), ("L4", True), ("l1+l4", True),
    ("all", True),
])
def test_is_enabled(monkeypatch, level, expected):
    if level is None:
        monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    else:
        monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", level)
    assert fn.is_enabled() is expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  nightly() — short-circuit paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_nightly_disabled_returns_disabled(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    out = await fn.nightly(baseline_model="b", base_model_for_finetune="b")
    assert out.status == "disabled"


@pytest.mark.asyncio
async def test_nightly_no_eligible_runs(fresh_db, monkeypatch, tmp_path):
    """Fresh DB has zero completed workflow_runs → exporter writes 0
    rows → status=no_eligible_runs."""
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        output_dir=tmp_path,
    )
    assert out.status == "no_eligible_runs"
    assert out.rows_written == 0


@pytest.mark.asyncio
async def test_nightly_below_min_rows_aborts(fresh_db, monkeypatch, tmp_path):
    """Force a non-zero export count just below the cap to exercise
    the threshold check."""
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")

    async def fake_export(out_path, **kw):
        Path(out_path).write_text("{}\n" * 5)
        return fx.ExportStats(written=5, output_path=str(out_path))
    monkeypatch.setattr(fx, "export_jsonl", fake_export)

    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        output_dir=tmp_path, min_rows_to_submit=10,
    )
    assert out.status == "below_min_rows"
    assert out.rows_written == 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Submit failures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FailSubmit:
    name = "x"
    def __init__(self, exc):
        self._exc = exc
    async def submit(self, *a, **kw):
        raise self._exc
    async def poll(self, h):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_nightly_submit_unavailable(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")

    async def fake_export(out_path, **kw):
        Path(out_path).write_text("{}\n" * 100)
        return fx.ExportStats(written=100, output_path=str(out_path))
    monkeypatch.setattr(fx, "export_jsonl", fake_export)

    backend = _FailSubmit(fb.BackendUnavailable("no SDK"))
    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        backend=backend, output_dir=tmp_path,
    )
    assert out.status == "submit_unavailable"
    assert "no SDK" in out.reason


@pytest.mark.asyncio
async def test_nightly_submit_runtime_error(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")

    async def fake_export(out_path, **kw):
        Path(out_path).write_text("{}\n" * 100)
        return fx.ExportStats(written=100, output_path=str(out_path))
    monkeypatch.setattr(fx, "export_jsonl", fake_export)

    backend = _FailSubmit(RuntimeError("network down"))
    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        backend=backend, output_dir=tmp_path,
    )
    assert out.status == "submit_error"
    assert "network down" in out.reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  End-to-end with NoopBackend (synthetic succeed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _stub_export(written: int):
    async def fake_export(out_path, **kw):
        Path(out_path).write_text("{}\n" * written)
        return fx.ExportStats(written=written, output_path=str(out_path))
    return fake_export


@pytest.mark.asyncio
async def test_nightly_promoted_when_eval_promotes(
    fresh_db, monkeypatch, tmp_path,
):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    monkeypatch.setattr(fx, "export_jsonl", await _stub_export(100))

    async def fake_eval(baseline, candidate, *, ask_fn, **kw):
        return fe.HoldoutEvaluation(
            baseline_model=baseline, candidate_model=candidate,
            benchmark_name="tiny",
            baseline_score=0.8, candidate_score=0.9,
            delta_pp=10.0, threshold_pp=5.0,
            decision="promote", reason="great",
        )
    monkeypatch.setattr(fe, "compare_models", fake_eval)

    out = await fn.nightly(
        baseline_model="base", base_model_for_finetune="base",
        backend=fb.NoopBackend(), output_dir=tmp_path,
        ask_fn=lambda *a, **kw: None,
    )
    assert out.status == "ok_promoted"
    assert out.eval and out.eval.decision == "promote"
    assert out.de_decision_id is not None

    # DE proposal landed with the right kind + severity.
    from backend import decision_engine as de
    matched = [d for d in (de.list_pending() + de.list_history(limit=5))
               if d.kind == "finetune/promote"]
    assert len(matched) == 1
    assert matched[0].severity == de.DecisionSeverity.routine
    assert matched[0].default_option_id == "accept"


@pytest.mark.asyncio
async def test_nightly_rejected_files_destructive_de_proposal(
    fresh_db, monkeypatch, tmp_path,
):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    monkeypatch.setattr(fx, "export_jsonl", await _stub_export(120))

    async def fake_eval(baseline, candidate, *, ask_fn, **kw):
        return fe.HoldoutEvaluation(
            baseline_model=baseline, candidate_model=candidate,
            benchmark_name="tiny",
            baseline_score=0.85, candidate_score=0.55,
            delta_pp=-30.0, threshold_pp=5.0,
            decision="reject", reason="huge regression",
        )
    monkeypatch.setattr(fe, "compare_models", fake_eval)

    out = await fn.nightly(
        baseline_model="base", base_model_for_finetune="base",
        backend=fb.NoopBackend(), output_dir=tmp_path,
        ask_fn=lambda *a, **kw: None,
    )
    assert out.status == "ok_rejected"
    assert out.eval and out.eval.decision == "reject"

    from backend import decision_engine as de
    matched = [d for d in (de.list_pending() + de.list_history(limit=5))
               if d.kind == "finetune/regression"]
    assert len(matched) == 1
    dec = matched[0]
    assert dec.severity == de.DecisionSeverity.destructive
    assert dec.default_option_id == "reject"
    assert {o["id"] for o in dec.options} == {"reject", "accept_anyway"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Poll loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _SlowJob:
    name = "slow"

    def __init__(self, terminal_after: int, terminal_state: str = "succeeded"):
        self._calls = 0
        self._after = terminal_after
        self._terminal = terminal_state

    async def submit(self, jsonl, *, base_model, suffix=""):
        return fb.JobHandle(backend=self.name, external_id="x",
                            submitted_at=0.0, metadata={"base_model": base_model})

    async def poll(self, handle):
        self._calls += 1
        if self._calls < self._after:
            return fb.JobStatus(handle=handle, state="running")
        return fb.JobStatus(handle=handle, state=self._terminal,
                            fine_tuned_model="ft:base:done"
                            if self._terminal == "succeeded" else None,
                            error="boom" if self._terminal == "failed" else None)


@pytest.mark.asyncio
async def test_poll_timeout_when_never_terminal(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    monkeypatch.setattr(fx, "export_jsonl", await _stub_export(100))

    backend = _SlowJob(terminal_after=999)
    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        backend=backend, output_dir=tmp_path,
        poll_interval_s=0.0, poll_max_attempts=2,
        ask_fn=lambda *a, **kw: None,
    )
    assert out.status == "poll_timeout"
    assert out.job_state == "running"


@pytest.mark.asyncio
async def test_poll_terminal_failed_records_job_failed(
    fresh_db, monkeypatch, tmp_path,
):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    monkeypatch.setattr(fx, "export_jsonl", await _stub_export(100))

    backend = _SlowJob(terminal_after=1, terminal_state="failed")
    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        backend=backend, output_dir=tmp_path,
        poll_interval_s=0.0, poll_max_attempts=2,
        ask_fn=lambda *a, **kw: None,
    )
    assert out.status == "job_failed"
    assert out.reason == "boom"


@pytest.mark.asyncio
async def test_poll_succeeded_but_no_model_returns_eval_skipped(
    fresh_db, monkeypatch, tmp_path,
):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    monkeypatch.setattr(fx, "export_jsonl", await _stub_export(100))

    class _OddBackend:
        name = "odd"
        async def submit(self, jsonl, *, base_model, suffix=""):
            return fb.JobHandle(backend="odd", external_id="x",
                                 submitted_at=0.0, metadata={})
        async def poll(self, handle):
            # succeeded but no fine_tuned_model — pathological backend
            return fb.JobStatus(handle=handle, state="succeeded",
                                 fine_tuned_model=None)

    out = await fn.nightly(
        baseline_model="b", base_model_for_finetune="b",
        backend=_OddBackend(), output_dir=tmp_path,
        poll_interval_s=0.0, poll_max_attempts=2,
        ask_fn=lambda *a, **kw: None,
    )
    assert out.status == "eval_skipped"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Loop guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_loop_singleton_then_cancel_clears_flag(fresh_db, monkeypatch):
    import asyncio as _aio
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l4")
    fn._LOOP_RUNNING = False

    task = _aio.create_task(fn.run_nightly_loop(interval_s=0.05))
    await _aio.sleep(0.01)
    assert fn._LOOP_RUNNING is True
    # Second start is no-op.
    result = await _aio.wait_for(fn.run_nightly_loop(interval_s=10), timeout=0.5)
    assert result is None

    task.cancel()
    try:
        await task
    except _aio.CancelledError:
        pass
    assert fn._LOOP_RUNNING is False
