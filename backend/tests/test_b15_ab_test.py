"""B15 #350 row 263 — tests for the eager vs lazy A/B test harness.

Covers three invariants of ``scripts/b15_ab_test.py`` that future
maintainers rely on:

  1. **Offline run is deterministic** — same tasks, same report (no
     live LLM, no randomness). Guards against accidental introduction
     of wall-clock timestamps or random sampling into the prompt
     comparison.
  2. **Schema is stable** — the per-task row and summary dict keys
     that dashboards / HANDOFF prose already quote must keep working.
  3. **Markdown writer honours the schema** — swapping rows to canned
     values produces expected summary numbers (no off-by-one in the
     delta math).

No live LLM is invoked. Live mode is exercised by an operator-run
smoke; CI only runs offline.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "b15_ab_test.py"
)


@pytest.fixture(scope="module")
def harness():
    """Import the script as a module without executing ``main()``."""
    spec = importlib.util.spec_from_file_location("b15_ab_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_estimate_tokens_anthropic_rule(harness):
    assert harness.estimate_tokens("") == 0
    assert harness.estimate_tokens("abcd") == 1  # 4 chars → 1 token
    assert harness.estimate_tokens("a" * 400) == 100


def test_keyword_coverage_handles_empty_inputs(harness):
    assert harness._keyword_coverage("anything", []) == 0.0
    assert harness._keyword_coverage("", ["foo"]) == 0.0
    assert harness._keyword_coverage("foo bar", ["FOO"]) == 1.0
    assert harness._keyword_coverage(
        "mentions X and Y", ["X", "Y", "Z"],
    ) == pytest.approx(2 / 3)


def test_offline_run_produces_stable_schema(harness):
    """A single offline row must carry the keys the markdown writer
    and downstream dashboards consume. If this test fails after a
    schema change, update the renderer + HANDOFF prose too."""
    task = harness.TASKS[0]
    row = harness.run_offline(task)
    # Top-level schema.
    assert row["task_id"] == task["id"]
    assert row["mode"] == "offline"
    for key in ("eager", "lazy", "delta"):
        assert key in row
    # eager block.
    assert set(row["eager"]) >= {
        "prompt_chars", "tokens_est", "keyword_coverage",
        "completed", "build_ms",
    }
    # lazy block — Phase-1 + Phase-2 must both be reported.
    assert set(row["lazy"]) >= {
        "phase1_chars", "phase1_tokens_est", "phase2_chars",
        "phase2_matches", "phase2_matched", "combined_chars",
        "tokens_est", "keyword_coverage", "completed",
        "phase1_build_ms", "phase2_build_ms",
    }
    # delta block — both headline KPIs present.
    assert set(row["delta"]) >= {
        "tokens_saved", "tokens_saved_pct", "coverage_delta",
    }
    # Sanity: eager token count is positive & lazy phase1 is positive.
    assert row["eager"]["tokens_est"] > 0
    assert row["lazy"]["phase1_tokens_est"] > 0


def test_offline_run_is_deterministic(harness):
    """Running twice with identical input must produce identical
    prompt-side numbers. Guards against any accidental non-determinism
    (hash seeds, time-based IDs, etc.) creeping into prompt assembly."""
    task = harness.TASKS[0]
    a = harness.run_offline(task)
    b = harness.run_offline(task)
    # Build timings will vary — strip before comparing.
    for row in (a, b):
        row["eager"].pop("build_ms", None)
        row["lazy"].pop("phase1_build_ms", None)
        row["lazy"].pop("phase2_build_ms", None)
    assert a == b


def test_summary_computes_token_deltas_correctly(harness):
    """Fabricate two rows with hand-calculable numbers so the summary
    math is provable. Catches any sign-flip or off-by-one in the
    delta / savings-percent formula."""
    rows = [
        {
            "task_id": "synthetic-1",
            "eager": {"tokens_est": 1000, "keyword_coverage": 1.0,
                      "completed": True},
            "lazy": {"tokens_est": 600, "phase1_tokens_est": 400,
                     "keyword_coverage": 1.0, "completed": True},
            "delta": {"tokens_saved": 400, "tokens_saved_pct": 40.0,
                      "coverage_delta": 0.0},
        },
        {
            "task_id": "synthetic-2",
            "eager": {"tokens_est": 2000, "keyword_coverage": 1.0,
                      "completed": True},
            "lazy": {"tokens_est": 1200, "phase1_tokens_est": 800,
                     "keyword_coverage": 0.5, "completed": True},
            "delta": {"tokens_saved": 800, "tokens_saved_pct": 40.0,
                      "coverage_delta": -0.5},
        },
    ]
    s = harness._summary(rows)
    assert s["tasks"] == 2
    assert s["eager"]["avg_tokens"] == pytest.approx(1500.0)
    assert s["lazy"]["avg_tokens"] == pytest.approx(900.0)
    assert s["lazy"]["avg_phase1_only_tokens"] == pytest.approx(600.0)
    # (1500 − 900) / 1500 = 40%
    assert s["delta"]["avg_tokens_saved_pct"] == pytest.approx(40.0)
    # Phase-1-only delta: (1500 − 600) / 1500 = 60%
    assert s["delta"]["avg_phase1_only_tokens_saved_pct"] == pytest.approx(60.0)
    # Completion unchanged; coverage down 0.25 on average.
    assert s["delta"]["completion_rate_delta"] == pytest.approx(0.0)
    assert s["delta"]["coverage_delta"] == pytest.approx(-0.25)


def test_render_markdown_includes_key_sections(harness):
    """The written report must carry the three sections the HANDOFF
    narrative references: Summary, Per-task results, Key findings,
    Methodology."""
    task = harness.TASKS[0]
    rows = [harness.run_offline(task)]
    summary = harness._summary(rows)
    md = harness.render_markdown(rows, mode="offline", summary=summary)
    for section in (
        "## Summary",
        "### Token deltas vs eager",
        "## Per-task results",
        "## Key findings",
        "## Methodology",
    ):
        assert section in md, f"missing section {section!r}"
    # Task ID and both mode headers must appear.
    assert task["id"] in md
    assert "Eager" in md and "Lazy" in md


def test_cli_writes_report_and_json(tmp_path, harness):
    """End-to-end: run the CLI with --mode offline → both outputs
    exist, JSON parses, and the markdown starts with the fixed title."""
    md = tmp_path / "report.md"
    js = tmp_path / "rows.json"
    rc = harness.main([
        "--mode", "offline",
        "--output", str(md),
        "--json", str(js),
    ])
    assert rc == 0
    assert md.is_file(), "markdown report not written"
    assert js.is_file(), "json sidecar not written"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# B15 #350 — Skill Lazy Loading A/B Test Report")
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["mode"] == "offline"
    assert payload["rows"]
    assert payload["summary"]["tasks"] == len(harness.TASKS)


def test_load_tasks_accepts_override_file(tmp_path, harness):
    """The --tasks override is the knob operators use to dog-food lazy
    mode on their own workload without patching the script."""
    custom = [{
        "id": "custom-task",
        "agent_type": "firmware",
        "sub_type": "bsp",
        "domain_context": "kernel driver I2C",
        "user_prompt": "add a driver",
        "expected_keywords": ["driver", "I2C"],
    }]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(custom), encoding="utf-8")
    loaded = harness._load_tasks(str(path))
    assert loaded == custom


def test_load_tasks_rejects_malformed_input(tmp_path, harness):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"id": "missing-fields"}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        harness._load_tasks(str(path))
