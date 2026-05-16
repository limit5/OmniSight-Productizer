#!/usr/bin/env bash
# [OP-964] AUDIT-16 — Audit DB connectivity smoke test for the D5
# develop -> main auto-promote.
#
# WHY THIS EXISTS
#   The D5 cron (deploy/systemd/auto-promote-develop.service ->
#   scripts/auto_promote_develop_to_main.sh) delegates the *release
#   audit row* write to backend.agents.auto_promote_main, which calls
#   backend.audit.log() over an asyncpg connection. If the unit's
#   environment carries no usable OMNISIGHT_DATABASE_URL the module
#   silently degrades to the SQLite dev path and the durable audit trail
#   for the promote attempt is lost — exactly the failure OP-925's R3
#   run hit ("no compatible audit DB in runner env; asyncpg timed out;
#   psql unavailable").
#
#   Run this BEFORE relying on the cron (and after any change to the
#   .env / DB credentials) to confirm the same code path the module
#   uses can actually reach Postgres and the `release_audit` table.
#
# WHAT IT CHECKS (in order; first failure aborts with a distinct exit
# code so an operator / CI step can branch on the reason)
#   1  OMNISIGHT_DATABASE_URL (or DATABASE_URL) is set AND resolves to a
#      Postgres DSN via backend.db._resolve_pg_dsn (the exact resolver
#      the module uses) — exit 2 if not.
#   2  asyncpg can connect with a short timeout and `SELECT 1` — exit 3
#      on timeout / connection error (the asyncpg-timeout failure mode).
#   3  the `release_audit` table exists (alembic 0207 applied) — exit 4
#      if absent.
#   On success it also prints the most recent release_audit row (if any)
#   for a quick eyeball, and exits 0.
#
#   Note: this deliberately does NOT shell out to `psql` — OP-960/OP-961
#   removed the psql dependency from the promote path, and psql is not
#   installed in the runner env. The diagnostic `which psql` line below
#   is informational only.
#
# USAGE
#   # On the prod host, as the user that owns the systemd --user units:
#   set -a; . ~/work/sora/OmniSight-Productizer/.env; set +a
#   PYTHONPATH=$HOME/work/sora/OmniSight-Productizer \
#     deploy/scripts/auto_promote_audit_db_smoke.sh
#
#   Or, mimicking the unit exactly:
#   systemd-run --user --pty \
#     -p EnvironmentFile=-%h/work/sora/OmniSight-Productizer/.env \
#     -p Environment=PYTHONPATH=/home/user/sora-bridge \
#     /home/user/sora-bridge/deploy/scripts/auto_promote_audit_db_smoke.sh
#
# ENV
#   OMNISIGHT_DATABASE_URL / DATABASE_URL  the DSN (SQLAlchemy or libpq form)
#   OP964_SMOKE_TIMEOUT                    asyncpg connect timeout, seconds (default 10)
#   PYTHONPATH                             must contain the `backend` package
set -euo pipefail

TIMEOUT="${OP964_SMOKE_TIMEOUT:-10}"

log() { printf '[OP-964 audit-db-smoke] %s\n' "$*"; }

# ── Diagnostic header (matches the ticket's "Diagnosis steps") ───────
log "which psql -> $(command -v psql || echo '(not installed — expected; promote path is asyncpg-only since OP-961)')"
if [ -n "${OMNISIGHT_DATABASE_URL:-}" ]; then
    log "OMNISIGHT_DATABASE_URL is set (value redacted)"
elif [ -n "${DATABASE_URL:-}" ]; then
    log "DATABASE_URL is set (value redacted); OMNISIGHT_DATABASE_URL is unset"
else
    log "neither OMNISIGHT_DATABASE_URL nor DATABASE_URL is set"
fi

# ── Steps 1-3 run inside one python3 process (same resolver + driver
#    the auto_promote_main audit sink uses) ───────────────────────────
python3 - "$TIMEOUT" <<'PYEOF'
import asyncio
import os
import sys

timeout = float(sys.argv[1])

try:
    from backend.db import _resolve_pg_dsn
except Exception as exc:  # PYTHONPATH not pointing at the backend package
    print(f"[OP-964 audit-db-smoke] FAIL: cannot import backend.db ({type(exc).__name__}: {exc})", flush=True)
    print("[OP-964 audit-db-smoke]       set PYTHONPATH to the OmniSight-Productizer / sora-bridge checkout root", flush=True)
    sys.exit(5)

dsn = _resolve_pg_dsn()
if not dsn:
    print("[OP-964 audit-db-smoke] FAIL: no Postgres DSN resolved from OMNISIGHT_DATABASE_URL / DATABASE_URL", flush=True)
    print("[OP-964 audit-db-smoke]       the audit sink would fall back to the SQLite dev path and skip release_audit", flush=True)
    sys.exit(2)

try:
    import asyncpg
except Exception as exc:
    print(f"[OP-964 audit-db-smoke] FAIL: asyncpg not importable ({type(exc).__name__}: {exc})", flush=True)
    sys.exit(3)


async def main() -> int:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[OP-964 audit-db-smoke] FAIL: asyncpg connect timed out after {timeout:g}s (network / firewall / wrong host?)", flush=True)
        return 3
    except Exception as exc:
        print(f"[OP-964 audit-db-smoke] FAIL: asyncpg connect error ({type(exc).__name__}: {exc})", flush=True)
        return 3
    try:
        one = await conn.fetchval("SELECT 1")
        assert one == 1, one
        print("[OP-964 audit-db-smoke] OK: SELECT 1 succeeded", flush=True)

        has_table = await conn.fetchval("SELECT to_regclass('public.release_audit') IS NOT NULL")
        if not has_table:
            print("[OP-964 audit-db-smoke] FAIL: release_audit table missing — apply alembic 0207_release_audit", flush=True)
            return 4
        print("[OP-964 audit-db-smoke] OK: release_audit table present", flush=True)

        row = await conn.fetchrow("SELECT id, outcome, fix_version, ts FROM release_audit ORDER BY ts DESC LIMIT 1")
        if row is None:
            print("[OP-964 audit-db-smoke] note: release_audit is empty (no D5 run recorded yet)", flush=True)
        else:
            print(f"[OP-964 audit-db-smoke] latest release_audit row: id={row['id']} outcome={row['outcome']!r} fix_version={row['fix_version']!r} ts={row['ts']}", flush=True)
        return 0
    finally:
        await conn.close()


sys.exit(asyncio.run(main()))
PYEOF
rc=$?

if [ "$rc" -eq 0 ]; then
    log "PASS: D5 auto-promote can reach the release_audit Postgres DB"
fi
exit "$rc"
