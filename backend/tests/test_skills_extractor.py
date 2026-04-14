"""Phase 62 S2 — skills extractor."""

from __future__ import annotations

from dataclasses import dataclass

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
#  extract — file output + scrub integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_extract_skips_when_below_threshold(tmp_path):
    run = _FakeRun()
    res = ex.extract(run, _mk_steps(2, 0), pending_dir=tmp_path)
    assert not res.written
    assert res.path is None
    assert "below threshold" in res.skipped_reason


def test_extract_writes_markdown_with_frontmatter(tmp_path):
    run = _FakeRun(kind="build/imx335-driver",
                   metadata={"platform": "rockchip-rk3588"})
    res = ex.extract(run, _mk_steps(5, 2), pending_dir=tmp_path)
    assert res.written
    assert res.path is not None
    assert res.path.parent == tmp_path
    txt = res.path.read_text()
    assert txt.startswith("---\n")
    assert "trigger_kinds:" in txt
    assert "rockchip-rk3588" in txt
    assert "step_count: 7" in txt
    assert "retry_count: 2" in txt
    assert "## Failure modes encountered" in txt
    assert "## Resolution path" in txt


def test_extract_scrubs_secrets_in_step_outputs(tmp_path):
    """Step error/output that contains a secret must come out scrubbed."""
    run = _FakeRun()
    steps = _mk_steps(0, 0)
    steps.append(_FakeStep(
        idempotency_key="leaky",
        started_at=0, completed_at=1,
        error="GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 expired",
    ))
    # bring step count above threshold
    steps += _mk_steps(5, 0)
    res = ex.extract(run, steps, pending_dir=tmp_path)
    assert res.written
    txt = res.path.read_text()
    assert "[GITHUB_PAT]" in txt
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in txt
    assert res.hits["github_pat"] >= 1


def test_extract_refuses_when_too_many_hits(tmp_path, monkeypatch):
    """Force the safety threshold low and verify refusal."""
    monkeypatch.setattr(ex, "MIN_STEPS", 1)
    from backend import skills_scrubber
    monkeypatch.setattr(skills_scrubber, "SAFETY_THRESHOLD", 2, raising=False)

    run = _FakeRun()
    leaky = _FakeStep(
        idempotency_key="leaky",
        error="\n".join(f"u{i}@x.com k=ghp_{'a'*36}" for i in range(10)),
    )
    res = ex.extract(run, [leaky] * 5, pending_dir=tmp_path)
    assert not res.written
    assert "too many secret hits" in res.skipped_reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled — opt-in gate
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
#  propose_promotion — Decision Engine wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_propose_returns_none_when_not_written(tmp_path):
    res = ex.SkillExtractionResult(
        written=False, path=None, hits=__import__("collections").Counter(),
    )
    assert ex.propose_promotion(res, _FakeRun()) is None


def test_propose_files_decision_with_correct_kind(tmp_path):
    from backend import decision_engine as de
    de._reset_for_tests()

    run = _FakeRun(kind="build/test")
    res = ex.extract(run, _mk_steps(6, 0), pending_dir=tmp_path)
    assert res.written

    dec_id = ex.propose_promotion(res, run)
    assert dec_id is not None

    dec = de.get(dec_id)
    assert dec.kind == "skill/promote"
    assert dec.severity == de.DecisionSeverity.routine
    # Default-safe option is `discard`, not `promote`.
    assert dec.default_option_id == "discard"
    option_ids = {o["id"] for o in dec.options}
    assert option_ids == {"promote", "discard"}
