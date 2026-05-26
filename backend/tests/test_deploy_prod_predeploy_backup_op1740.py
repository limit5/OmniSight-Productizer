"""OP-1740 — pre-deploy backup is fail-closed + sources the passphrase env.

Background
----------
``scripts/deploy-prod.sh`` Step 1b takes a WAL-safe pre-deploy backup
via ``scripts/backup_prod_db.sh`` before any replica restarts. Two
fragilities were fixed in OP-1740:

* **F15** — the missing/non-executable backup-helper branch used to
  ``warn`` "proceeding WITHOUT backup" and continue. That let the
  "pg_dump first" rule be silently skipped. It now ``err``s and aborts
  (fail-closed), unless the operator passes an explicit ``--skip-backup``.
* **F6-residual** — the backup passphrase (``OMNISIGHT_BACKUP_PASSPHRASE``)
  is NOT in ``.env``; it lives in ``/etc/omnisight/backup-dr.env`` (the
  backup timers source it). An interactive operator deploy hit
  "passphrase required" and had to hunt for it (live during v0.6.2).
  ``deploy-prod.sh`` now sources that file (path overridable via
  ``OMNISIGHT_BACKUP_DR_ENV``) before the backup step so the passphrase
  is available; it is never printed.

Why subprocess + a sandbox copy
-------------------------------
The contract is bash exit codes + the operator-facing message at the
real process boundary. The fail-closed branch only fires when the
backup helper is *absent*, so we run ``deploy-prod.sh`` from a sandbox
copy of the repo where we control whether ``scripts/backup_prod_db.sh``
exists. A fake backup helper records (without printing) whether the
passphrase reached it, then exits non-zero so the deploy aborts at
Step 1b — never touching docker. That keeps the test hermetic on a host
with no docker daemon.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"
VERIFIER = REPO_ROOT / "scripts" / "check_deploy_ref.sh"

GOOD_DIGEST = "sha256:" + "a" * 64
TEST_PASSPHRASE = "op1740-test-passphrase-not-a-real-secret"

# A fake backup helper: it records ONLY whether the passphrase reached it
# (never the value) into $OMNISIGHT_TEST_MARKER, then exits non-zero so
# deploy-prod.sh aborts at Step 1b before any docker step runs.
FAKE_BACKUP = """\
#!/usr/bin/env bash
if [ -n "${OMNISIGHT_BACKUP_PASSPHRASE:-}" ]; then
  printf 'PASSPHRASE_PRESENT\\n' > "$OMNISIGHT_TEST_MARKER"
else
  printf 'PASSPHRASE_ABSENT\\n' > "$OMNISIGHT_TEST_MARKER"
fi
exit 7
"""


def _make_sandbox(tmp_path: Path, *, with_backup_helper: bool) -> Path:
    """A minimal repo sandbox deploy-prod.sh can run a digest deploy in.

    Contains the real ``deploy-prod.sh`` + ``check_deploy_ref.sh`` (so the
    digest gate passes) and an empty ``.env``. ``backup_prod_db.sh`` is the
    FAKE helper above, present only when ``with_backup_helper`` is True — its
    absence exercises the fail-closed branch.
    """
    sandbox = tmp_path / "repo"
    scripts = sandbox / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(DEPLOY_SH, scripts / "deploy-prod.sh")
    shutil.copy2(VERIFIER, scripts / "check_deploy_ref.sh")
    (sandbox / ".env").write_text("", encoding="utf-8")
    if with_backup_helper:
        helper = scripts / "backup_prod_db.sh"
        helper.write_text(FAKE_BACKUP, encoding="utf-8")
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return sandbox


def _run_deploy(sandbox: Path, *args: str, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a real (non --dry-run) digest deploy in the sandbox.

    The env is built fresh WITHOUT ``OMNISIGHT_BACKUP_PASSPHRASE`` so the
    only way the passphrase can reach the backup helper is via the sourced
    backup-dr.env. ``OMNISIGHT_REGISTRY`` is required by the digest path.
    """
    env = {k: v for k, v in os.environ.items() if k != "OMNISIGHT_BACKUP_PASSPHRASE"}
    env["OMNISIGHT_REGISTRY"] = "reg.example/ns"
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(sandbox / "scripts" / "deploy-prod.sh"), f"--digest={GOOD_DIGEST}", *args],
        capture_output=True,
        text=True,
        cwd=str(sandbox),
        env=env,
    )


# ═════════════════════════════════════════════════════════════════════
# F15 — fail-closed when the backup helper is missing
# ═════════════════════════════════════════════════════════════════════


def test_missing_backup_helper_aborts_fail_closed(tmp_path: Path) -> None:
    """No backup helper → deploy ABORTS (not warns) before any docker step."""
    sandbox = _make_sandbox(tmp_path, with_backup_helper=False)
    proc = _run_deploy(sandbox, env_extra={})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "missing or non-executable" in out
    assert "fail-closed" in out
    # It must NOT have fallen through to the retired warn-and-proceed path.
    assert "proceeding WITHOUT backup" not in out


def test_non_executable_backup_helper_aborts_fail_closed(tmp_path: Path) -> None:
    """A present-but-non-executable helper also fails closed (-x is false)."""
    sandbox = _make_sandbox(tmp_path, with_backup_helper=True)
    helper = sandbox / "scripts" / "backup_prod_db.sh"
    helper.chmod(0o644)  # readable but NOT executable
    proc = _run_deploy(sandbox, env_extra={})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "missing or non-executable" in out


def test_skip_backup_overrides_fail_closed(tmp_path: Path) -> None:
    """--skip-backup is the sanctioned override: it skips (loudly) instead
    of aborting at the backup gate, even with no backup helper present."""
    sandbox = _make_sandbox(tmp_path, with_backup_helper=False)
    proc = _run_deploy(sandbox, "--skip-backup", env_extra={})
    out = proc.stdout + proc.stderr
    assert "SKIPPING pre-deploy backup" in out
    # The fail-closed abort must NOT have fired.
    assert "missing or non-executable" not in out


# ═════════════════════════════════════════════════════════════════════
# F6-residual — the passphrase is sourced from backup-dr.env
# ═════════════════════════════════════════════════════════════════════


def test_passphrase_sourced_from_backup_dr_env(tmp_path: Path) -> None:
    """With the passphrase absent from the shell, deploy-prod.sh sources it
    from $OMNISIGHT_BACKUP_DR_ENV and it reaches the backup helper."""
    sandbox = _make_sandbox(tmp_path, with_backup_helper=True)
    dr_env = tmp_path / "backup-dr.env"
    dr_env.write_text(
        f"OMNISIGHT_BACKUP_PASSPHRASE={TEST_PASSPHRASE}\n", encoding="utf-8"
    )
    marker = tmp_path / "marker.txt"
    proc = _run_deploy(
        sandbox,
        env_extra={
            "OMNISIGHT_BACKUP_DR_ENV": str(dr_env),
            "OMNISIGHT_TEST_MARKER": str(marker),
        },
    )
    out = proc.stdout + proc.stderr
    assert f"Sourced backup env from {dr_env}" in out
    # The passphrase reached the helper — proving the env-source path works.
    assert marker.read_text(encoding="utf-8").strip() == "PASSPHRASE_PRESENT"
    # MUST NOT echo the passphrase value anywhere.
    assert TEST_PASSPHRASE not in out


def test_passphrase_absent_without_dr_env_is_the_negative_control(tmp_path: Path) -> None:
    """Control: with NO backup-dr.env and an empty shell, the helper sees no
    passphrase — confirming it is the sourced file (not ambient env) that
    delivers it in the positive test above."""
    sandbox = _make_sandbox(tmp_path, with_backup_helper=True)
    marker = tmp_path / "marker.txt"
    missing = tmp_path / "does-not-exist.env"
    proc = _run_deploy(
        sandbox,
        env_extra={
            "OMNISIGHT_BACKUP_DR_ENV": str(missing),
            "OMNISIGHT_TEST_MARKER": str(marker),
        },
    )
    out = proc.stdout + proc.stderr
    assert f"backup env {missing} not found" in out
    assert marker.read_text(encoding="utf-8").strip() == "PASSPHRASE_ABSENT"


# ═════════════════════════════════════════════════════════════════════
# Static guards — the source carries the OP-1740 contract
# ═════════════════════════════════════════════════════════════════════


def test_deploy_script_is_valid_bash() -> None:
    rc = subprocess.run(["bash", "-n", str(DEPLOY_SH)], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


def test_retired_warn_and_proceed_branch_is_gone() -> None:
    """The old fail-OPEN message must not survive anywhere in the script."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    assert "proceeding WITHOUT backup" not in body
    assert "--skip-backup" in body
    assert "BACKUP_DR_ENV" in body
