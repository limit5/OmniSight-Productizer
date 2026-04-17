"""G4 #5 (TODO row 1364) — contract tests for the CI Postgres service matrix.

Pins the shape of ``.github/workflows/db-engine-matrix.yml`` so that an
accidental edit (`continue-on-error: true` creeps back onto postgres-matrix,
PG 15/16/17 stops being three cells, the live-integration job loses its
Postgres service container, `OMNI_TEST_PG_URL` env goes missing, etc.)
fails CI *before* the next PR ships a silently-softened DB gate.

No network / no Docker required: the workflow YAML is parsed with
``yaml.safe_load`` and its shape is asserted against a handful of
load-bearing invariants. Runs in <100 ms alongside the rest of the
backend test suite.

Why this file exists (and not just trust the human reviewer): the
pre-G4 workflow intentionally ran postgres-matrix as **advisory**, so
"please review `continue-on-error: true`" is a weak review signal —
the same line was correct six months ago. These tests freeze the
post-G4 contract into an explicit rule set the reviewer can point at.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "db-engine-matrix.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW_PATH.exists(), f"workflow missing: {WORKFLOW_PATH}"
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _triggers(workflow: dict) -> dict:
    # PyYAML turns bareword `on:` into Python True. Accept either spelling.
    return workflow.get("on") or workflow.get(True) or {}


# ---------------------------------------------------------------------------
# Triggers — matrix must fire on PRs touching any of the PG/SQLite layers
# ---------------------------------------------------------------------------


def test_triggered_on_pr_and_push(workflow: dict) -> None:
    triggers = _triggers(workflow)
    assert "push" in triggers, "must fire on push to catch post-merge regressions"
    assert "pull_request" in triggers, "must fire on PR — this is the merge gate"


def test_triggered_on_manual_dispatch(workflow: dict) -> None:
    triggers = _triggers(workflow)
    assert "workflow_dispatch" in triggers, (
        "operator escape hatch required — release branches need a full sweep "
        "on demand, not just on paths-touched."
    )


def test_path_filters_cover_pg_shim_artefacts(workflow: dict) -> None:
    triggers = _triggers(workflow)
    for event in ("push", "pull_request"):
        paths = triggers.get(event, {}).get("paths") or []
        joined = "\n".join(paths)
        # G4 #1 shim — if this file changes the matrix MUST re-run.
        assert "backend/alembic_pg_compat.py" in joined, (
            f"{event} path filter must include the G4 #1 shim"
        )
        # G4 #2 connection abstraction — if dispatcher changes the matrix re-runs.
        assert "backend/db_url.py" in joined, (
            f"{event} path filter must include the G4 #2 db_url abstraction"
        )
        assert "backend/db_connection.py" in joined, (
            f"{event} path filter must include the G4 #2 connection dispatcher"
        )
        # G4 #4 migration script — changes to the migrator MUST re-verify.
        assert "scripts/migrate_sqlite_to_pg.py" in joined, (
            f"{event} path filter must include the G4 #4 migration script"
        )
        # Alembic revisions (always relevant).
        assert "backend/alembic/**" in joined


# ---------------------------------------------------------------------------
# SQLite matrix — still present, still hard gate (pre-cutover)
# ---------------------------------------------------------------------------


def test_sqlite_matrix_job_exists(workflow: dict) -> None:
    jobs = workflow.get("jobs") or {}
    assert "sqlite-matrix" in jobs, (
        "sqlite-matrix is the pre-cutover production engine — removing it "
        "before docs/ops/db_failover.md declares cutover is a regression."
    )


def test_sqlite_matrix_is_hard_gate(workflow: dict) -> None:
    job = workflow["jobs"]["sqlite-matrix"]
    # SQLite cells must not be advisory — SQLite IS the live engine today.
    assert job.get("continue-on-error") in (None, False), (
        "sqlite-matrix must remain a hard gate while SQLite is production"
    )


def test_sqlite_matrix_covers_two_versions(workflow: dict) -> None:
    job = workflow["jobs"]["sqlite-matrix"]
    include = job["strategy"]["matrix"]["include"]
    versions = sorted(entry["sqlite"] for entry in include)
    # 3.40.x (Debian bookworm floor) + 3.45.x (current Python 3.12 baseline).
    assert len(versions) >= 2, "sqlite-matrix must exercise >=2 versions"
    assert any(v.startswith("3.40") for v in versions), "3.40 floor required"
    assert any(v.startswith("3.45") for v in versions), "3.45 baseline required"


# ---------------------------------------------------------------------------
# Postgres matrix — HARD GATE since G4 #1 landed (the headline of this PR)
# ---------------------------------------------------------------------------


def test_postgres_matrix_job_exists(workflow: dict) -> None:
    jobs = workflow.get("jobs") or {}
    assert "postgres-matrix" in jobs, (
        "the Postgres dual-track cell is the whole point of G4 #5"
    )


def test_postgres_matrix_is_hard_gate_for_15_and_16(workflow: dict) -> None:
    job = workflow["jobs"]["postgres-matrix"]
    # The `continue-on-error` expression MUST NOT be a bare `true`
    # (which would make every cell advisory). It MAY be an expression
    # that scopes advisory to the 17 forward-look cell only.
    coe = job.get("continue-on-error")
    assert coe is not True, (
        "postgres-matrix must be a hard gate for PG 15 + 16 since G4 #1 "
        "landed the runtime shim. Setting `continue-on-error: true` here "
        "unilaterally regresses the PR merge gate."
    )
    if coe is not None and coe is not False:
        # If scoped, it must reference the forward-look (17) cell only.
        assert "17" in str(coe), (
            "scoped continue-on-error must reference PG 17 as the only "
            "advisory cell (N7 forward-look pattern)"
        )


def test_postgres_matrix_includes_15_16_17(workflow: dict) -> None:
    job = workflow["jobs"]["postgres-matrix"]
    versions = job["strategy"]["matrix"]["postgres"]
    assert set(versions) >= {"15", "16", "17"}, (
        f"postgres-matrix must cover PG 15 (floor) + 16 (production) + 17 "
        f"(forward-look, N7 pattern). Got: {versions}"
    )


def test_postgres_matrix_has_service_container(workflow: dict) -> None:
    job = workflow["jobs"]["postgres-matrix"]
    services = job.get("services") or {}
    assert "postgres" in services, "must declare a postgres service container"
    svc = services["postgres"]
    assert str(svc["image"]).startswith("postgres:"), (
        "service image must be a postgres: tag (matrix variable picks version)"
    )
    # Health-check must exist — without it the dual-track step races the
    # container and fails with `connection refused` on cold runners.
    options = str(svc.get("options") or "")
    assert "pg_isready" in options, "service must have pg_isready health-check"


def test_postgres_matrix_runs_dual_track_validator(workflow: dict) -> None:
    job = workflow["jobs"]["postgres-matrix"]
    all_run_text = "\n".join(
        str(step.get("run", "")) for step in job.get("steps", [])
    )
    assert "alembic_dual_track.py" in all_run_text, (
        "postgres-matrix must run scripts/alembic_dual_track.py --engine postgres"
    )
    assert "--engine postgres" in all_run_text


# ---------------------------------------------------------------------------
# pg-live-integration — G4 #5 headline job
# ---------------------------------------------------------------------------


def test_pg_live_integration_job_exists(workflow: dict) -> None:
    jobs = workflow.get("jobs") or {}
    assert "pg-live-integration" in jobs, (
        "G4 #5 delivers pg-live-integration: the job that runs the "
        "OMNI_TEST_PG_URL-gated live tests against a real PG service."
    )


def test_pg_live_integration_is_hard_gate(workflow: dict) -> None:
    job = workflow["jobs"]["pg-live-integration"]
    assert job.get("continue-on-error") in (None, False), (
        "pg-live-integration must be a hard gate — the whole point of "
        "having a live test is to block regression, not to warn about it."
    )


def test_pg_live_integration_has_postgres_service(workflow: dict) -> None:
    job = workflow["jobs"]["pg-live-integration"]
    services = job.get("services") or {}
    assert "postgres" in services
    assert str(services["postgres"]["image"]).startswith("postgres:")


def test_pg_live_integration_exports_omni_test_pg_url(workflow: dict) -> None:
    job = workflow["jobs"]["pg-live-integration"]
    # The env var can live on a step, a job, or be set inline. We
    # accept any of them — the contract is that SOMETHING sets it.
    step_envs = [step.get("env") or {} for step in job.get("steps", [])]
    run_texts = [str(step.get("run", "")) for step in job.get("steps", [])]

    seen = False
    for env in step_envs:
        if "OMNI_TEST_PG_URL" in env:
            url = env["OMNI_TEST_PG_URL"]
            assert "postgresql" in url, (
                f"OMNI_TEST_PG_URL must be a postgres URL, got: {url!r}"
            )
            seen = True
    # Or set inline in a `run:` step.
    if not seen:
        for text in run_texts:
            if "OMNI_TEST_PG_URL" in text:
                seen = True
                break
    assert seen, (
        "pg-live-integration must set OMNI_TEST_PG_URL so "
        "test_alembic_pg_live_upgrade.py's pytest.mark.skipif lifts"
    )


def test_pg_live_integration_runs_alembic_pg_live_test(workflow: dict) -> None:
    job = workflow["jobs"]["pg-live-integration"]
    all_run_text = "\n".join(
        str(step.get("run", "")) for step in job.get("steps", [])
    )
    assert "test_alembic_pg_live_upgrade.py" in all_run_text, (
        "pg-live-integration must pytest test_alembic_pg_live_upgrade.py — "
        "that's the G4 #1 live contract this job exists to pin"
    )


def test_pg_live_integration_runs_migrate_script_smoke(workflow: dict) -> None:
    job = workflow["jobs"]["pg-live-integration"]
    all_run_text = "\n".join(
        str(step.get("run", "")) for step in job.get("steps", [])
    )
    assert "migrate_sqlite_to_pg.py" in all_run_text, (
        "pg-live-integration must smoke-run scripts/migrate_sqlite_to_pg.py "
        "(G4 #4) so the data-migration entry point is always callable"
    )
    assert "--dry-run" in all_run_text, (
        "smoke must use --dry-run — the goal is to catch regressions in "
        "the source-chain verifier, not to push a real migration in CI"
    )


def test_pg_live_integration_installs_both_drivers(workflow: dict) -> None:
    job = workflow["jobs"]["pg-live-integration"]
    all_run_text = "\n".join(
        str(step.get("run", "")) for step in job.get("steps", [])
    )
    # psycopg2 = Alembic (sync); asyncpg = runtime (G4 #2 dispatcher).
    assert "psycopg2" in all_run_text, (
        "must install psycopg2 for Alembic's sync connection"
    )
    assert "asyncpg" in all_run_text, (
        "must install asyncpg so the migrate-script dry-run doesn't "
        "crash on the lazy-import sanity check (G4 #2 dispatcher)"
    )


# ---------------------------------------------------------------------------
# Summary roll-up — must include the new PG cells
# ---------------------------------------------------------------------------


def test_summary_needs_all_matrix_cells(workflow: dict) -> None:
    job = workflow["jobs"]["matrix-summary"]
    needs = set(job.get("needs") or [])
    # The roll-up writes a step summary on the run page. If it doesn't
    # depend on a cell, a red-X on that cell won't show in the summary.
    for expected in (
        "sqlite-matrix",
        "postgres-matrix",
        "pg-live-integration",
        "engine-syntax-scan",
    ):
        assert expected in needs, f"summary must depend on {expected}"


def test_summary_runs_even_if_cells_fail(workflow: dict) -> None:
    job = workflow["jobs"]["matrix-summary"]
    # `if: always()` ensures the roll-up is written even when a hard-
    # gate cell is red — that's the whole point of the roll-up.
    assert job.get("if") == "always()", (
        "matrix-summary must run even on cell failure so the roll-up "
        "table is always readable"
    )


# ---------------------------------------------------------------------------
# Concurrency — ref-scoped, cancels in progress
# ---------------------------------------------------------------------------


def test_concurrency_is_ref_scoped(workflow: dict) -> None:
    conc = workflow.get("concurrency") or {}
    assert conc.get("cancel-in-progress") is True, (
        "stacked PR pushes should cancel the in-flight run — without "
        "this a 3-push PR eats 3× the Actions minutes budget"
    )
    assert "github.ref" in str(conc.get("group", "")), (
        "concurrency group must include github.ref so different PRs "
        "don't cancel each other"
    )


# ---------------------------------------------------------------------------
# Cross-artefact anchor — `alembic_pg_compat.install_pg_compat` must be
# wired into `backend/alembic/env.py`, otherwise the shim is dead code
# and the PG cells would pass for a different reason than we expect.
# ---------------------------------------------------------------------------


def test_alembic_env_installs_pg_compat_shim() -> None:
    env_py = REPO_ROOT / "backend" / "alembic" / "env.py"
    text = env_py.read_text()
    assert "install_pg_compat" in text, (
        "env.py must install the G4 #1 PG compat shim — if it doesn't, "
        "the postgres-matrix cell's greenness comes from Alembic somehow "
        "running SQLite-idiom SQL on PG, which is a false-positive we "
        "really do not want"
    )
