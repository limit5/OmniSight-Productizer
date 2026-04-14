"""Phase 63-D D3 — nightly orchestrator: persistence, regression, notify."""

from __future__ import annotations

import os
import tempfile

import pytest

from backend import iq_benchmark as ib
from backend import iq_nightly as iqn
from backend import iq_runner as ir


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def fresh_db(monkeypatch):
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


def _bench(name: str) -> ib.IQBenchmark:
    return ib.IQBenchmark(
        name=name, schema_version=1, description="",
        questions=[ib.IQQuestion(id="q1", prompt="say alpha",
                                  expected_keywords=["alpha"])],
    )


def _report(model: str, bench: str, score: float,
            tokens: int = 100, truncated: str | None = None) -> ir.RunReport:
    # Synthesise a RunReport with a score matching `score` value.
    qs = [ib.IQQuestion(id="q1", prompt="?", expected_keywords=["x"])]
    passed = score > 0
    bscore = ib.BenchmarkScore(
        benchmark=bench, model=model,
        pass_count=1 if passed else 0,
        total_count=1,
        weighted_score=score,
        per_question=[("q1", passed)],
    )
    return ir.RunReport(score=bscore, tokens_used=tokens,
                        truncated_at_question=truncated)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("level,expected", [
    ("off", False), ("", False), (None, False),
    ("l1", False), ("L3", True), ("l1+l3", True), ("all", True),
])
def test_is_enabled_honours_level(monkeypatch, level, expected):
    if level is None:
        monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    else:
        monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", level)
    assert iqn.is_enabled() is expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  persist + recent_scores
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_persist_and_read_back(fresh_db):
    reports = [
        _report("m1", "setA", 0.9),
        _report("m1", "setB", 0.7),
        _report("m2", "setA", 0.5),
    ]
    n = await iqn.persist_run(reports)
    assert n == 3
    rows = await iqn.recent_scores("m1")
    assert len(rows) == 2
    assert {r.benchmark for r in rows} == {"setA", "setB"}


@pytest.mark.asyncio
async def test_recent_scores_filters_by_days(fresh_db):
    # Write one row from 10 days ago and one from today.
    old_ts = 1_000_000.0
    new_ts = old_ts + 10 * iqn.SECONDS_PER_DAY
    await iqn.persist_run([_report("m1", "s", 0.8)], ts=old_ts)
    await iqn.persist_run([_report("m1", "s", 0.9)], ts=new_ts)

    rows = await iqn.recent_scores("m1", days=7, now=new_ts)
    assert len(rows) == 1
    assert rows[0].ts == new_ts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  detect_regression
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_regression_insufficient_samples(fresh_db):
    await iqn.persist_run([_report("m1", "s", 0.8)])
    reg, reason = await iqn.detect_regression("m1")
    assert not reg
    assert "insufficient samples" in reason or "only 1 calendar day" in reason


async def _write_day(model: str, score: float, day_offset: int, *, base_ts: float):
    await iqn.persist_run(
        [_report(model, "s", score)],
        ts=base_ts + day_offset * iqn.SECONDS_PER_DAY,
    )


@pytest.mark.asyncio
async def test_regression_triggers_on_two_low_days(fresh_db):
    base = 1_700_000_000.0
    # Baseline pool: 5 days at 0.9.
    for i in range(-6, -1):
        await _write_day("m1", 0.9, i, base_ts=base)
    # Latest two days drop to 0.6 (way below 0.9 - 10pp = 0.8).
    await _write_day("m1", 0.6, -1, base_ts=base)
    await _write_day("m1", 0.55, 0, base_ts=base)

    reg, reason = await iqn.detect_regression(
        "m1", threshold_pp=10, baseline_days=5, now=base,
    )
    assert reg, reason
    assert "baseline" in reason


@pytest.mark.asyncio
async def test_regression_does_not_trigger_on_single_bad_day(fresh_db):
    base = 1_700_000_000.0
    for i in range(-6, -1):
        await _write_day("m1", 0.9, i, base_ts=base)
    # Only the latest day dropped.
    await _write_day("m1", 0.9, -1, base_ts=base)
    await _write_day("m1", 0.5, 0, base_ts=base)

    reg, _ = await iqn.detect_regression("m1", threshold_pp=10,
                                         baseline_days=5, now=base)
    assert not reg


@pytest.mark.asyncio
async def test_regression_does_not_trigger_on_shallow_dip(fresh_db):
    base = 1_700_000_000.0
    for i in range(-6, -1):
        await _write_day("m1", 0.85, i, base_ts=base)
    # Dip is ~5pp, less than the 10pp threshold.
    await _write_day("m1", 0.80, -1, base_ts=base)
    await _write_day("m1", 0.79, 0, base_ts=base)

    reg, _ = await iqn.detect_regression("m1", threshold_pp=10,
                                         baseline_days=5, now=base)
    assert not reg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  nightly() orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_nightly_no_op_when_disabled(fresh_db, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    out = await iqn.nightly(["m1"], ask_fn=lambda *a: None)
    assert out == []


@pytest.mark.asyncio
async def test_nightly_runs_end_to_end(fresh_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l3")

    # Stub load_all → single tiny benchmark so we don't depend on shipped YAML.
    bench = _bench("tiny")
    monkeypatch.setattr(iqn.ib, "load_all", lambda *a, **kw: [bench])

    async def ask(model, prompt):
        if "alpha" in prompt:
            return ("alpha yes", 30)
        return ("", 0)

    reports = await iqn.nightly(["m1"], ask_fn=ask)
    assert len(reports) == 1
    assert reports[0].score.pass_count == 1

    # Row persisted.
    rows = await iqn.recent_scores("m1")
    assert len(rows) == 1
    assert rows[0].model == "m1"
    assert rows[0].weighted_score == 1.0


@pytest.mark.asyncio
async def test_nightly_empty_benchmark_list_is_noop(fresh_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l3")
    monkeypatch.setattr(iqn.ib, "load_all", lambda *a, **kw: [])

    async def ask(model, prompt):
        return ("never called", 0)

    out = await iqn.nightly(["m1"], ask_fn=ask)
    assert out == []


@pytest.mark.asyncio
async def test_nightly_notify_on_regression(fresh_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l3")
    bench = _bench("tiny")
    monkeypatch.setattr(iqn.ib, "load_all", lambda *a, **kw: [bench])

    # Seed historical scores so regression fires on this run.
    base = 1_700_000_000.0
    for i in range(-6, -1):
        await _write_day("m1", 0.95, i, base_ts=base)
    await _write_day("m1", 0.60, -1, base_ts=base)

    # Today's ask returns empty (fail → score 0). We'll force the ts by
    # stubbing persist_run's default, but easier: rely on current real
    # time which will be > base + days → regression check looks at
    # recent_scores(days=7) from "now" which is real-time; our seeded
    # days are at base (epoch 2023) so far from now they won't show up.
    # Instead, seed using near-now offsets:
    import time as _t
    now = _t.time()

    # Rewrite seed using near-now.
    async def seed_day(score, day_offset):
        await iqn.persist_run([_report("m1", "s", score)],
                               ts=now + day_offset * iqn.SECONDS_PER_DAY)
    # Clear db rows for m1 so we don't have stale ones.
    from backend import db
    await db._conn().execute("DELETE FROM iq_runs WHERE model='m1'")
    await db._conn().commit()

    for i in range(-6, -1):
        await seed_day(0.95, i)
    await seed_day(0.60, -1)

    sent: list[dict] = []

    async def fake_notify(level, title, message="", source="", **kw):
        sent.append({"level": level, "title": title, "source": source})
        return type("N", (), {"id": "n", "level": level})()

    from backend import notifications as _n
    monkeypatch.setattr(_n, "notify", fake_notify)

    async def ask(model, prompt):
        return ("", 0)  # today also fails

    await iqn.nightly(["m1"], ask_fn=ask)

    action = [s for s in sent if s["level"] == "action"]
    assert action, f"expected an 'action' level notification, got {sent}"
    assert "[IIS-IQ]" in action[0]["title"]
    assert "m1" in action[0]["title"]


@pytest.mark.asyncio
async def test_nightly_silent_when_no_regression(fresh_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", "l3")
    bench = _bench("tiny")
    monkeypatch.setattr(iqn.ib, "load_all", lambda *a, **kw: [bench])

    sent: list = []

    async def fake_notify(level, title, message="", source="", **kw):
        sent.append(level)
        return type("N", (), {"id": "n", "level": level})()

    from backend import notifications as _n
    monkeypatch.setattr(_n, "notify", fake_notify)

    async def ask(model, prompt):
        return ("alpha yes", 30)  # always pass

    await iqn.nightly(["fresh-model"], ask_fn=ask)
    # No history → detect_regression returns (False, insufficient) → silent.
    assert sent == []
