"""OP-876 — Re-runnable smoke test for the merger proactive-trigger spike.

Exercises the static-analysis layer of
``scripts/spike_merger_proactive_trigger.py`` so that:

  * the decision-rule mappings stay in sync with the documented
    Q1/Q2/Q3 verdicts in
    ``docs/research/merger-proactive-trigger-spike-2026-05.md``;
  * Q2 static wiring (auto_rebase / bridge / webhook) does not silently
    drift — if a future refactor moves OP-733 into the webhook handler
    or out of the SSH bridge, this test will fail and force the
    decision document to be re-reviewed;
  * Q3 stays a tripwire: if the project.config in /tmp/gerrit-meta is
    patched to header-auth (the fix), the test flips from no_go → go
    automatically, which is the signal that the L-OP-713 hazard has
    been cleared.

Wired into CI later if Phase 2 ships (per ticket DoD). Until then
``pytest tests/integration/test_merger_spike.py`` is the manual
re-run.
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_PATH = REPO_ROOT / "scripts" / "spike_merger_proactive_trigger.py"


@pytest.fixture(scope="module")
def spike():
    """Import the spike script as a module so we can call its
    decision-rule helpers directly instead of shelling out."""
    spec = importlib.util.spec_from_file_location(
        "spike_merger_proactive_trigger", SPIKE_PATH,
    )
    assert spec and spec.loader, f"could not load spec for {SPIKE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["spike_merger_proactive_trigger"] = module
    spec.loader.exec_module(module)
    yield module


# ─── Q1 decision-rule contract ────────────────────────────────────────


@pytest.mark.parametrize(
    "deltas, expected_verdict",
    [
        # All <1s → real-time trigger viable, no retry.
        ([0.1, 0.2, 0.3, 0.4, 0.5], "go"),
        # Mixed <1s + 1-10s → backoff needed.
        ([0.1, 0.5, 1.5, 3.0, 7.0], "go_with_backoff"),
        # Any >10s → no_go, fall back to ref-updated.
        ([0.5, 1.0, 12.0], "no_go"),
        # Timeout → no_go.
        ([0.1, 0.2, 35.0], "no_go"),
        # Empty → deferred to operator.
        ([], "deferred"),
    ],
    ids=["all-fast", "mixed-backoff-band", "over-10s", "timeout", "empty"],
)
def test_q1_decision_rule_matches_documented_bands(
    spike, deltas, expected_verdict
):
    """The Q1 decision rule MUST follow the bands in the spike doc:

    <1s → go ; 1-10s → go_with_backoff ; >10s OR timeout → no_go ;
    no samples → deferred.

    If this test breaks, either the rule changed (update the doc) or
    the sim is wrong (fix the code).
    """
    result = spike.q1_simulate_samples(deltas)
    assert result.verdict == expected_verdict, (
        f"deltas={deltas} → {result.verdict} (expected {expected_verdict})"
        f" — rationale: {result.rationale}"
    )


def test_q1_histogram_buckets_align_with_thresholds(spike):
    """Sanity-check that the histogram counts match the band counts."""
    result = spike.q1_simulate_samples([0.2, 0.9, 1.1, 5.0, 11.0])
    hist = result.histogram()
    assert hist["<1s"] == 2, hist
    assert hist["1-10s"] == 2, hist
    assert hist[">10s"] == 1, hist


# ─── Q2 static-analysis contract ──────────────────────────────────────


def test_q2_static_finds_op733_in_ssh_bridge(spike):
    """OP-733 lives in ``backend/agents/auto_rebase.py`` and is wired
    through ``gerrit_jira_bridge._schedule_auto_rebase_sweep`` — the
    webhook handler must NOT call it. If any of these flip, the spike
    rationale becomes stale and the verdict must be revisited.
    """
    result = spike.q2_static_check()
    by_name = {c.name: c for c in result.checkpoints}
    assert by_name["auto_rebase_module_present"].passed is True
    assert by_name["bridge_wires_sweep_on_change_merged"].passed is True
    assert by_name["webhook_handler_does_not_call_op733"].passed is True
    # Live checkpoints must remain deferred — they are operator work.
    assert by_name["live_trigger_fires_in_staging"].passed is None
    assert by_name["live_sweep_pushes_ps2"].passed is None
    assert result.verdict == "deferred", result.rationale
    assert "SSH stream-events bridge" in result.static_finding


# ─── Q3 static-analysis contract ──────────────────────────────────────


def test_q3_flags_secret_block_as_broken(spike, tmp_path: Path):
    """A project.config with ``secret = ...`` on the ai-reviewer-webhook
    block MUST be flagged as ``no_go`` per L-OP-713 (webhooks plugin
    v3.13.5 silently drops the key).
    """
    cfg = tmp_path / "project.config"
    cfg.write_text(textwrap.dedent("""
        [remote "ai-reviewer-webhook"]
            url = https://example.test/api/v1/webhooks/gerrit
            event = patchset-created
            secret = aaaaa
            sslVerify = true
    """).strip())
    result = spike.q3_inspect_config(cfg)
    assert result.has_secret_line is True
    assert result.has_header_auth is False
    assert result.verdict == "no_go", result.rationale
    assert "L-OP-713" in result.rationale


def test_q3_accepts_header_auth_fix(spike, tmp_path: Path):
    """Once project.config is patched to use
    ``header = Authorization: Bearer ...`` (the L-OP-708 pattern), the
    verdict MUST flip to ``go`` so the rest of the pipeline knows the
    hazard is cleared.
    """
    cfg = tmp_path / "project.config"
    cfg.write_text(textwrap.dedent("""
        [remote "ai-reviewer-webhook"]
            url = https://example.test/api/v1/webhooks/gerrit
            event = patchset-created
            header = Authorization: Bearer omni_synthetic_key
            sslVerify = true
    """).strip())
    result = spike.q3_inspect_config(cfg)
    assert result.has_header_auth is True
    assert result.verdict == "go", result.rationale


def test_q3_deferred_when_block_absent(spike, tmp_path: Path):
    """No ``[remote "ai-reviewer-webhook"]`` block → cannot conclude;
    must be ``deferred``, not ``go`` or ``no_go``.
    """
    cfg = tmp_path / "project.config"
    cfg.write_text('[remote "merge-conflict-webhook"]\n  url = x\n')
    result = spike.q3_inspect_config(cfg)
    assert result.verdict == "deferred", result.rationale


def test_q3_live_classifier_flags_missing_signature(spike):
    """When operator captures the live request header dump and Gerrit
    sent no ``X-Gerrit-Signature``, the live classifier MUST return
    ``no_go`` (matches the static config's prediction)."""
    result = spike.q3_classify_live_headers(
        observed=["X-Gerrit-Event: patchset-created", "User-Agent: gerrit"],
        backend_status=401,
    )
    assert result.verdict == "no_go", result.rationale


# ─── End-to-end smoke ─────────────────────────────────────────────────


def test_build_static_report_emits_markdown(spike):
    """Wire the full static report end-to-end — confirms render does
    not crash on any verdict combination and the markdown is present.
    """
    report = spike.build_static_report()
    md = spike.render_markdown(report)
    # Markdown skeleton sanity.
    for section in (
        "# OP-876 Merger Proactive-Trigger Spike",
        "## Q1 — `mergeable` race",
        "## Q2 — OP-733 end-to-end",
        "## Q3 — `ai-reviewer-webhook` auth",
    ):
        assert section in md, f"missing section: {section}"
    # Every verdict must be one of the documented enums.
    for verdict in (report.q1.verdict, report.q2.verdict, report.q3.verdict):
        assert verdict in {"go", "go_with_backoff", "no_go", "deferred"}
