"""Phase 62 S2 — skills extractor (post-U4-I redirect).

Legacy assertions pinning the ``configs/skills/_pending/*.md`` file
shape have been retargeted to pin the U4-I substrate producer submit
call. The scrub thresholds + L1 gate assertions are unchanged. Files
are never written by the extractor after this ticket — the frozen
archive dir (``configs/skills/_pending``) stays untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend import skills_extractor as ex


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Lightweight fakes for WorkflowRun + StepRecord
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _FakeStep:
    idempotency_key: str
    started_at: float = 0.0
    completed_at: float = 0.0
    output: dict | None = None
    error: str | None = None


@dataclass
class _FakeRun:
    id: str = "run-deadbeef"
    kind: str = "build/firmware"
    status: str = "completed"
    metadata: dict = None
    started_at: float = 0.0
    completed_at: float = 0.0

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class _SubmitResultShim:
    def __init__(self, *, version_id: str, created: bool) -> None:
        self.version_id = version_id
        self.created = created


@pytest.fixture()
def recorder(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def _stub(conn, **kwargs):
        calls.append({"conn": conn, **kwargs})
        return _SubmitResultShim(version_id="v-x-1", created=True)

    monkeypatch.setattr(ex, "submit_quarantined_version", _stub, raising=True)
    return calls


class _FakeConn:
    async def execute(self, sql: str, *args: Any) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any):
        return None


def _mk_steps(n_success: int, n_error: int) -> list[_FakeStep]:
    out: list[_FakeStep] = []
    t = 1000.0
    for i in range(n_error):
        out.append(_FakeStep(
            idempotency_key=f"step-err-{i}",
            started_at=t, completed_at=t + 5,
            error=f"compile failed: stage {i}",
        ))
        t += 6
    for i in range(n_success):
        out.append(_FakeStep(
            idempotency_key=f"step-ok-{i}",
            started_at=t, completed_at=t + 3,
            output={"summary": f"step {i} ok"},
        ))
        t += 4
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  should_extract — trigger gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_should_not_extract_for_failed_run():
    run = _FakeRun(status="failed")
    assert not ex.should_extract(run, _mk_steps(10, 0))


def test_should_extract_when_step_count_threshold_hit():
    run = _FakeRun()
    assert ex.should_extract(run, _mk_steps(5, 0))
    assert not ex.should_extract(run, _mk_steps(4, 0))


def test_should_extract_when_retry_threshold_hit():
    run = _FakeRun()
    assert ex.should_extract(run, _mk_steps(1, 3))
    assert not ex.should_extract(run, _mk_steps(1, 2))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  extract — substrate submit + scrub integration (U4-I redirect)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_extract_skips_when_below_threshold(recorder, tmp_path):
    run = _FakeRun()
    res = await ex.extract(
        run, _mk_steps(2, 0), pending_dir=tmp_path, conn=_FakeConn(),
    )
    assert not res.written
    assert res.path is None
    assert "below threshold" in res.skipped_reason
    # Threshold gate happens BEFORE the substrate submit.
    assert recorder == []


@pytest.mark.asyncio
async def test_extract_submits_quarantined_record_with_kind_skill(
    recorder, tmp_path,
):
    run = _FakeRun(
        kind="build/imx335-driver",
        metadata={"platform": "rockchip-rk3588", "tenant_id": "t-acme"},
    )
    res = await ex.extract(
        run, _mk_steps(5, 2), pending_dir=tmp_path, conn=_FakeConn(),
    )
    assert res.written
    # Legacy assertion retargeted: NO file is written; path is None.
    assert res.path is None
    # No stray files land in the pending dir.
    assert not list(tmp_path.glob("*.md"))
    assert res.version_id == "v-x-1"

    assert len(recorder) == 1
    call = recorder[0]
    assert call["kind"] == "skill"
    assert call["audience"] == "tenant"
    assert call["tenant_id"] == "t-acme"
    assert call["created_by"] == "skills_extractor"
    payload = call["payload"]
    # The parsed record carries scope + resolution steps + failure modes.
    assert payload["scope"].startswith("Applies to a workflow_run of kind")
    assert len(payload["procedure_steps"]) >= 1
    assert len(payload["known_failures"]) == 2


@pytest.mark.asyncio
async def test_extract_scrubs_secrets_before_record_build(
    recorder, tmp_path,
):
    """Scrub still runs BEFORE record building — a secret in a step
    error must be redacted before it can reach the quarantined row."""
    run = _FakeRun()
    steps = _mk_steps(0, 0)
    steps.append(_FakeStep(
        idempotency_key="leaky",
        started_at=0, completed_at=1,
        error="GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 expired",
    ))
    steps += _mk_steps(5, 0)
    res = await ex.extract(
        run, steps, pending_dir=tmp_path, conn=_FakeConn(),
    )
    assert res.written
    assert res.hits["github_pat"] >= 1
    # The scrubbed markdown feeds the parser — the raw secret must not
    # appear in the submitted record's leaves.
    payload = recorder[0]["payload"]
    blob = "\n".join(
        payload["scope"]
        + "\n".join(payload["preconditions"])
        + "\n".join(payload["procedure_steps"])
        + "\n".join(payload["known_failures"])
    )
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in blob


@pytest.mark.asyncio
async def test_extract_refuses_when_too_many_hits(
    recorder, tmp_path, monkeypatch,
):
    """Force the safety threshold low and verify refusal — same
    scrub-gate semantics, no substrate write."""
    monkeypatch.setattr(ex, "MIN_STEPS", 1)
    from backend import skills_scrubber
    monkeypatch.setattr(skills_scrubber, "SAFETY_THRESHOLD", 2, raising=False)

    run = _FakeRun()
    leaky = _FakeStep(
        idempotency_key="leaky",
        error="\n".join(f"u{i}@x.com k=ghp_{'a'*36}" for i in range(10)),
    )
    res = await ex.extract(
        run, [leaky] * 5, pending_dir=tmp_path, conn=_FakeConn(),
    )
    assert not res.written
    assert "too many secret hits" in res.skipped_reason
    assert recorder == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled — opt-in gate (unchanged)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("level,expected", [
    ("off", False),
    ("", False),
    (None, False),
    ("l1", True),
    ("L1", True),
    ("l1+l3", True),
    ("all", True),
    ("l3", False),  # no L1 → extractor stays off
])
def test_is_enabled_honours_self_improve_level(monkeypatch, level, expected):
    if level is None:
        monkeypatch.delenv("OMNISIGHT_SELF_IMPROVE_LEVEL", raising=False)
    else:
        monkeypatch.setenv("OMNISIGHT_SELF_IMPROVE_LEVEL", level)
    assert ex.is_enabled() is expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  propose_promotion — retired since U4-I
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_propose_returns_none_when_not_written(tmp_path):
    res = ex.SkillExtractionResult(
        written=False, path=None, hits=__import__("collections").Counter(),
    )
    assert ex.propose_promotion(res, _FakeRun()) is None


@pytest.mark.asyncio
async def test_propose_is_retired_and_never_files_decision(
    recorder, tmp_path,
):
    """U4-I: the ``skill/promote`` card is retired — no decision is
    filed regardless of whether the extract succeeded."""
    from backend import decision_engine as de
    de._reset_for_tests()

    run = _FakeRun(kind="build/test", metadata={"tenant_id": "t-acme"})
    res = await ex.extract(
        run, _mk_steps(6, 0), pending_dir=tmp_path, conn=_FakeConn(),
    )
    assert res.written
    # propose_promotion returns None regardless of extract success.
    assert ex.propose_promotion(res, run) is None
    # No skill/promote card lands in the decision engine.
    assert not [d for d in de.list_pending() if d.kind == "skill/promote"]
    assert not [
        d for d in de.list_history(limit=10) if d.kind == "skill/promote"
    ]
