"""OP-1127 (Boreas-A2) — synthetic drift integration test.

Covers the Code AC + Integration AC of the alembic image-DB drift gate:

* Code AC — ``backend.alembic_drift_gate`` runs before the listen port is
  bound; on mismatch it prints a structured JSON diagnostic
  (``image_head``, ``db_head``, ``drift_direction``) to stderr and exits
  non-zero. On match it exits 0.
* Integration AC — synthetic drift test: spin up the gate against a
  SQLite DB whose ``alembic_version`` row deliberately disagrees with the
  script-tree heads; assert the subprocess exits 1 within 3 s with a
  structured diagnostic on stderr.

Pure-unit tests of ``_classify_drift`` / ``_redact`` live alongside in
the same file so a refactor that weakens the classifier shows up red.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.alembic_drift_gate import (
    _classify_drift,
    _redact,
    check_drift,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "backend" / "alembic"


@pytest.fixture(scope="module")
def real_script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_DIR))
    return ScriptDirectory.from_config(cfg)


# ── _classify_drift unit table ───────────────────────────────────────


@pytest.mark.parametrize(
    "image, db, expected",
    [
        ([], [], "match"),
        (["a"], ["a"], "match"),
        (["a", "b"], ["b", "a"], "match"),
        # image strict-superset of db ⇒ DB needs upgrade
        (["a", "b"], ["a"], "image_ahead"),
        # db strict-superset of image ⇒ image is stale
        (["a"], ["a", "b"], "db_ahead"),
        # neither subset ⇒ divergent
        (["a", "b"], ["c", "d"], "divergent"),
        (["a", "b"], ["a", "c"], "divergent"),
    ],
)
def test_classify_drift(image, db, expected, real_script):
    # Synthetic letters resolve to ResolutionError against the real DAG;
    # the classifier's identity short-circuit + exception handling preserves
    # the original set-semantic test contract while the production path uses
    # ancestry (see test_classify_drift_forward_linear_mixed_prefix).
    assert _classify_drift(image, db, real_script) == expected


def test_classify_drift_forward_linear_mixed_prefix(real_script):
    """OP-1446 regression: forward linear drift across a numeric → m_* merge
    → numeric chain must classify as ``image_ahead`` (DB needs upgrade), not
    ``divergent``.

    Reproduces the v0.5.0-rc2 prod scenario: image baked at numeric head
    ``0242``, DB still at the prior merge node ``m_2026_05_16_3head`` which
    is an ancestor of ``0242`` per the alembic DAG. Set-equality alone
    flagged this as divergent and halted the deploy.
    """
    heads = real_script.get_heads()
    assert "0242" in heads, (
        "fixture assumes 0242 is a current image head; if migrations "
        "advanced, pick a descendant of m_2026_05_16_3head"
    )
    walk = [
        s.revision
        for s in real_script.iterate_revisions("0242", "m_2026_05_16_3head")
    ]
    assert walk, (
        "fixture assumes m_2026_05_16_3head is an ancestor of 0242 in the "
        "alembic DAG (walk should not be empty)"
    )

    assert (
        _classify_drift(["0242"], ["m_2026_05_16_3head"], real_script)
        == "image_ahead"
    )
    # Symmetry: the reverse direction must classify as db_ahead, not
    # divergent (image is stale relative to a DB that ran ahead).
    assert (
        _classify_drift(["m_2026_05_16_3head"], ["0242"], real_script)
        == "db_ahead"
    )


def test_redact_strips_password():
    assert (
        _redact("postgresql://app:s3cret@db.example.com:5432/omni")
        == "postgresql://app:***@db.example.com:5432/omni"
    )


def test_redact_passes_through_when_no_password():
    assert _redact("sqlite:///tmp/x.db") == "sqlite:///tmp/x.db"
    assert (
        _redact("postgresql://app@db.example.com/omni")
        == "postgresql://app@db.example.com/omni"
    )


# ── check_drift in-process against SQLite ────────────────────────────


def _make_sqlite_with_version(path: Path, version: str | None) -> None:
    """Write a SQLite file that simulates a DB at the given alembic head.

    ``version=None`` writes the file with no ``alembic_version`` table at
    all — the "fresh install" branch.
    """
    conn = sqlite3.connect(path)
    try:
        if version is not None:
            conn.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            conn.execute(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                (version,),
            )
            conn.commit()
        else:
            # Touch the file so it exists but has no schema.
            conn.execute("CREATE TABLE _placeholder (x INTEGER)")
            conn.commit()
    finally:
        conn.close()


def test_check_drift_db_missing_alembic_version_returns_zero(tmp_path):
    db = tmp_path / "fresh.db"
    _make_sqlite_with_version(db, version=None)
    code, payload = check_drift(f"sqlite:///{db}", SCRIPT_DIR)
    assert code == 0
    assert payload["reason"] == "db_has_no_alembic_version"


def test_check_drift_sqlite_file_absent_returns_zero(tmp_path):
    db = tmp_path / "never_created.db"
    assert not db.exists()
    code, payload = check_drift(f"sqlite:///{db}", SCRIPT_DIR)
    assert code == 0
    assert payload["reason"] == "sqlite_db_file_absent"


def test_check_drift_mismatch_returns_one_with_diagnostic(tmp_path):
    db = tmp_path / "drifted.db"
    # Deliberately pin a revision that does not match any image head.
    _make_sqlite_with_version(db, version="0200")
    code, payload = check_drift(f"sqlite:///{db}", SCRIPT_DIR)
    assert code == 1
    assert payload["reason"] == "alembic_head_drift"
    assert payload["db_head"] == ["0200"]
    assert payload["image_head"], "image_head must be non-empty"
    assert payload["drift_direction"] in {"image_ahead", "db_ahead", "divergent"}
    assert payload["remediation_hint"]


def test_check_drift_unreachable_db_returns_two():
    # Point at a host that cannot resolve so the engine raises promptly.
    bogus = (
        "postgresql+psycopg2://app:nope@127.0.0.1:1/"
        "definitely_no_such_db"
    )
    code, payload = check_drift(bogus, SCRIPT_DIR)
    assert code == 2
    assert payload["reason"] in {"db_unreachable", "db_check_failed"}
    # Password must be redacted in the diagnostic.
    assert "nope" not in json.dumps(payload)


# ── Synthetic Integration AC: subprocess exits 1 within 3 s ──────────


def test_synthetic_divergent_drift_subprocess_fails_fast(tmp_path):
    """Integration AC: image-at-real-head against DB-at-unknown-rev →
    divergent → exit 1 < 3 s with structured diagnostic on stderr.

    Uses a synthetic revision string the alembic DAG has never seen so
    the classifier falls through to ``divergent``. ``image_ahead`` is
    the rolling-deploy case which since OP-1448 exits 0 (covered by
    ``test_image_ahead_subprocess_defers_to_startup_hook``).
    """
    db = tmp_path / "synthetic_drift.db"
    _make_sqlite_with_version(db, version="deadbeef_not_a_real_rev")

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "OMNISIGHT_DATABASE_PATH": str(db),
    }
    for k in (
        "SQLALCHEMY_URL",
        "OMNISIGHT_DATABASE_URL",
        "DATABASE_URL",
        "OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE",
    ):
        env.pop(k, None)

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "backend.alembic_drift_gate"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - t0

    assert proc.returncode == 1, (
        f"expected drift exit code 1, got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert elapsed < 3.0, (
        f"drift gate took {elapsed:.2f}s, AC budget is < 3s"
    )

    # The gate emits a single JSON record; alembic may add UserWarning
    # lines from the versions tree before it, so take the last { -prefixed
    # line.
    json_lines = [
        line for line in proc.stderr.strip().splitlines() if line.startswith("{")
    ]
    assert json_lines, f"no JSON diagnostic on stderr: {proc.stderr!r}"
    payload = json.loads(json_lines[-1])
    assert payload["event"] == "alembic_drift_gate"
    assert payload["reason"] == "alembic_head_drift"
    assert payload["db_head"] == ["deadbeef_not_a_real_rev"]
    assert payload["image_head"], "image_head must be reported"
    assert payload["drift_direction"] == "divergent"


def test_image_ahead_subprocess_defers_to_startup_hook(tmp_path):
    """OP-1448 AC: image-ahead drift exits 0 (was 1) so the uvicorn
    lifespan can reach OP-1166 ``maybe_run_startup_upgrade``.

    Pins the DB to an old numeric ancestor (``0200``) of the current
    image head; the classifier returns ``image_ahead`` and ``main()``
    demotes the diagnostic to a warning + exits 0 instead of fail-
    closing the container. Before the fix this exited 1 and put the
    backend into CrashLoopBackOff (v0.5.0-rc2-hotfix1 incident,
    2026-05-18).
    """
    db = tmp_path / "image_ahead.db"
    _make_sqlite_with_version(db, version="0200")

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "OMNISIGHT_DATABASE_PATH": str(db),
    }
    for k in (
        "SQLALCHEMY_URL",
        "OMNISIGHT_DATABASE_URL",
        "DATABASE_URL",
        "OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE",
    ):
        env.pop(k, None)

    proc = subprocess.run(
        [sys.executable, "-m", "backend.alembic_drift_gate"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, (
        f"expected image_ahead to exit 0 (defer to startup hook), got "
        f"{proc.returncode}; stderr={proc.stderr!r}"
    )

    json_lines = [
        line for line in proc.stderr.strip().splitlines() if line.startswith("{")
    ]
    assert json_lines, f"no JSON diagnostic on stderr: {proc.stderr!r}"
    payload = json.loads(json_lines[-1])
    assert payload["event"] == "alembic_drift_gate"
    assert payload["level"] == "warning"
    assert payload["reason"] == "image_ahead_deferring_to_startup_hook"
    assert payload["drift_direction"] == "image_ahead"
    assert payload["db_head"] == ["0200"]
    assert payload["image_head"], "image_head must be reported"
    assert "maybe_run_startup_upgrade" in payload["remediation_hint"]


def test_skip_env_short_circuits_to_zero(tmp_path):
    """Emergency escape hatch: OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE=1 → 0 even
    when drift would otherwise be reported.
    """
    db = tmp_path / "would_drift.db"
    _make_sqlite_with_version(db, version="0200")

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "OMNISIGHT_DATABASE_PATH": str(db),
        "OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE": "1",
    }
    for k in ("SQLALCHEMY_URL", "OMNISIGHT_DATABASE_URL", "DATABASE_URL"):
        env.pop(k, None)

    proc = subprocess.run(
        [sys.executable, "-m", "backend.alembic_drift_gate"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    json_lines = [
        line for line in proc.stderr.strip().splitlines() if line.startswith("{")
    ]
    payload = json.loads(json_lines[-1])
    assert payload["reason"] == "skip_gate_env_set"


def test_no_db_configured_exits_zero():
    """SQLite dev mode with nothing configured: gate stays open."""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
    }
    for k in (
        "SQLALCHEMY_URL",
        "OMNISIGHT_DATABASE_URL",
        "DATABASE_URL",
        "OMNISIGHT_DATABASE_PATH",
        "OMNISIGHT_SKIP_ALEMBIC_DRIFT_GATE",
    ):
        env.pop(k, None)

    proc = subprocess.run(
        [sys.executable, "-m", "backend.alembic_drift_gate"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    json_lines = [
        line for line in proc.stderr.strip().splitlines() if line.startswith("{")
    ]
    payload = json.loads(json_lines[-1])
    assert payload["reason"] == "no_db_configured_skip"
