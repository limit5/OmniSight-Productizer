"""OP-867 (Sprint X / X3) — tests for the C1 recall helpfulness audit.

Per the ticket Test plan: 3 cases — sample selection, scoring
aggregation, decision threshold. Each test maps 1:1 to one AC of
OP-867 so retrospective evidence collection is mechanical.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_memory_recall_helpfulness.py"
_SPEC = importlib.util.spec_from_file_location("audit_memory_recall_helpfulness", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
audit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = audit
_SPEC.loader.exec_module(audit)


# ── Fixtures ──────────────────────────────────────────────────────────


def _row(
    *, op: str = "read", tier: str = "S", key: str = "/memories/x.md",
    ts: str = "2026-05-10T12:00:00+00:00", ticket: str = "OP-100",
    type_field: str = "memory_tool",
) -> dict:
    return {
        "type": type_field,
        "tool": "memory",
        "op": op,
        "key": key,
        "timestamp": ts,
        "ticket_key": ticket,
        "tier": tier,
    }


@pytest.fixture
def progress_file(tmp_path: Path) -> Path:
    path = tmp_path / "progress.txt"
    path.write_text("", encoding="utf-8")
    return path


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


# ── Case 1 — sample selection (AC #1 + #2) ────────────────────────────


def test_sample_selection_filters_window_op_and_tier_and_seeded_draws(
    progress_file: Path,
) -> None:
    """AC #1+#2: load filters to auto-recall in window; sample is seeded.

    Constructed corpus:
      * 40 in-window tier:S/M read rows → eligible
      * 5 in-window write rows → dropped (op filter)
      * 5 in-window tier:X read rows → dropped (tier filter)
      * 5 OUT-of-window read rows → dropped (timestamp filter)
      * 5 wrong-type rows → dropped (type filter)
    Verify:
      * loader returns exactly 40
      * sample(seed=0) is deterministic and matches sample(seed=0) again
      * sample(seed=1) differs (seeding actually changes the draw)
    """
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    in_window_ts = (now - timedelta(days=3)).isoformat()
    out_of_window_ts = (now - timedelta(days=10)).isoformat()

    rows: list[dict] = []
    for i in range(40):
        rows.append(_row(
            op="read",
            tier="S" if i % 2 == 0 else "M",
            key=f"/memories/eligible-{i}.md",
            ts=in_window_ts,
        ))
    for i in range(5):
        rows.append(_row(op="write", key=f"/memories/wr-{i}.md", ts=in_window_ts))
    for i in range(5):
        rows.append(_row(op="read", tier="X", key=f"/memories/x-{i}.md", ts=in_window_ts))
    for i in range(5):
        rows.append(_row(op="read", key=f"/memories/old-{i}.md", ts=out_of_window_ts))
    for i in range(5):
        rows.append(_row(
            op="read", key=f"/memories/wrong-{i}.md", ts=in_window_ts,
            type_field="tom_scratchpad",
        ))
    _write_rows(progress_file, rows)

    events = audit.load_audit_events(
        [progress_file],
        window_start=now - timedelta(days=7),
        window_end=now,
    )
    assert len(events) == 40, [e.key for e in events][:5]
    assert {e.tier for e in events} == {"S", "M"}
    assert all(e.op == "read" for e in events)

    sample_a = audit.sample_events(events, size=30, seed=0)
    sample_b = audit.sample_events(events, size=30, seed=0)
    sample_c = audit.sample_events(events, size=30, seed=1)
    assert [e.key for e in sample_a] == [e.key for e in sample_b]
    assert [e.key for e in sample_a] != [e.key for e in sample_c]
    assert len({e.key for e in sample_a}) == 30  # sample without replacement


def test_sample_raises_when_population_below_sample_size(progress_file: Path) -> None:
    """AC error catalog: InsufficientSamplesForAudit when <30 events.

    The error name + code are part of the contract (the runner reads
    them to decide whether to defer the ticket).
    """
    rows = [
        _row(op="read", tier="S", key=f"/memories/e-{i}.md")
        for i in range(10)
    ]
    _write_rows(progress_file, rows)
    events = audit.load_audit_events(
        [progress_file],
        window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert len(events) == 10
    with pytest.raises(audit.InsufficientSamplesForAudit) as exc_info:
        audit.sample_events(events, size=30, seed=0)
    assert exc_info.value.code == "insufficient_samples_for_audit"
    assert "10" in str(exc_info.value)


# ── Case 2 — scoring aggregation (AC #2 + DoD) ────────────────────────


def test_scoring_aggregation_emits_mean_stdev_and_per_tier_split() -> None:
    """AC #2: collect_scores_* produces ints 1-5; aggregate() computes
    mean/stdev plus a per-tier breakdown that lets the operator see
    whether tier:M is dragging the average.

    Constructed: 6 events, 3 tier:S scored 5/5/5 (mean 5.0), 3 tier:M
    scored 1/1/2 (mean 1.33). Overall mean = (5+5+5+1+1+2)/6 = 3.167.
    """
    events = [
        audit.AuditEvent(key=f"/memories/s-{i}.md", op="read", tier="S",
                         timestamp="2026-05-10T12:00:00+00:00",
                         ticket_key="OP-1", raw={})
        for i in range(3)
    ] + [
        audit.AuditEvent(key=f"/memories/m-{i}.md", op="read", tier="M",
                         timestamp="2026-05-10T12:00:00+00:00",
                         ticket_key="OP-2", raw={})
        for i in range(3)
    ]
    scores = [5, 5, 5, 1, 1, 2]
    result = audit.aggregate(
        events, scores,
        window_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        total_in_window=42,
        seed=7,
    )
    assert result.sampled_event_count == 6
    assert result.total_events_in_window == 42
    assert result.seed == 7
    assert result.mean_score == pytest.approx(3.167, abs=1e-3)
    assert result.stdev_score is not None
    assert result.per_tier_mean == {"S": 5.0, "M": pytest.approx(1.333, abs=1e-3)}
    # The score rows preserve the operator-visible identity so the
    # decision doc can cite which keys drove the verdict.
    assert {row["key"] for row in result.scores} == {e.key for e in events}


def test_collect_scores_from_file_rejects_partial_and_validates_range(
    tmp_path: Path,
) -> None:
    """AC #2: scores file must cover every sampled key; values 1-5 only."""
    events = [
        audit.AuditEvent(key="/memories/a.md", op="read", tier="S",
                         timestamp="t", ticket_key=None, raw={}),
        audit.AuditEvent(key="/memories/b.md", op="read", tier="M",
                         timestamp="t", ticket_key=None, raw={}),
    ]
    # Partial file → ValueError.
    partial = tmp_path / "scores-partial.json"
    partial.write_text(json.dumps({"/memories/a.md": 4}))
    with pytest.raises(ValueError, match="missing"):
        audit.collect_scores_from_file(events, partial)

    # Out-of-range score → ValueError.
    bad = tmp_path / "scores-bad.json"
    bad.write_text(json.dumps({"/memories/a.md": 4, "/memories/b.md": 6}))
    with pytest.raises(ValueError, match=r"\[1,5\]"):
        audit.collect_scores_from_file(events, bad)

    # Happy path.
    good = tmp_path / "scores-good.json"
    good.write_text(json.dumps({"/memories/a.md": 4, "/memories/b.md": 3}))
    assert audit.collect_scores_from_file(events, good) == [4, 3]


# ── Case 3 — decision threshold (AC #3) ──────────────────────────────


def test_decision_threshold_below_3_recommends_tighten_above_3_keeps() -> None:
    """AC #3: mean <3 → TIGHTEN_TIER_M; mean >=3 → KEEP_AS_IS.

    Boundary specifically tested: 2.999 must tighten, 3.0 must keep.
    """
    below = audit.decide_verdict(2.999)
    assert below[0] == audit.DECISION_TIGHTEN
    assert "tighten" in below[1].lower()

    boundary = audit.decide_verdict(3.0)
    assert boundary[0] == audit.DECISION_KEEP

    above = audit.decide_verdict(4.2)
    assert above[0] == audit.DECISION_KEEP
    assert "net-positive" in above[1].lower()


def test_run_audit_end_to_end_with_scores_file(
    progress_file: Path, tmp_path: Path,
) -> None:
    """AC #1–#3 integration: load → sample → score → decide, all wired."""
    now = datetime(2026, 5, 11, tzinfo=timezone.utc)
    in_window_ts = (now - timedelta(days=2)).isoformat()
    rows = [
        _row(op="read", tier="M", key=f"/memories/e-{i}.md", ts=in_window_ts)
        for i in range(30)
    ]
    _write_rows(progress_file, rows)

    # Score every sampled key 2 → forces a TIGHTEN verdict.
    scores = {f"/memories/e-{i}.md": 2 for i in range(30)}
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(json.dumps(scores))

    result = audit.run_audit(
        progress_paths=[progress_file],
        now=now,
        window_days=7,
        sample_size=30,
        seed=0,
        scores_path=scores_path,
        interactive=False,
    )
    assert result.sampled_event_count == 30
    assert result.mean_score == 2.0
    assert result.decision == audit.DECISION_TIGHTEN
    assert result.per_tier_mean == {"M": 2.0}
