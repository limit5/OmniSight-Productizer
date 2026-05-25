"""OP-1720 — contract test for the declarative cross-stage parity audit.

Pins, for ``scripts/deploy_line_parity.sh`` (reached via the single entrypoint
``scripts/deployment-audit.sh --cross-stage-parity``):

  * the exact set of parity DIMENSIONS,
  * the DIRECTION-AWARE verdicts (dev loosest, prod strictest),
  * exit-non-zero-on-wrong-direction-violation,
  * the reserved JSONL ``check_family`` field (only ``parity`` implemented),
  * and the two FIXTURES (aligned PASSES zero; drifted reproducing deep-audit
    findings #13 latest-tag + ADR-0042 GHCR is FLAGGED non-zero).

The audit is exercised via FIXTURES (not the live deploy line) so it never
forces remediation of real drift, per the ticket's REPORT-ONLY guardrail.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "scripts" / "deployment-audit.sh"
ENGINE = REPO_ROOT / "scripts" / "deploy_line_parity.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "deploy_parity"

# The nine dimensions the ticket pins (each = exact extractor + exact verdict).
EXPECTED_DIMENSIONS = {
    "registry+namespace",
    "image-identity",
    "pair-identity",
    "env-contract",
    "db-topology",
    "gate-wiring",
    "overlay-lock",
    "version/schema",
    "auth/secrets",
}


def _run(args, env_extra=None):
    env = None
    if env_extra:
        import os

        env = {**os.environ, **env_extra}
    return subprocess.run(
        [str(ENTRYPOINT), "--cross-stage-parity", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_fixture(name, extra=None):
    fdir = FIXTURES / name
    return _run(["--root", str(fdir), "--config", str(fdir / "parity.tsv"), *(extra or [])])


# ── plumbing ──────────────────────────────────────────────────────────────────
def test_scripts_exist_and_executable():
    for s in (ENTRYPOINT, ENGINE):
        assert s.is_file(), f"missing {s}"
        assert s.stat().st_mode & 0o111, f"{s} not executable"


def test_entrypoint_routes_to_parity_engine():
    """`deployment-audit.sh --cross-stage-parity` must shell out to the engine."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "--cross-stage-parity" in text
    assert "deploy_line_parity.sh" in text


# ── dimensions ──────────────────────────────────────────────────────────────--
def test_all_nine_dimensions_emitted_on_real_repo():
    res = _run([])
    missing = {d for d in EXPECTED_DIMENSIONS if d not in res.stdout}
    assert not missing, f"dimensions missing from output: {missing}\n{res.stdout}"


def test_real_repo_static_run_is_clean_finding_generator():
    """Reads real repo artefacts statically; the committed line is direction-OK
    (it surfaces advisory warn/info, not wrong-direction violations)."""
    res = _run([])
    assert res.returncode == 0, res.stdout
    assert "RESULT: PASS" in res.stdout
    # canary == prod backend-a by design, and testing-env is informational only.
    assert "testing-env=informational only" in res.stdout
    assert "stage=not-materialised" not in res.stdout  # all four real stages present


# ── fixtures + exit codes ───────────────────────────────────────────────────--
def test_aligned_fixture_passes_zero():
    res = _run_fixture("aligned")
    assert res.returncode == 0, res.stdout
    assert "RESULT: PASS" in res.stdout
    assert "✗ RED" not in res.stdout


def test_drifted_fixture_flagged_nonzero_reproduces_findings():
    res = _run_fixture("drifted")
    assert res.returncode == 1, res.stdout
    assert "RESULT: FAIL" in res.stdout
    # deep-audit #13 (latest-tag) on a prod stage:
    assert "image-identity" in res.stdout
    assert "MUTABLE tag (latest)" in res.stdout
    assert "#13" in res.stdout
    # ADR-0042 (GHCR decommission):
    assert "ghcr.io" in res.stdout
    assert "ADR-0042" in res.stdout


# ── direction-aware verdicts (NOT naive equality) ───────────────────────────--
def test_direction_dev_loosest_is_not_a_violation():
    """A mutable tag / AUTH_MODE=open / DEBUG=true is right-direction for dev."""
    res = _run_fixture("aligned")
    out = res.stdout
    assert "right-direction for dev (loosest)" in out
    assert "OMNISIGHT_AUTH_MODE=open (dev may be open)" in out
    assert "OMNISIGHT_DEBUG=true (dev may debug)" in out


def test_direction_prod_strictest_mutable_tag_is_a_violation():
    """The SAME mutable tag that is OK for dev is a violation at prod rank."""
    res = _run_fixture("drifted")
    assert "prod" in res.stdout
    red_lines = [ln for ln in res.stdout.splitlines() if "✗ RED" in ln and "image-identity" in ln]
    assert red_lines, res.stdout
    assert "prod" in red_lines[0]


def test_co_tenant_is_inferred_not_proven():
    """deep-audit #34 — staging PG co-tenant is labelled inferred, never proven."""
    res = _run([])
    assert "INFERRED (not proven)" in res.stdout
    assert "#34" in res.stdout


# ── reserved JSONL check_family ─────────────────────────────────────────────--
def test_jsonl_emits_reserved_check_family_parity_only(tmp_path):
    log = tmp_path / "parity.jsonl"
    res = _run(
        ["--root", str(FIXTURES / "aligned"), "--config", str(FIXTURES / "aligned" / "parity.tsv")],
        env_extra={"DEPLOY_PARITY_JSONL_LOG": str(log)},
    )
    assert res.returncode == 0, res.stdout
    record = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["event"] == "deploy_line_parity"
    assert record["result"] == "PASS"
    families = {row["check_family"] for row in record["rows"]}
    assert families == {"parity"}, f"phase-1 implements only parity; got {families}"
    # candidate_compat is reserved for phase 2 — must NOT be emitted yet.
    assert "candidate_compat" not in families


def test_optional_live_mode_is_best_effort_never_fatal():
    """--live annotates from /readyz + /api/version + systemctl; unreachable
    targets are annotated live=unknown and never flip the exit code."""
    res = _run_fixture("aligned", extra=["--live"])
    assert res.returncode == 0, res.stdout
    assert "live=" in res.stdout  # at least one best-effort live annotation row


# ── REPORT-ONLY guardrail ───────────────────────────────────────────────────--
def test_engine_is_report_only_no_remediation():
    """The engine must never mutate compose/deploy state (reports only)."""
    text = ENGINE.read_text(encoding="utf-8")
    for forbidden in ("docker compose up", "docker-compose up", "git commit", "git push", "systemctl start", "systemctl enable"):
        assert forbidden not in text, f"engine appears to mutate state: {forbidden!r}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
