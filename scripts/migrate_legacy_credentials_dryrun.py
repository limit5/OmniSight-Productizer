#!/usr/bin/env python3
"""Phase 5-11 (#multi-account-forge) — legacy credential migration
**dry-run** / staging verifier.

Read-only companion to :mod:`backend.legacy_credential_migration`.
Executes the same ``_plan_rows()`` pass that the lifespan hook runs
at boot, but never writes to the database. Useful for:

* **Pre-flight on staging** — "what WOULD the migration do if I
  rebuilt the backend image right now?". Operator runs this before
  the Phase-5-5 auto-migration for the first time so there are no
  surprises.

* **Audit trail attestation** — dump the planned row set as JSON,
  review it, attach it to the migration ticket as evidence of what
  was migrated (signing off on the pre-state).

* **Idempotency check** — if ``git_accounts`` already has rows, the
  hook would skip migration; this script reports that so operators
  don't wonder why their ``.env`` legacy knobs appear "still active"
  (they aren't — the already-migrated rows shadow them).

The script prints everything in fingerprint-masked form — the last 4
chars of each PAT only, NEVER plaintext. Same contract as the
``git_accounts`` REST API.

Usage
-----

  # Human-readable default output
  scripts/migrate_legacy_credentials_dryrun.py

  # Machine-readable JSON (for CI attestation, audit log archive, etc.)
  scripts/migrate_legacy_credentials_dryrun.py --json

  # Probe the live DB for already-migrated rows (requires
  # OMNISIGHT_DATABASE_URL / the lifespan pool):
  scripts/migrate_legacy_credentials_dryrun.py --probe-db

Exit codes
----------

* 0 — dry-run completed (whether or not any rows would migrate)
* 2 — unexpected error (bad JSON in ``.env`` legacy maps, etc.)
* 3 — ``git_accounts`` already populated AND ``--probe-db`` AND
      ``--strict-idempotency`` — signals to CI that migration has
      already happened and the expectation of a clean slate was wrong.

Security note
-------------

This script reads ``settings.*_token`` etc. from ``backend.config``.
If you run it on a host where the env vars are set, the in-process
memory will hold those plaintext secrets transiently — but nothing
is printed except last-4 fingerprints. Do NOT redirect stderr
elsewhere expecting to capture plaintext; there is none to capture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Ensure the project root is importable when the script is invoked
# directly (``scripts/migrate_legacy_credentials_dryrun.py``) rather
# than via ``python -m scripts.migrate_legacy_credentials_dryrun``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fingerprint(secret: str) -> str:
    """Mirror of :func:`backend.secret_store.fingerprint` without
    importing it here — keeps the script independent of the crypto
    stack.  ``fingerprint("")`` → ``""``,  ``fingerprint("abcd")``
    → ``"****"``, ``fingerprint("abcdefghijkl")`` → ``"…ijkl"``.
    """
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"…{secret[-4:]}"


def _summarize_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a fingerprint-only summary dict for printing + JSON
    output. Caller sees platform counts, per-row preview (id, label,
    host, default flag, fingerprinted secrets), and the total count.
    """
    per_platform: dict[str, int] = {}
    preview: list[dict[str, Any]] = []
    for r in rows:
        p = r.get("platform", "?")
        per_platform[p] = per_platform.get(p, 0) + 1
        preview.append(
            {
                "id": r.get("id"),
                "platform": p,
                "label": r.get("label", ""),
                "instance_url": r.get("instance_url", ""),
                "ssh_host": r.get("ssh_host", ""),
                "project": r.get("project", ""),
                "is_default": bool(r.get("is_default", False)),
                "enabled": bool(r.get("enabled", True)),
                "token_fingerprint": _fingerprint(r.get("token", "")),
                "ssh_key_fingerprint": _fingerprint(r.get("ssh_key", "")),
                "webhook_secret_fingerprint": _fingerprint(
                    r.get("webhook_secret", "")
                ),
                "url_patterns": list(r.get("url_patterns") or []),
                "source": r.get("source", ""),
            }
        )
    return {
        "total": len(rows),
        "per_platform": per_platform,
        "rows": preview,
    }


def plan() -> dict[str, Any]:
    """Build the planned-row set from the current ``Settings`` snapshot.

    Delegates to the production planner
    (``backend.legacy_credential_migration._plan_rows``) so the dry-run
    output matches exactly what the lifespan hook would do — no risk
    of dry-run / real-run divergence.
    """
    from backend import legacy_credential_migration as lcm

    rows = lcm._plan_rows()
    return _summarize_plan(rows)


async def _probe_db_for_existing_rows() -> dict[str, Any]:
    """Return ``{"available": bool, "count": int, "error": str|None}``.

    When ``OMNISIGHT_DATABASE_URL`` points at a real PG and the pool
    can be built, count the rows in ``git_accounts`` — if > 0, the
    lifespan hook would skip migration (the operator-managed-table
    branch). Returns ``available=False`` if PG is unreachable or if
    the project runs in SQLite dev mode (no pool).
    """
    try:
        from backend.db_pool import get_pool, init_pool
    except ImportError:
        return {
            "available": False,
            "count": 0,
            "error": "backend.db_pool unavailable — SQLite dev mode?",
        }

    try:
        pool = get_pool()
    except RuntimeError:
        # Pool not initialised — try to build a transient one from
        # the env DSN so the dry-run can probe without a running server.
        try:
            await init_pool()
            pool = get_pool()
        except Exception as exc:
            return {
                "available": False,
                "count": 0,
                "error": f"pool init failed: {type(exc).__name__}: {exc}",
            }
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM git_accounts")
        return {"available": True, "count": int(n or 0), "error": None}
    except Exception as exc:
        return {
            "available": False,
            "count": 0,
            "error": f"COUNT query failed: {type(exc).__name__}: {exc}",
        }


def _print_text(plan_d: dict[str, Any], probe: dict[str, Any] | None) -> None:
    """Operator-friendly human output."""
    print("Phase 5-5 legacy credential migration — DRY RUN")
    print("=" * 54)
    print(
        f"Candidate rows: {plan_d['total']} "
        f"(per platform: {plan_d['per_platform']})"
    )
    print()
    if plan_d["total"] == 0:
        print("No legacy credentials found in backend.config.Settings.")
        print(
            "   If you expected rows here, check your .env for "
            "OMNISIGHT_GITHUB_TOKEN / OMNISIGHT_GITLAB_TOKEN / "
            "OMNISIGHT_GERRIT_INSTANCES / OMNISIGHT_NOTIFICATION_JIRA_* ."
        )
    else:
        print("The following rows WOULD be inserted on the next backend boot")
        print("(unless git_accounts already has rows — see idempotency check):")
        print()
        for row in plan_d["rows"]:
            print(f"  • {row['id']}")
            print(
                f"      platform = {row['platform']:7s}"
                f"  label = {row['label']!r}"
            )
            if row["instance_url"]:
                print(f"      instance_url = {row['instance_url']}")
            if row["ssh_host"]:
                port = ""
                print(
                    f"      ssh_host = {row['ssh_host']}  "
                    f"project = {row['project']!r}"
                )
                del port
            print(
                f"      default = {row['is_default']}  "
                f"enabled = {row['enabled']}"
            )
            if row["token_fingerprint"]:
                print(
                    f"      token fingerprint = {row['token_fingerprint']}"
                )
            if row["ssh_key_fingerprint"]:
                print(
                    "      ssh_key fingerprint = "
                    f"{row['ssh_key_fingerprint']}"
                )
            if row["webhook_secret_fingerprint"]:
                print(
                    "      webhook_secret fingerprint = "
                    f"{row['webhook_secret_fingerprint']}"
                )
            if row["url_patterns"]:
                print(f"      url_patterns = {row['url_patterns']}")
            print(f"      source = {row['source']}")
            print()
    print()
    if probe is not None:
        print("-" * 54)
        if not probe["available"]:
            print(f"DB probe: unavailable — {probe['error']}")
        else:
            n = probe["count"]
            if n == 0:
                print(
                    "DB probe: git_accounts is EMPTY — the next backend "
                    "boot will insert the rows listed above."
                )
            else:
                print(
                    f"DB probe: git_accounts already has {n} row(s). The "
                    "migration hook will SKIP — operator-managed table."
                )
                print(
                    "   If you want to re-migrate, first DELETE the "
                    "existing rows (backup first!) — see "
                    "docs/ops/git_credentials.md."
                )


def run(
    *, emit_json: bool, probe_db: bool, strict_idempotency: bool,
) -> int:
    """Main entry — returns exit code."""
    try:
        plan_d = plan()
    except Exception as exc:
        print(
            f"ERROR: failed to build migration plan "
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 2

    probe: dict[str, Any] | None = None
    if probe_db:
        try:
            probe = asyncio.run(_probe_db_for_existing_rows())
        except Exception as exc:
            probe = {
                "available": False,
                "count": 0,
                "error": f"probe failed: {type(exc).__name__}: {exc}",
            }

    if emit_json:
        out = {"plan": plan_d, "probe": probe}
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_text(plan_d, probe)

    if (
        strict_idempotency
        and probe is not None
        and probe.get("available")
        and (probe.get("count") or 0) > 0
    ):
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Phase 5-11 dry-run for the Phase 5-5 legacy credential "
            "migration. Reports what the lifespan hook would insert "
            "WITHOUT writing anything. Fingerprint-masked output — "
            "plaintext secrets are never printed."
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON blob instead of text.",
    )
    ap.add_argument(
        "--probe-db",
        action="store_true",
        help=(
            "Also connect to the configured PG pool and count existing "
            "git_accounts rows. If > 0, the real migration would skip "
            "(idempotency branch)."
        ),
    )
    ap.add_argument(
        "--strict-idempotency",
        action="store_true",
        help=(
            "Exit 3 instead of 0 when --probe-db reports git_accounts "
            "already has rows. Useful in CI: a clean-slate expectation "
            "that's violated should fail the pipeline."
        ),
    )
    args = ap.parse_args(argv)
    return run(
        emit_json=args.json,
        probe_db=args.probe_db,
        strict_idempotency=args.strict_idempotency,
    )


if __name__ == "__main__":
    sys.exit(main())
