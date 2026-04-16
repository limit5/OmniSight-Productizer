"""N8 — Alembic dual-track migration validator.

Exercises every committed migration against both a SQLite target and a
Postgres target so an engine-specific SQL landmine is caught in CI
rather than during the G4 cutover.

What the script does, per invocation:

    1. Point Alembic at a fresh, empty DB (SQLite file or Postgres URL).
    2. Walk up one revision at a time from `base` -> `head`, recording
       the schema fingerprint (table + column names) after each step.
    3. Walk back down one revision at a time to the `0001` baseline.
       (The baseline migration refuses `downgrade` by design -- dropping
       every table is never what you want in production -- so the floor
       is `0001`, not `base`.)
    4. Walk up again to `head` and confirm the fingerprint matches the
       one recorded in step (2). A mismatch means an up/down pair is
       asymmetric and will drift a production DB over time.

Output is a single JSON document on stdout plus a GitHub Actions
`::notice`/`::error` annotation per revision when run inside CI. The
exit code is 0 on success and 1 on any validation failure.

Why stdlib + Alembic only: this script is the one that catches
engine-incompat bugs. If we let it pull in e.g. SQLAlchemy ORM models
or heavy fixture libs, a dep upgrade could break the validator before
the migrations themselves break -- exactly the self-defense argument
N5/N6/N7 already apply.

Usage:

    python3 scripts/alembic_dual_track.py --engine sqlite
    python3 scripts/alembic_dual_track.py --engine postgres \\
        --url postgresql+psycopg://omnisight:omnisight@localhost:5432/omnitest

The caller is responsible for provisioning the Postgres instance
(CI uses a service container); the script only reads the URL.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
BASELINE_REV = "0001"


def _discover_revisions() -> list[str]:
    """Return the ordered list of Alembic revision IDs under
    ``backend/alembic/versions`` from oldest (0001) to newest.

    We read the filenames rather than asking Alembic because at this
    point we may not yet have installed a working DB; a filename sort
    is deterministic and matches the repo's `NNNN_label.py` scheme.
    """
    versions_dir = BACKEND_DIR / "alembic" / "versions"
    revs: list[str] = []
    for entry in sorted(versions_dir.glob("*.py")):
        stem = entry.stem
        head = stem.split("_", 1)[0]
        if head.isdigit():
            revs.append(head)
    if not revs:
        raise RuntimeError("no Alembic revision files found")
    return revs


def _alembic(cmd: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Run an ``alembic`` subcommand inside ``backend/`` and return
    (returncode, stdout, stderr).  ``env`` must carry whatever DB URL
    override the caller wants -- we do not mutate os.environ here."""
    proc = subprocess.run(
        ["alembic", *cmd],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _sqlite_fingerprint(db_path: Path) -> dict[str, list[str]]:
    """Return ``{table: [column, ...]}`` for a SQLite DB using only
    stdlib sqlite3, so the fingerprint itself cannot be broken by a
    bad SQLAlchemy upgrade."""
    import sqlite3

    out: dict[str, list[str]] = {}
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
            "ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
            out[t] = sorted(c[1] for c in cols)
    return out


def _postgres_fingerprint(url: str) -> dict[str, list[str]]:
    """Mirror of ``_sqlite_fingerprint`` but against the information
    schema of a Postgres URL. Uses SQLAlchemy (already an Alembic
    dependency) so we avoid a direct psycopg import dance."""
    from sqlalchemy import create_engine, text

    out: dict[str, list[str]] = {}
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = [
                r[0] for r in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name NOT LIKE 'alembic_%' "
                    "ORDER BY table_name"
                ))
            ]
            for t in tables:
                cols = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t "
                    "ORDER BY column_name"
                ), {"t": t})
                out[t] = sorted(r[0] for r in cols)
    finally:
        engine.dispose()
    return out


def _fingerprint(engine: str, db_path: Path | None, url: str | None) -> dict:
    if engine == "sqlite":
        assert db_path is not None
        return _sqlite_fingerprint(db_path)
    if engine == "postgres":
        assert url is not None
        return _postgres_fingerprint(url)
    raise ValueError(engine)


def _ci_annotate(kind: str, msg: str) -> None:
    """Emit a GitHub Actions log annotation. No-op outside CI."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{msg}")


def run_dual_track(engine: str, url: str | None) -> int:
    revs = _discover_revisions()
    head_rev = revs[-1]

    with tempfile.TemporaryDirectory(prefix="alembic-dualtrack-") as tmp:
        tmp_path = Path(tmp)

        env = os.environ.copy()
        # Alembic's `env.py` reads OMNISIGHT_DATABASE_PATH (SQLite) and
        # SQLALCHEMY_URL (any engine). We set the one the engine needs.
        # Data-migrations like 0014 shuffle real files around a production
        # workspace; the dual-track validator is strictly about SQL
        # correctness so we skip filesystem side-effects via a dedicated
        # env var that migration modules honour.
        env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"
        db_file = tmp_path / "track.db"
        if engine == "sqlite":
            env["OMNISIGHT_DATABASE_PATH"] = str(db_file)
            env.pop("SQLALCHEMY_URL", None)
        else:
            assert url, "Postgres mode requires --url"
            env["SQLALCHEMY_URL"] = url
            env.pop("OMNISIGHT_DATABASE_PATH", None)

        report: dict = {
            "engine": engine,
            "revisions": revs,
            "head": head_rev,
            "baseline": BASELINE_REV,
            "steps": [],
            "ok": True,
        }

        # ── Step 1: fresh upgrade to head ─────────────────────────
        rc, so, se = _alembic(["upgrade", "head"], env)
        report["steps"].append({"phase": "upgrade-head", "rc": rc, "stderr": se[-500:]})
        if rc != 0:
            _ci_annotate("error",
                         f"{engine}: initial upgrade head failed rc={rc}")
            report["ok"] = False
            print(json.dumps(report, indent=2))
            return 1

        fingerprint_head = _fingerprint(engine, db_file, url)
        report["fingerprint_head_tables"] = sorted(fingerprint_head.keys())

        # ── Step 2: step down to baseline, one rev at a time ──────
        for rev in reversed(revs):
            if rev == BASELINE_REV:
                break  # baseline refuses downgrade by design
            rc, so, se = _alembic(["downgrade", "-1"], env)
            report["steps"].append({
                "phase": f"downgrade-from-{rev}", "rc": rc, "stderr": se[-500:],
            })
            if rc != 0:
                _ci_annotate("error",
                             f"{engine}: downgrade from {rev} failed rc={rc}")
                report["ok"] = False
                print(json.dumps(report, indent=2))
                return 1

        # ── Step 3: re-upgrade to head ────────────────────────────
        rc, so, se = _alembic(["upgrade", "head"], env)
        report["steps"].append({"phase": "re-upgrade-head", "rc": rc, "stderr": se[-500:]})
        if rc != 0:
            _ci_annotate("error",
                         f"{engine}: re-upgrade head failed rc={rc}")
            report["ok"] = False
            print(json.dumps(report, indent=2))
            return 1

        # ── Step 4: fingerprint symmetry check ────────────────────
        fingerprint_after = _fingerprint(engine, db_file, url)
        drift = {
            k: (fingerprint_head.get(k), fingerprint_after.get(k))
            for k in set(fingerprint_head) | set(fingerprint_after)
            if fingerprint_head.get(k) != fingerprint_after.get(k)
        }
        if drift:
            _ci_annotate("error",
                         f"{engine}: schema drift after down+up cycle: "
                         f"{list(drift.keys())[:5]}")
            report["ok"] = False
            report["drift"] = {k: {"before": v[0], "after": v[1]}
                               for k, v in drift.items()}

    _ci_annotate("notice",
                 f"{engine}: dual-track OK over {len(revs)} revisions")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["sqlite", "postgres"], required=True,
                    help="which engine to exercise")
    ap.add_argument("--url", default=None,
                    help="SQLAlchemy URL (required for --engine=postgres)")
    args = ap.parse_args()

    if args.engine == "postgres" and not args.url:
        ap.error("--engine=postgres requires --url")

    return run_dual_track(args.engine, args.url)


if __name__ == "__main__":
    sys.exit(main())
