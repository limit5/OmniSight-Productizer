"""G4 #1 — Static scan for SQLite-isms in Alembic migrations.

The scanner complements the dual-track runtime validator
(``scripts/alembic_dual_track.py``) by catching SQLite-isms *before* a
migration ever hits a Postgres instance. CI wires both:

    # Static pre-flight (fast, no DB required)
    python3 scripts/scan_sqlite_isms.py

    # Runtime dual-track (requires Postgres service container)
    python3 scripts/alembic_dual_track.py --engine postgres --url ...

Any SQLite-ism found is reported on stdout in a grep-friendly format and
the process exits non-zero. The ``--ism`` flag narrows to a single
pattern; ``--allow-unhandled`` lists which isms are *known* but rely on
the Alembic shim rather than migration-file changes, so reviewers can
decide at a glance whether a new addition is safe.

Patterns are sourced from :mod:`backend.alembic_pg_compat` so the
scanner and the runtime translator stay in lockstep — adding a new
pattern in the compat module automatically extends the scanner.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from alembic_pg_compat import ISM_PATTERNS, scan_sqlite_isms  # noqa: E402


MIGRATIONS_DIR = BACKEND_DIR / "alembic" / "versions"

# These isms ARE present in migration source but are handled at runtime
# by the Postgres shim (``backend/alembic_pg_compat.py``). They count as
# acceptable under the current design — the shim translates them before
# they hit the DB. New migrations SHOULD still avoid them so the shim
# can one day be retired, but the scanner does not block on them alone.
SHIM_HANDLED = {
    "datetime_now",
    "strftime_epoch",
    "AUTOINCREMENT",
    "INSERT_OR_IGNORE",
    "INSERT_OR_REPLACE",
    "PRAGMA_TABLE_INFO",
}

# DYNAMIC_TYPE pattern has a high false-positive rate (matches any
# single-word line inside parentheses). Off by default in the CLI, but
# still exposed via ``--ism DYNAMIC_TYPE`` for deep audits.
NOISY_BY_DEFAULT = {"DYNAMIC_TYPE"}


def _scan_file(path: Path, *, ignore: set[str]) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    hits = scan_sqlite_isms(source, ignore=ignore)
    return [
        {
            "file": str(path.relative_to(REPO_ROOT)),
            "ism": h.ism,
            "lineno": h.lineno,
            "snippet": h.snippet,
        }
        for h in hits
    ]


def run(
    *,
    target_ism: str | None,
    fail_on_shim_handled: bool,
    include_dynamic_type: bool,
    as_json: bool,
) -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"error: migrations dir not found: {MIGRATIONS_DIR}", file=sys.stderr)
        return 2

    ignore = set() if include_dynamic_type else set(NOISY_BY_DEFAULT)
    if target_ism and target_ism.upper() not in {k.upper() for k in ISM_PATTERNS}:
        print(f"error: unknown ism {target_ism!r}. "
              f"Known: {sorted(ISM_PATTERNS)}", file=sys.stderr)
        return 2

    all_hits: list[dict] = []
    for mig in sorted(MIGRATIONS_DIR.glob("*.py")):
        for hit in _scan_file(mig, ignore=ignore):
            if target_ism and hit["ism"].upper() != target_ism.upper():
                continue
            all_hits.append(hit)

    if as_json:
        print(json.dumps(
            {
                "hits": all_hits,
                "shim_handled": sorted(SHIM_HANDLED),
                "noisy_by_default": sorted(NOISY_BY_DEFAULT),
                "total": len(all_hits),
            },
            indent=2,
        ))
    else:
        for h in all_hits:
            badge = "[shim]" if h["ism"] in SHIM_HANDLED else "[FAIL]"
            print(f"{badge} {h['file']}:{h['lineno']}:{h['ism']} — {h['snippet']}")

    unhandled = [h for h in all_hits if h["ism"] not in SHIM_HANDLED]
    if unhandled:
        return 1
    if fail_on_shim_handled and all_hits:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--ism",
        help="scan for only this ism (e.g. AUTOINCREMENT, datetime_now)",
    )
    ap.add_argument(
        "--fail-on-shim-handled",
        action="store_true",
        help="exit 1 even for isms the runtime shim can translate",
    )
    ap.add_argument(
        "--include-dynamic-type",
        action="store_true",
        help="include the high-false-positive DYNAMIC_TYPE pattern",
    )
    ap.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="machine-readable output",
    )
    args = ap.parse_args(argv)

    return run(
        target_ism=args.ism,
        fail_on_shim_handled=args.fail_on_shim_handled,
        include_dynamic_type=args.include_dynamic_type,
        as_json=args.as_json,
    )


if __name__ == "__main__":
    sys.exit(main())
