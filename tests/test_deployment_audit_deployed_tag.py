"""OP-1738 — deployment-audit deployed-tag assertion path.

Exercises the alembic-head ``auto`` check after OP-1738 wired
``OMNISIGHT_DEPLOYED_TAG`` into the audit env (defaulted from the committed
promotion ledger). The point of the ticket: the row must become a *real
pinned-release assertion* (RED when prod lags the deployed release) instead of
the old always-informational develop-trunk fallback — WITHOUT making the
develop-fallback case RED (OP-1701 invariant) and WITHOUT touching the
OP-1701 git-ref comparison logic itself.

All cases run the real ``scripts/deployment-audit.sh`` against a throwaway git
repo so they never touch the live deploy line. ``cur`` (prod's applied
revision) is driven by a fake ``backend/.venv/bin/alembic`` and the prod-PG
container name is pointed at a bogus name so the docker read is a no-op.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "deployment-audit.sh"

# Two-revision linear chain: a1a1 (base) -> b2b2 (leaf/head).
_BASE = "revision = 'a1a1'\ndown_revision = None\n"
_NEXT = "revision = 'b2b2'\ndown_revision = 'a1a1'\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def audit_repo(tmp_path: Path) -> Path:
    """A throwaway repo carrying the real script, a tagged migration tree, and
    a promotion ledger whose last promote is the tag ``vtest`` (head=b2b2)."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, repo / "scripts" / "deployment-audit.sh")
    os.chmod(repo / "scripts" / "deployment-audit.sh", 0o755)

    versions = repo / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_base.py").write_text(_BASE, encoding="utf-8")
    (versions / "0002_next.py").write_text(_NEXT, encoding="utf-8")

    (repo / "audit").mkdir()
    (repo / "audit" / "image_promotion_audit.jsonl").write_text(
        json.dumps({"event": "image_bundle_promoted", "version": "vtest"}) + "\n",
        encoding="utf-8",
    )

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    # Tag the deployed release + a develop ref at the leaf-head commit.
    # (-m so it works whether or not global config forces annotated tags.)
    _git(repo, "tag", "-m", "vtest", "vtest")
    _git(repo, "branch", "develop-fixture")
    return repo


def _fake_alembic(repo: Path, current_rev: str) -> None:
    venv_bin = repo / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    alembic = venv_bin / "alembic"
    alembic.write_text(f'#!/usr/bin/env bash\necho "{current_rev} (head)"\n', encoding="utf-8")
    os.chmod(alembic, 0o755)


def _run(repo: Path, expected: str = "yes", env_extra: dict | None = None):
    manifest = repo / "manifest.tsv"
    manifest.write_text(f"alembic-head\tauto\t{expected}\tOP-1738\tassertion\n", encoding="utf-8")
    env = {
        **os.environ,
        "USER": os.environ.get("USER", "tester"),
        "DEPLOYMENT_AUDIT_USER_SYSTEMD": "0",
        # Point the prod-PG read at a bogus container so it is a no-op and `cur`
        # falls back to the fake local alembic below.
        "OMNISIGHT_PROD_PG_CONTAINER": "omnisight-pg-nonexistent-test-xyz",
    }
    env.update(env_extra or {})
    return subprocess.run(
        [str(repo / "scripts" / "deployment-audit.sh"), str(manifest)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_ledger_pins_deployed_tag_and_passes_when_prod_at_head(audit_repo: Path) -> None:
    """Ledger-derived pin + prod at the deployed-release head → OK assertion."""
    _fake_alembic(audit_repo, "b2b2")
    res = _run(audit_repo)
    assert "deployed_tag=vtest" in res.stdout, res.stdout
    assert "prod current=b2b2 == deployed release (vtest) head" in res.stdout, res.stdout
    assert res.returncode == 0, res.stdout


def test_ledger_pin_asserts_red_when_prod_lags_deployed_release(audit_repo: Path) -> None:
    """The core OP-1738 win: prod behind the pinned deployed tag is a real,
    FATAL red row — no longer silently informational."""
    _fake_alembic(audit_repo, "a1a1")
    res = _run(audit_repo)
    assert "deployed_tag=vtest" in res.stdout, res.stdout
    assert "✗ RED" in res.stdout, res.stdout
    assert "behind the pinned deployed release (vtest) head=b2b2" in res.stdout, res.stdout
    assert res.returncode == 1, res.stdout


def test_develop_fallback_stays_informational_never_red(audit_repo: Path) -> None:
    """OP-1701 invariant preserved: with NO deployed tag (no ledger, no env) the
    develop-trunk comparison must stay informational even when prod lags."""
    (audit_repo / "audit" / "image_promotion_audit.jsonl").unlink()
    _fake_alembic(audit_repo, "a1a1")
    res = _run(audit_repo, env_extra={"OMNISIGHT_AUDIT_DEVELOP_REF": "develop-fixture"})
    assert "deployed_tag=<none>" in res.stdout, res.stdout
    assert "✗ RED" not in res.stdout, res.stdout
    assert "informational" in res.stdout, res.stdout
    assert res.returncode == 0, res.stdout


def test_stale_main_rc1_annotation_is_refreshed() -> None:
    """OP-1738: the stale "stranded on main@rc1 / deferred to cutover" note is
    gone (OP-1608 re-point landed)."""
    text = REAL_SCRIPT.read_text(encoding="utf-8")
    assert "stranded on main@rc1" not in text
    assert "deferred to cutover" not in text
    assert "re-pointed off main@rc1 per OP-1608" in text


def test_alembic_auto_row_is_a_pinned_release_assertion() -> None:
    """The alembic-head `auto` manifest row is now an assertion (expected=yes),
    not the old informational `n-a`."""
    text = REAL_SCRIPT.read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("alembic-head")]
    assert len(rows) == 1, rows
    fields = rows[0].split()
    assert fields[:3] == ["alembic-head", "auto", "yes"], fields


def test_single_canonical_script_in_repo() -> None:
    """OP-1738 dedupe: exactly one deployment-audit.sh under the repo."""
    copies = list(REPO_ROOT.rglob("deployment-audit.sh"))
    assert copies == [REAL_SCRIPT], copies


def test_explicit_env_tag_overrides_ledger(audit_repo: Path) -> None:
    """An explicit OMNISIGHT_DEPLOYED_TAG wins over the ledger default."""
    (audit_repo / "audit" / "image_promotion_audit.jsonl").write_text(
        json.dumps({"event": "image_bundle_promoted", "version": "v9.9.9-bogus"}) + "\n",
        encoding="utf-8",
    )
    _fake_alembic(audit_repo, "b2b2")
    res = _run(audit_repo, env_extra={"OMNISIGHT_DEPLOYED_TAG": "vtest"})
    assert "deployed_tag=vtest" in res.stdout, res.stdout
    assert "== deployed release (vtest) head" in res.stdout, res.stdout
    assert res.returncode == 0, res.stdout
