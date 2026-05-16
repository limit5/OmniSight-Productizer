r"""[OP-978] AUDIT-25 — `scripts/portability-audit.sh` standalone CLI.

The pytest module ``tests/test_staging_migration_5a_to_5c.py`` is the
*canonical* definition of "what counts as a 5a→5c portability hit"; this
shell CLI (salvaged from the abandoned Gerrit #487) mirrors the same four
facets so an operator / CI gate / pre-commit hook can run a single
executable with exit codes instead of invoking pytest. These tests pin:

  * the CLI's shape — exists, executable, valid bash, the documented
    flags (``--verbose`` / ``--json`` / ``--help``);
  * its contract — exit ``0`` clean / ``1`` ≥1 finding / ``2`` usage error
    (AUDIT-25 AC), a human report by default and a single JSON object
    under ``--json``;
  * that it actually catches each facet — a planted violation per facet in
    a throw-away mini-copy of the repo flips the exit code and surfaces in
    ``findings[]``;
  * the **ScriptDriftsFromPytest** regression guard (ticket "Error
    catalog"): the shell CLI and the canonical pytest checks must agree
    that the live repo is clean (0 hits on both).

Cost: a handful of bash subprocesses + a tmp copytree; stdlib + pytest,
no docker / systemd / network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT = REPO_ROOT / "scripts" / "portability-audit.sh"

# the artifact set the audit copies into a mini-repo to plant violations —
# kept in sync with PORTABLE_FILES / SYSTEMD_UNITS in portability-audit.sh.
PORTABLE_FILES = [
    "infra/staging/.env.template",
    "infra/staging/anonymize.sh",
    "infra/staging/anonymize-fields.yaml",
    "infra/staging/snapshot-restore.sh",
    "infra/staging/verify-env-contract.sh",
    "deploy/staging/docker-compose.yml",
    "deploy/staging/caddy.json",
    "scripts/staging_deploy.sh",
    "scripts/sync_staging_to_develop.sh",
]
SYSTEMD_UNITS = [
    "deploy/systemd/omnisight-staging-compose.service",
    "deploy/systemd/staging-pg-snapshot.service",
    "deploy/systemd/staging-pg-snapshot.timer",
    "deploy/systemd/staging-sync.service",
    "deploy/systemd/staging-sync.timer",
]


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kw)


# ─────────────────────────────────────────────────────────────────────
# shape
# ─────────────────────────────────────────────────────────────────────
def test_audit_script_exists_executable_and_valid_bash():
    assert AUDIT.exists(), "scripts/portability-audit.sh is missing"
    assert AUDIT.stat().st_mode & 0o111, "portability-audit.sh must be executable"
    r = _run(["bash", "-n", str(AUDIT)])
    assert r.returncode == 0, f"portability-audit.sh failed `bash -n`: {r.stderr}"


def test_audit_help_exits_zero_and_usage_error_exits_two():
    assert _run(["bash", str(AUDIT), "--help"]).returncode == 0
    r = _run(["bash", str(AUDIT), "--bogus-flag"])
    assert r.returncode == 2, "an unknown flag must be a usage error (exit 2)"
    assert "usage:" in r.stderr


def test_audit_covers_all_four_ac1_facets():
    text = AUDIT.read_text()
    for marker in ("relative or", ":6432", "parameterised", "fraction"):
        assert marker in text, f"portability-audit.sh missing AC#1 facet marker: {marker!r}"
    for f in PORTABLE_FILES + SYSTEMD_UNITS:
        assert f in text, f"portability-audit.sh does not cover artifact {f}"


# ─────────────────────────────────────────────────────────────────────
# contract — clean live repo
# ─────────────────────────────────────────────────────────────────────
def test_audit_clean_on_live_repo_human_mode():
    """AUDIT-25 AC: runs with no args, exit 0 when there are zero hits."""
    r = _run(["bash", str(AUDIT)])
    assert r.returncode == 0, (
        "portability-audit.sh found a portability regression in the staging "
        f"artifacts:\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    assert "0 hits" in r.stdout
    assert "operator-edit points" in r.stdout
    for u in SYSTEMD_UNITS:
        assert u in r.stdout


def test_audit_json_mode_is_a_single_clean_object():
    """AUDIT-25 AC: `--json` emits one machine-readable object for CI."""
    r = _run(["bash", str(AUDIT), "--json"])
    assert r.returncode == 0
    doc = json.loads(r.stdout)  # raises if not exactly one JSON value
    assert doc["audit"] == "portability-audit"
    assert doc["hits"] == 0
    assert doc["exit"] == 0
    assert doc["findings"] == []
    assert {ep["unit"] for ep in doc["operator_edit_points"]} == set(SYSTEMD_UNITS)


# ─────────────────────────────────────────────────────────────────────
# behaviour — planted violations in a throw-away mini-copy
# ─────────────────────────────────────────────────────────────────────
def _mini_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in [*PORTABLE_FILES, *SYSTEMD_UNITS, "scripts/portability-audit.sh"]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst)
    return root


def _audit_in(root: Path, *flags):
    return _run(["bash", str(root / "scripts" / "portability-audit.sh"), *flags])


def test_audit_passes_on_clean_mini_copy(tmp_path):
    assert _audit_in(_mini_repo(tmp_path)).returncode == 0


@pytest.mark.parametrize(
    "facet, rel, mutate, needle",
    [
        # A — a bare absolute host path baked into a portable artifact
        ("A", "infra/staging/snapshot-restore.sh",
         lambda t: t + "\nLEAK_DIR=/home/someone/private/state\n", "bare absolute path"),
        # B — a hard-coded prod pgbouncer endpoint
        ("B", "scripts/sync_staging_to_develop.sh",
         lambda t: t + "\nPROD_POOL=localhost:6432\n", ":6432"),
        # C — an unparameterised published host port in the staging compose
        ("C", "deploy/staging/docker-compose.yml",
         lambda t: t.replace('"${STAGING_POSTGRES_PORT:-55432}:5432"', '"55432:5432"'),
         "not parameterised"),
        # D — an absolute cgroup ceiling instead of a host fraction
        ("D", "deploy/systemd/omnisight-staging-compose.service",
         lambda t: t.replace("MemoryMax=30%", "MemoryMax=8G"), "absolute value"),
    ],
    ids=["A-bare-abs-path", "B-pgbouncer-6432", "C-unparam-port", "D-abs-cgroup"],
)
def test_audit_flags_each_facet(tmp_path, facet, rel, mutate, needle):
    root = _mini_repo(tmp_path)
    target = root / rel
    before = target.read_text()
    target.write_text(mutate(before))
    assert target.read_text() != before, "fixture mutation was a no-op — fix the test"
    r = _audit_in(root)
    assert r.returncode == 1, f"facet {facet}: a planted violation must exit 1\n{r.stdout}"
    assert needle in r.stdout
    doc = json.loads(_audit_in(root, "--json").stdout)
    assert doc["hits"] >= 1 and doc["exit"] == 1
    assert any(f["facet"] == facet for f in doc["findings"]), f"facet {facet} not in findings[]"


def test_audit_env_var_default_path_is_not_flagged(tmp_path):
    """The *correct* way to write a host path — ${VAR:-/abs} — must pass."""
    root = _mini_repo(tmp_path)
    target = root / "infra" / "staging" / "snapshot-restore.sh"
    target.write_text(target.read_text() + '\nLEAK_DIR="${LEAK_DIR:-/home/user/state}"\n')
    assert _audit_in(root).returncode == 0


def test_audit_missing_artifact_is_a_usage_error(tmp_path):
    root = _mini_repo(tmp_path)
    (root / "deploy" / "staging" / "caddy.json").unlink()
    r = _audit_in(root)
    assert r.returncode == 2, "a missing declared artifact must be exit 2, not a false pass"


# ─────────────────────────────────────────────────────────────────────
# ScriptDriftsFromPytest — shell CLI and canonical pytest must agree
# ─────────────────────────────────────────────────────────────────────
def test_shell_audit_agrees_with_canonical_pytest_on_live_repo():
    """Error-catalog `ScriptDriftsFromPytest`: the shell CLI and the
    canonical pytest checks (tests/test_staging_migration_5a_to_5c.py) must
    not disagree on whether the live repo is portable — both must be clean.
    If a future artifact change makes only one of them red, this fails and
    forces the two definitions back into sync."""
    sys.path.insert(0, os.path.dirname(__file__))
    import test_staging_migration_5a_to_5c as canonical  # noqa: E402

    pytest_hits: list[str] = []
    for _name, fn in canonical.PORTABILITY_CHECKS:
        pytest_hits.extend(fn())

    shell = _run(["bash", str(AUDIT), "--json"])
    shell_doc = json.loads(shell.stdout)
    shell_hits = shell_doc["findings"]

    assert not pytest_hits and not shell_hits, (
        "shell CLI and canonical pytest disagree (or both found hits) — "
        f"pytest={pytest_hits!r}  shell={shell_hits!r}"
    )
    assert (shell.returncode == 0) == (not pytest_hits)
