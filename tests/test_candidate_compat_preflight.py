"""OP-1721 — contract test for the candidate forward-compat promotion-preflight.

Phase 2 of the deploy-line parity tooling (phase 1 = OP-1720 declarative parity).
Pins, for ``scripts/deploy_line_parity.sh --candidate-bundle …`` (reached via the
single entrypoint ``scripts/deployment-audit.sh --cross-stage-parity``):

  * the four STATIC candidate-compat checks (bundle completeness, env-contract
    delta, migration compatibility, FE bundle-shape),
  * the ``check_family=candidate_compat`` rows (table + JSONL) that OP-1720
    reserved,
  * exit-non-zero on any forward-compat break,
  * the two FIXTURES — a candidate breaking all four checks is FLAGGED non-zero;
    a clean candidate PASSES zero,
  * the static-only guardrail (NO live migration execution against any DB; the
    migration check is a descendant-reachability walk reusing
    ``scripts/check_migration_compat.py``),
  * and the integration path: a real ``bundle.json`` + the existing
    migration-compat checker read statically.

Exercised via FIXTURES (not a live promote), per the ticket's STATIC-ONLY +
REPORT-ONLY guardrails.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "scripts" / "deployment-audit.sh"
ENGINE = REPO_ROOT / "scripts" / "deploy_line_parity.sh"
MIGRATION_COMPAT = REPO_ROOT / "scripts" / "check_migration_compat.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "candidate_compat"

# The four candidate-compat dimensions the ticket pins.
EXPECTED_DIMENSIONS = {
    "bundle-completeness",
    "env-contract-delta",
    "migration-compat",
    "fe-bundle-shape",
}


def _run(args, env_extra=None):
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    return subprocess.run(
        [str(ENTRYPOINT), "--cross-stage-parity", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_fixture(name, extra=None):
    fdir = FIXTURES / name
    return _run([
        "--root", str(fdir),
        "--config", str(fdir / "parity.tsv"),
        "--candidate-bundle", "candidate-bundle.json",
        "--candidate-compose", "candidate-compose.yml",
        "--deployed-head", "0001base",
        "--alembic-versions", "versions",
        *(extra or []),
    ])


# ── plumbing ──────────────────────────────────────────────────────────────────
def test_fixtures_present():
    for name in ("clean", "flagged"):
        d = FIXTURES / name
        assert (d / "parity.tsv").is_file(), f"missing {d}/parity.tsv"
        assert (d / "candidate-bundle.json").is_file(), f"missing {d}/candidate-bundle.json"
        assert (d / "candidate-compose.yml").is_file(), f"missing {d}/candidate-compose.yml"
        assert (d / "versions").is_dir(), f"missing {d}/versions"


# ── the four static checks + all four dimensions emitted ────────────────────--
def test_all_four_candidate_dimensions_emitted():
    res = _run_fixture("clean")
    missing = {d for d in EXPECTED_DIMENSIONS if d not in res.stdout}
    assert not missing, f"candidate-compat dimensions missing: {missing}\n{res.stdout}"


# ── fixtures + exit codes (AC: Exercised) ───────────────────────────────────--
def test_clean_candidate_passes_zero():
    """AC (b): a clean candidate PASSES zero — every static check green."""
    res = _run_fixture("clean")
    assert res.returncode == 0, res.stdout
    assert "RESULT: PASS" in res.stdout
    assert "✗ RED" not in res.stdout
    # all four candidate-compat rows are green
    for dim in EXPECTED_DIMENSIONS:
        assert any(
            ("✓ OK" in ln and "candidate" in ln and dim in ln)
            for ln in res.stdout.splitlines()
        ), f"expected a green candidate {dim} row\n{res.stdout}"


def test_flagged_candidate_is_flagged_nonzero_on_all_four_checks():
    """AC (a): a candidate with a non-reachable alembic head / a missing
    downstream env var / a placeholder digest (here: all of them, plus a
    skewed FE API contract) is FLAGGED non-zero."""
    res = _run_fixture("flagged")
    assert res.returncode == 1, res.stdout
    assert "RESULT: FAIL" in res.stdout
    red = [ln for ln in res.stdout.splitlines() if "✗ RED" in ln and "candidate" in ln]
    red_blob = "\n".join(red)
    # (1) placeholder (all-zeros) backend digest — deep-audit #23
    assert "bundle-completeness" in red_blob and "placeholder" in red_blob
    # (2) candidate-declared var with no downstream supplier
    assert "env-contract-delta" in red_blob
    assert "declared by candidate, no matching stage key: OMNISIGHT_EXPERIMENTAL_FLAG" in red_blob
    # (3) candidate alembic head not descendant-reachable from the deployed head
    assert "migration-compat" in red_blob
    assert "NOT descendant-reachable" in red_blob
    # (4) FE built against an API the backend no longer supports (V5 contract)
    assert "fe-bundle-shape" in red_blob
    assert "frontend_compat_check would FAIL" in red_blob


# ── reserved JSONL check_family (mirrors OP-1720) ───────────────────────────--
def test_jsonl_emits_candidate_compat_family(tmp_path):
    log = tmp_path / "preflight.jsonl"
    fdir = FIXTURES / "clean"
    res = _run(
        [
            "--root", str(fdir),
            "--config", str(fdir / "parity.tsv"),
            "--candidate-bundle", "candidate-bundle.json",
            "--candidate-compose", "candidate-compose.yml",
            "--deployed-head", "0001base",
            "--alembic-versions", "versions",
        ],
        env_extra={"DEPLOY_PARITY_JSONL_LOG": str(log)},
    )
    assert res.returncode == 0, res.stdout
    record = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["event"] == "deploy_line_parity"
    families = {row["check_family"] for row in record["rows"]}
    assert "candidate_compat" in families, families
    # the candidate rows carry exactly the four dimensions
    cand_dims = {
        row["dimension"] for row in record["rows"]
        if row["check_family"] == "candidate_compat"
    }
    assert cand_dims == EXPECTED_DIMENSIONS, cand_dims


# ── AC: Integration — real bundle.json + the real migration-compat checker ───--
def test_reads_real_bundle_and_real_migration_compat_checker():
    """The preflight reads the repo's real bundle.json and walks the real
    backend/alembic/versions tree through scripts/check_migration_compat.py
    (statically). The committed bundle.json is the local-dev placeholder, so a
    forward-compat break is the CORRECT verdict — what we pin here is that the
    real artefacts are read and the candidate-compat family is produced."""
    res = _run(["--candidate-bundle", "bundle.json", "--deployed-head", "0248"])
    out = res.stdout
    assert "candidate" in out
    for dim in EXPECTED_DIMENSIONS:
        assert dim in out, f"{dim} missing from real-repo run\n{out}"
    # the placeholder committed bundle is correctly flagged non-deployable
    assert res.returncode == 1
    assert "bundle-completeness" in out and "placeholder" in out


# ── MUST NOT: static-only — no live migration run against any DB ─────────────--
def test_static_only_no_live_migration_run():
    """The candidate migration check must be a static reachability walk that
    reuses the migration-compat checker's spec index + ancestry — never a live
    `alembic upgrade/downgrade` against a DB (the ticket's hard guardrail)."""
    text = ENGINE.read_text(encoding="utf-8")
    # reuse of the existing checker (read-only import), reachability via ancestry
    assert "import check_migration_compat" in text
    assert "_ancestry" in text and "_spec_index" in text
    # no live migration execution / no live stage bring-up triggered by the engine
    for forbidden in (
        "alembic upgrade", "alembic downgrade", "alembic current",
        "docker compose up", "docker-compose up", "docker run",
        "systemctl start", "systemctl enable",
    ):
        assert forbidden not in text, f"engine appears to run live ops: {forbidden!r}"


def test_migration_compat_checker_unedited_reuse():
    """backend/alembic + the migration-compat script are READ-ONLY reuse: the
    candidate check imports the checker, it does not fork or edit it."""
    assert MIGRATION_COMPAT.is_file()
    src = MIGRATION_COMPAT.read_text(encoding="utf-8")
    # the functions OP-1721 reuses still exist with their static signatures
    assert "def _spec_index(" in src
    assert "def _ancestry(" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
