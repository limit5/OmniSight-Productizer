#!/usr/bin/env python3
"""P5 host-side executor — the ONLY component that runs a real
`systemctl --user restart` for an operator-approved Sora proposal.

WHY IT EXISTS: the backend runs in a container with NO systemd (verified
2026-07-07: no systemctl, no DBus, no socket mounts), so it CANNOT restart the
host's units. The approve API therefore only marks the proposed_actions row
'executing' and DEFERS to this host agent, which runs on the host where systemd
+ our units live and writes the terminal result back.

SAFETY — THIS HOST ALSO RUNS OTHER, UNRELATED SERVICES (operator note
2026-07-07). Every guard below exists to guarantee this agent can NEVER touch
anything that is not one of OUR explicitly-named, non-critical units:

  1. HARDCODED allowlist (`_ALLOWED_UNITS`) — the ONLY units this agent may ever
     restart. Deliberately NOT read from the environment: on a shared host an
     env-widened allowlist must not be able to reach a non-omnisight service.
  2. A second PREFIX guard (`_UNIT_PREFIX`) — the unit must ALSO match
     `^(omnisight-|pipeline-coordinator)`, so even editing the hardcoded set can't
     accidentally add a foreign unit without also matching our naming.
  3. DRY-RUN BY DEFAULT — prints what it WOULD restart; needs `--execute` to act.
  4. It only ever issues `systemctl --user restart -- <unit>` for a matched unit.
     No stop/disable/mask, no other verbs, no other targets, no shell.
  5. Idempotent under concurrency: it ATOMICALLY CLAIMS each row
     (executing→host_running via a CAS `UPDATE ... WHERE status='executing'
     RETURNING id`) BEFORE the restart — only the claim winner restarts, and the
     terminal 'executed'/'failed' write then guards `AND status='host_running'`,
     so each row is restarted at most once even if two passes overlap. A refused
     (off-allowlist) row is terminated straight from 'executing' (no restart).

A unit that a proposal names but that fails guards 1/2 is REFUSED (row → 'failed'
with a clear reason) — it is never restarted.

DB access is via `docker exec <pg> psql` (postgres is a container; the host
reaches it through docker, not a host-exposed port).

Usage:
  python3 scripts/p5_host_executor.py                 # one dry-run pass
  python3 scripts/p5_host_executor.py --execute       # one real pass
  python3 scripts/p5_host_executor.py --execute --loop 15   # poll every 15s
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

# The ONLY units this host agent may EVER restart. HARDCODED on purpose (see (1)).
_ALLOWED_UNITS = frozenset({
    "omnisight-slo-monitor.service",   # monitoring — restart is fully recoverable
    "pipeline-coordinator.service",    # has its own watchdog; restart is recoverable
})
# Second, independent gate (see (2)): the unit MUST also match our naming.
_UNIT_PREFIX = re.compile(r"^(omnisight-|pipeline-coordinator)")

PG_CONTAINER = "omnisight-pg-primary"
PG_USER = "omnisight"
PG_DB = "omnisight"


def _psql(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-tAqc", sql],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {(r.stderr or '').strip()[:200]}")
    return r.stdout


def _sql_lit(s) -> str:
    """Escape a value for a single-quoted SQL literal: TRUNCATE first, THEN double
    the quotes (audit r4 — truncating AFTER escaping could cut an escaped '' pair
    in half and corrupt the statement)."""
    return (str(s) if s is not None else "")[:1000].replace("'", "''")


def _unit_is_allowed(unit) -> bool:
    return isinstance(unit, str) and unit in _ALLOWED_UNITS and bool(_UNIT_PREFIX.match(unit))


def _restart(unit: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["systemctl", "--user", "restart", "--", unit],
        capture_output=True, text=True, timeout=60,
    )
    ok = r.returncode == 0
    return ok, f"rc={r.returncode}" + ("" if ok else f" stderr={(r.stderr or '').strip()[:180]}")


def _fetch_executing() -> list[dict]:
    """Fetch pending 'executing' restart rows as structured JSON (audit r4 — NOT
    hand-delimited: a malformed params with an embedded tab/newline must not be
    able to manufacture a second logical row)."""
    raw = _psql(
        "SELECT COALESCE(json_agg(json_build_object('id', id, 'params', params) "
        "ORDER BY proposed_at), '[]'::json)::text FROM proposed_actions "
        "WHERE status='executing' AND action_kind='restart'"
    ).strip()
    try:
        rows = json.loads(raw or "[]")
        return rows if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


def _claim(action_id: str) -> bool:
    """Atomically transition executing→host_running (audit r4). Only the caller
    that WINS this CAS may restart — so overlapping passes can't double-restart."""
    out = _psql(
        f"UPDATE proposed_actions SET status='host_running', executed_at={time.time()} "
        f"WHERE id='{_sql_lit(action_id)}' AND status='executing' RETURNING id"
    ).strip()
    return bool(out)


def _finish(action_id: str, status: str, detail: str, *, from_status: str) -> None:
    _psql(
        f"UPDATE proposed_actions SET status='{_sql_lit(status)}', "
        f"result='{_sql_lit(detail)}', executed_at={time.time()} "
        f"WHERE id='{_sql_lit(action_id)}' AND status='{_sql_lit(from_status)}'"
    )


_STRANDED_TTL_S = 300  # a host_running row older than this = a crashed/lost pass


def _reap_stranded() -> None:
    """Audit r5 AB-05: a host crash between _claim (→host_running) and _finish
    leaves the row 'host_running' forever, invisible to _fetch_executing. Reap
    rows older than the TTL to 'failed (stranded)' so they are surfaced, not
    silently lost. We do NOT auto-retry (the restart may already have run) — the
    operator verifies the unit and re-proposes if needed."""
    _psql(
        "UPDATE proposed_actions SET status='failed', result='host executor: "
        f"stranded in host_running > {_STRANDED_TTL_S}s (host crash?); verify the "
        "unit is healthy and re-propose if the restart did not complete', "
        f"executed_at={time.time()} WHERE status='host_running' "
        f"AND action_kind='restart' AND executed_at < {time.time() - _STRANDED_TTL_S}"
    )


def process(execute: bool) -> int:
    if execute:
        _reap_stranded()   # recover crashed claims before taking new work
    rows = _fetch_executing()
    if not rows:
        print("[p5-host] no 'executing' restart proposals.")
        return 0
    handled = 0
    for row in rows:
        action_id = str(row.get("id") or "").strip()
        params_raw = row.get("params") or "{}"
        try:
            parsed = json.loads(params_raw) if isinstance(params_raw, str) else params_raw
            svc = (parsed or {}).get("service") if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            svc = None
        # Guards 1 + 2: refuse anything not in the hardcoded allowlist + prefix.
        if not _unit_is_allowed(svc):
            msg = (f"host executor REFUSED unit {svc!r} — not in the hardcoded "
                   f"allowlist {sorted(_ALLOWED_UNITS)} (+ prefix guard). Not restarted.")
            if execute:
                _finish(action_id, "failed", msg, from_status="executing")
                print(f"[p5-host] {action_id}: {msg}")
                handled += 1
            else:  # dry-run: pure observation, no DB write
                print(f"[p5-host] {action_id}: [DRY-RUN] would REFUSE {svc!r} (off-allowlist)")
            continue
        if not execute:  # dry-run: pure observation, no restart, no DB write
            print(f"[p5-host] {action_id}: [DRY-RUN] would `systemctl --user restart {svc}` "
                  f"(pass --execute to actually restart)")
            continue
        # Atomic claim BEFORE the side effect — only the winner restarts.
        if not _claim(action_id):
            print(f"[p5-host] {action_id}: already claimed by another pass; skipping.")
            continue
        ok, detail = _restart(svc)
        status = "executed" if ok else "failed"
        msg = f"host executor ran `systemctl --user restart {svc}` -> {status} ({detail})"
        print(f"[p5-host] {action_id}: {msg}")
        _finish(action_id, status, msg, from_status="host_running")
        handled += 1
    return handled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="actually restart (DEFAULT: dry-run — only prints what it would do)")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="poll every N seconds (0 = a single pass, the default)")
    args = ap.parse_args()
    if args.loop:
        print(f"[p5-host] polling every {args.loop}s (execute={args.execute})")
        while True:
            try:
                process(args.execute)
            except Exception as exc:  # noqa: BLE001
                print(f"[p5-host] pass error: {exc}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        process(args.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
