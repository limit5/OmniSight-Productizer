#!/usr/bin/env python3
"""omnisight-runner-rescue — operator CLI for the OP-1106 runner_claims table.

Per Sprint Atlas Family ⑩ §4 ticket row 15 (OP-1118 / v2-Ⅹ-RescueCLI).

Three subcommands:

* ``dump``    — read-only inspect; lists every active claim row. Useful
                when an operator is investigating a stuck pickup and needs
                to know who currently holds what.
* ``release`` — force-release one specific lease, bypassing the fencing-
                token check. Used when the original holder (runner
                process) has died, hung, or otherwise can't release the
                lease through the normal path.
* ``reset``   — bulk-release every active claim whose heartbeat is older
                than ``--max-age-seconds``. Equivalent to running the TTL
                sweeper (OP-1109) manually with a wider age window. Useful
                after a host-wide incident where several runners died.

Operator audit (ADR-0033)
-------------------------

Every invocation (including read-only ``dump``) writes one row to the
``runner_audit_events`` table (alembic 0237). ``release`` and ``reset``
require ``--operator <fingerprint>`` so the override has an attributable
name. The fingerprint is opaque to the CLI; ``OMNISIGHT_L2_OPERATORS``
env var, if set, gates which fingerprints are accepted (comma-separated
allow-list).

Exit codes
----------

* ``0`` — success
* ``1`` — runtime / DB error
* ``2`` — argument / authorization error
* ``3`` — no rows matched (nothing to release)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import runner_coordination as rc  # noqa: E402


# ── Operator allow-list (ADR-0033 L2 fingerprint gate) ────────────────


_OPERATOR_ALLOWLIST_ENV = "OMNISIGHT_L2_OPERATORS"


def _operator_allowed(fingerprint: str) -> bool:
    """Return True if ``fingerprint`` is permitted to run write operations.

    Allow-list is the comma-separated value of OMNISIGHT_L2_OPERATORS.
    If the env var is unset or empty, the CLI accepts any non-empty
    fingerprint (dev / single-operator deployment). Operators running
    multi-tenant or production deployments should set the env var to
    restrict it.
    """
    if not fingerprint:
        return False
    allowlist_raw = os.environ.get(_OPERATOR_ALLOWLIST_ENV, "").strip()
    if not allowlist_raw:
        return True
    allowed = {item.strip() for item in allowlist_raw.split(",") if item.strip()}
    return fingerprint in allowed


# ── Output helpers ────────────────────────────────────────────────────


def _lease_to_dict(lease: rc.ClaimLease) -> dict:
    return {
        "lease_id": lease.lease_id,
        "ticket_key": lease.ticket_key,
        "resource_key": lease.resource_key,
        "owner_agent_class": lease.owner_agent_class,
        "owner_instance_id": lease.owner_instance_id,
        "fencing_token": lease.fencing_token,
        "state": lease.state,
        "phase": lease.phase,
        "heartbeat_at": lease.heartbeat_at,
        "acquired_at": lease.acquired_at,
        "external_refs": lease.external_refs,
    }


def _print_table(leases: list[rc.ClaimLease], stream=None) -> None:
    """Render leases as a fixed-width table. Columns chosen for ops
    triage: who, what, when, on what resource.

    ``stream`` defaults to ``None`` (resolved to live ``sys.stdout`` at
    call time, not at import time) so pytest's ``capsys`` can intercept
    output through its monkey-patched stdout.
    """
    if stream is None:
        stream = sys.stdout
    if not leases:
        print("(no active claims)", file=stream)
        return
    rows = [
        (
            l.ticket_key,
            l.owner_agent_class,
            l.owner_instance_id,
            l.resource_key,
            l.phase,
            l.heartbeat_at[:19],  # trim sub-second + tz for readability
            l.lease_id,
        )
        for l in leases
    ]
    header = ("TICKET", "AGENT_CLASS", "INSTANCE", "RESOURCE",
              "PHASE", "HEARTBEAT_AT", "LEASE_ID")
    widths = [max(len(h), max(len(str(r[i])) for r in rows)) for i, h in enumerate(header)]
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*header), file=stream)
    print(fmt.format(*("-" * w for w in widths)), file=stream)
    for r in rows:
        print(fmt.format(*r), file=stream)


# ── Subcommands ───────────────────────────────────────────────────────


def cmd_dump(args: argparse.Namespace) -> int:
    """Read-only: enumerate active claim rows."""
    try:
        leases = rc.find_active_holders()
    except Exception as exc:
        print(f"ERROR: failed to read runner_claims: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([_lease_to_dict(l) for l in leases], indent=2))
    else:
        _print_table(leases)

    # Even read-only ops are audit-logged so the trail is complete.
    try:
        rc.record_audit_event(
            action="rescue.dump",
            operator_fingerprint=args.operator or None,
            details={"count": len(leases), "format": "json" if args.json else "table"},
        )
    except Exception as exc:  # noqa: BLE001
        # Audit insert failures should NOT mask the dump output the
        # operator requested. Warn to stderr and continue.
        print(f"WARN: audit-log write failed: {exc}", file=sys.stderr)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """Force-release a specific lease by lease_id."""
    if not _operator_allowed(args.operator):
        print(
            f"ERROR: operator fingerprint {args.operator!r} not in "
            f"{_OPERATOR_ALLOWLIST_ENV}; refusing override.",
            file=sys.stderr,
        )
        return 2

    snapshot: Optional[rc.ClaimLease]
    try:
        snapshot = rc.force_release_claim(
            lease_id=args.lease_id,
            release_reason=args.reason,
            operator_fingerprint=args.operator,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: force_release_claim failed: {exc}", file=sys.stderr)
        return 1

    if snapshot is None:
        print(f"No active lease for lease_id {args.lease_id!r} (already "
              f"released or never existed)", file=sys.stderr)
        # Still record the attempt for audit
        try:
            rc.record_audit_event(
                action="rescue.release_no_match",
                operator_fingerprint=args.operator,
                target_lease_id=args.lease_id,
                details={"reason": args.reason},
            )
        except Exception:
            pass
        return 3

    print(f"Released lease {snapshot.lease_id} on resource "
          f"{snapshot.resource_key!r} held by ticket {snapshot.ticket_key} "
          f"(reason: {args.reason})")

    try:
        rc.record_audit_event(
            action="rescue.release",
            operator_fingerprint=args.operator,
            target_lease_id=snapshot.lease_id,
            target_ticket_key=snapshot.ticket_key,
            details={
                "reason": args.reason,
                "snapshot": _lease_to_dict(snapshot),
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: audit-log write failed: {exc}", file=sys.stderr)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Bulk-release all leases older than max-age."""
    if not _operator_allowed(args.operator):
        print(
            f"ERROR: operator fingerprint {args.operator!r} not in "
            f"{_OPERATOR_ALLOWLIST_ENV}; refusing override.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        # Read-only preview: count what WOULD be released
        try:
            leases = rc.find_active_holders()
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        # Filter by heartbeat age
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        threshold_iso = (now.timestamp() - args.max_age_seconds)
        stale = [
            l for l in leases
            if _heartbeat_age_seconds(l.heartbeat_at, now) > args.max_age_seconds
        ]
        print(f"DRY-RUN: would release {len(stale)} stale lease(s) "
              f"(max_age_seconds={args.max_age_seconds})")
        for l in stale:
            print(f"  {l.lease_id}  {l.ticket_key}  {l.resource_key}  "
                  f"heartbeat={l.heartbeat_at}")
        try:
            rc.record_audit_event(
                action="rescue.reset_dry_run",
                operator_fingerprint=args.operator,
                details={"max_age_seconds": args.max_age_seconds,
                         "would_release_count": len(stale)},
            )
        except Exception:
            pass
        return 0

    try:
        n = rc.expire_stale_active_claims(max_age_seconds=args.max_age_seconds)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: expire_stale_active_claims failed: {exc}", file=sys.stderr)
        return 1

    print(f"Released {n} stale lease(s) (max_age_seconds={args.max_age_seconds})")
    try:
        rc.record_audit_event(
            action="rescue.reset",
            operator_fingerprint=args.operator,
            details={
                "max_age_seconds": args.max_age_seconds,
                "released_count": n,
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: audit-log write failed: {exc}", file=sys.stderr)
    return 0


def _heartbeat_age_seconds(heartbeat_iso: str, now) -> float:
    """Best-effort ISO8601 → seconds-ago helper. Falls back to 0 (treat
    as fresh) on unparseable input so dry-run never spuriously
    over-releases."""
    from datetime import datetime
    try:
        # Heartbeat format from runner_coordination._now_iso():
        # "2026-05-15T01:02:03.456789Z"
        hb = datetime.fromisoformat(heartbeat_iso.replace("Z", "+00:00"))
        return (now - hb).total_seconds()
    except (ValueError, TypeError):
        return 0.0


# ── Argparse wiring ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnisight-runner-rescue",
        description="Operator CLI for the runner_claims coordination table.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    p_dump = sub.add_parser(
        "dump",
        help="Read-only: list active claim rows.",
        description="Read-only inspect of runner_claims active rows.",
    )
    p_dump.add_argument("--json", action="store_true",
                        help="Output JSON instead of text table.")
    p_dump.add_argument("--operator", default=None,
                        help="Operator fingerprint for audit log (optional "
                             "for dump; required for release/reset).")
    p_dump.set_defaults(func=cmd_dump)

    p_release = sub.add_parser(
        "release",
        help="Force-release one specific lease by lease_id.",
        description="Override-release a stuck lease that the normal "
                    "release_claim() path can't clear (e.g., dead runner).",
    )
    p_release.add_argument("lease_id",
                           help="UUID of the lease (see `dump` output).")
    p_release.add_argument("--reason", required=True,
                           help="Free-form justification (recorded in audit log).")
    p_release.add_argument("--operator", required=True,
                           help="Operator L2 fingerprint per ADR-0033.")
    p_release.set_defaults(func=cmd_release)

    p_reset = sub.add_parser(
        "reset",
        help="Bulk-release every claim whose heartbeat is older than --max-age-seconds.",
        description="Mass-rescue after a host-wide incident. Use --dry-run "
                    "first to preview impact.",
    )
    p_reset.add_argument("--max-age-seconds", type=int, default=600,
                         help="Heartbeat-age threshold in seconds "
                              "(default 600 = 10 min).")
    p_reset.add_argument("--dry-run", action="store_true",
                         help="Show what would be released without doing it.")
    p_reset.add_argument("--operator", required=True,
                         help="Operator L2 fingerprint per ADR-0033.")
    p_reset.set_defaults(func=cmd_reset)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
