"""[OP-964] AUDIT-16 — runner env can reach the `release_audit` Postgres DB.

The OP-925 R3 attempt skipped its `release_audit` write because the runner
env had no compatible audit DB: `psql` was not installed and the configured
asyncpg verification timed out (no `OMNISIGHT_DATABASE_URL` reached the
unit). These structural tests pin the three fixes so the regression can't
silently come back:

1. ``scripts/setup-dev-env.sh`` provisions ``postgresql-client`` (psql) and
   keeps a connectivity probe in its verification step.
2. ``deploy/systemd/auto-promote-develop.service`` reads the audit DSN from
   an ``EnvironmentFile`` (a systemd unit does not inherit the login shell
   env), so the OP-961 Python audit sink connects to pg-primary rather than
   the local SQLite default.
3. ``docs/operations/release-conductor-runbook.md`` carries the "Audit DB
   connectivity smoke test" section the DoD requires.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-dev-env.sh"
PROMOTE_UNIT = REPO_ROOT / "deploy" / "systemd" / "auto-promote-develop.service"
RUNBOOK = REPO_ROOT / "docs" / "operations" / "release-conductor-runbook.md"

ENV_FILE_PATH = "/home/user/.config/omnisight/release-audit.env"


# ── 1. setup-dev-env.sh installs postgresql-client ──────────────────────

def test_setup_script_exists_and_parses() -> None:
    assert SETUP_SCRIPT.is_file(), f"{SETUP_SCRIPT} missing"
    r = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


def test_setup_script_installs_postgresql_client() -> None:
    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    # Must appear inside (or adjacent to) the apt-get install block, not just
    # in a comment.
    install_idx = text.index("apt-get install")
    block = text[install_idx:install_idx + 600]
    assert "postgresql-client" in block, (
        "scripts/setup-dev-env.sh must `apt-get install ... postgresql-client` "
        "so the runner host has `psql` for the release_audit smoke test"
    )


def test_setup_script_keeps_release_audit_probe() -> None:
    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    assert "OMNISIGHT_DATABASE_URL" in text
    assert "release_audit" in text
    # The verification step prints the psql version (warns when absent).
    assert "psql --version" in text


# ── 2. auto-promote-develop.service wires the audit DSN ─────────────────

def test_promote_unit_has_environment_file_for_audit_dsn() -> None:
    text = PROMOTE_UNIT.read_text(encoding="utf-8")
    line = f"EnvironmentFile=-{ENV_FILE_PATH}"
    assert line in text, (
        f"{PROMOTE_UNIT} must read the release_audit DSN from {ENV_FILE_PATH} "
        "via `EnvironmentFile=-` (optional, so dev boxes without it still run)"
    )
    # Header must tell the operator what to put in that file.
    assert "OMNISIGHT_DATABASE_URL=" in text
    assert "release-audit.env" in text
    # Optional prefix `-` so a missing file is not a unit start failure.
    assert "EnvironmentFile=-/" in text


def test_promote_unit_references_smoke_test_runbook() -> None:
    text = PROMOTE_UNIT.read_text(encoding="utf-8")
    assert "release-conductor-runbook.md" in text
    assert "AUDIT-16" in text or "OP-964" in text


# ── 3. runbook has the Audit DB connectivity smoke test section ────────

def test_runbook_has_audit_db_smoke_test_section() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Audit DB connectivity smoke test" in text, (
        "release-conductor-runbook.md must document the §Audit DB "
        "connectivity smoke test (OP-964 DoD)"
    )
    # The diagnostic recipe from the ticket: which psql / SELECT 1 / asyncpg.
    assert "which psql" in text
    assert "SELECT 1" in text
    assert "asyncpg" in text
    # And the remediation hooks back to the two infra fixes.
    assert "postgresql-client" in text
    assert ENV_FILE_PATH in text


def test_runbook_smoke_test_lists_failure_modes() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text[text.index("Audit DB connectivity smoke test"):]
    for symptom in (
        "command not found",
        "could not connect",
        'relation "release_audit" does not exist',
    ):
        assert symptom in section, f"smoke-test triage table missing row: {symptom!r}"
