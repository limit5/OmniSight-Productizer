#!/usr/bin/env bash
#
# OmniSight DR drill — operator-runnable wrapper (G6 #5 / HA-06, TODO row 1383).
#
# The G6 #1 daily CI workflow (`.github/workflows/dr-drill-daily.yml`)
# proves the backup round-trip holds on a **fresh VM**. This script is
# the **local-host twin** — same four stages, same backup API, same
# selftest, same smoke subset — so an operator can prove (or
# reproduce a CI red) from their own shell without waiting on GitHub
# Actions or spinning up a runner.
#
# Four stages — each a SEPARATE step so a red surfaces the exact
# point of breakage (mirrors the G6 #1 job boundaries):
#
#   1. primary-backup     sqlite3.Connection.backup() of the source
#                         DB into `<out>/backup.db`. Same Python API
#                         that scripts/backup_selftest.py uses so the
#                         bytes we hand over are exactly the bytes
#                         the selftest expects.
#
#   2. secondary-restore  Copies the backup into `<out>/restored.db`,
#                         simulating the "another host" hop. On CI
#                         the hop is two VMs + an artefact; here it
#                         is two files in the same filesystem (the
#                         round-trip that matters is the Python
#                         backup → open cycle, not the physical hop).
#
#   3. selftest           Invokes `scripts/backup_selftest.py
#                         <out>/restored.db` — same script the CI
#                         workflow runs. Verifies integrity_check +
#                         6 required tables + Phase 53 audit_log
#                         hash chain.
#
#   4. smoke-subset       Runs `pytest backend/tests/
#                         test_prod_smoke_test_subset_cli.py` — the
#                         same smoke subset G6 #1 runs. Can be
#                         skipped with `--no-smoke` for a data-plane-
#                         only drill.
#
#   report                Always runs (even on earlier failure) and
#                         writes a markdown summary to
#                         `<out>/dr-drill-report.md` that mirrors the
#                         G6 #1 report shape so reviewers see the
#                         same table whether the drill ran on CI or
#                         locally.
#
# Usage:
#   scripts/dr_drill.sh                      # drill against data/omnisight.db
#   scripts/dr_drill.sh --db data/prod.db    # drill a specific DB
#   scripts/dr_drill.sh --out /tmp/drill-42  # write artefacts to a path
#   scripts/dr_drill.sh --no-smoke           # skip smoke subset (data-plane only)
#   scripts/dr_drill.sh --seed               # seed a synthetic DB first
#                                            # (same seed the G6 #1 workflow uses —
#                                            # lets the drill run on empty checkouts)
#   scripts/dr_drill.sh --help               # usage
#
# Exit codes (mirror scripts/backup_selftest.py where applicable so
# the operator can read the same cheat-sheet for both):
#   0  — all stages green
#   1  — usage / missing input file
#   2  — backup stage failed (source DB unreadable, backup() raised)
#   3  — restore / selftest stage failed (integrity_check, schema, hash chain)
#   4  — smoke-subset stage failed (pytest non-zero)
#   5  — report stage failed (couldn't write the markdown summary)
#
# Why this script exists even though G6 #1 already runs daily:
#   * CI green ≠ operator-host green. A real DR event will be
#     executed on the operator's laptop or the on-call host, NOT on
#     a GitHub runner — the shape of that host must be rehearsable
#     without network access to github.com.
#   * Developer loop: anyone touching `scripts/backup_selftest.py`,
#     the SQLite schema, or `scripts/prod_smoke_test.py` can run
#     this script locally in ~15 s and know if G6 #1 will red on
#     the next daily schedule.
#   * Annual drill (G6 #4 Scenario C §5) explicitly invokes this
#     script on the staging host against the real staging backup,
#     not the CI-seeded synthetic one.
#
# Canonical docs (in reading order):
#   * `docs/ops/dr_runbook.md`           — G6 #5 bundle aggregator;
#                                          points the operator at this
#                                          script + every sibling artefact.
#   * `docs/ops/dr_rto_rpo.md`           — G6 #2 RTO ≤ 15 min / RPO ≤ 5 min.
#   * `docs/ops/dr_manual_failover.md`   — G6 #3 manual failover step-by-step.
#   * `docs/ops/dr_annual_drill_checklist.md` — G6 #4 annual human-led drill.
#   * `.github/workflows/dr-drill-daily.yml`  — G6 #1 automated daily drill.

set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# Locate the project root from the script path so the drill runs
# from anywhere (`cd /tmp && scripts/dr_drill.sh` works because
# $0 tells us where scripts/ actually lives).
# ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Defaults. Every default is documented in the --help output so the
# operator does not have to read this file to understand them.
DB_PATH_DEFAULT="${PROJECT_ROOT}/data/omnisight.db"
OUT_DIR_DEFAULT="${PROJECT_ROOT}/dr-artifacts"

DB_PATH="${DB_PATH_DEFAULT}"
OUT_DIR="${OUT_DIR_DEFAULT}"
RUN_SMOKE=1
DO_SEED=0

# ─────────────────────────────────────────────────────────────────
# Logging — cyan prefix so the drill's output is visible inside
# dense CI logs. Mirrors `scripts/backup_selftest.py`'s `log()`.
# ─────────────────────────────────────────────────────────────────
log() {
    # shellcheck disable=SC2059
    printf '\033[36m[dr-drill]\033[0m %s\n' "$*"
}

fail() {
    local code="$1"; shift
    printf 'error: %s\n' "$*" >&2
    # Best-effort report write before we exit — operators should see
    # a report artefact even on failure. If the report write itself
    # fails, we exit with the ORIGINAL code (not 5) so the cause is
    # not obscured.
    write_report "$code" || true
    exit "$code"
}

usage() {
    cat <<'USAGE'
OmniSight DR drill — operator-runnable wrapper (G6 #5)

Usage:
  scripts/dr_drill.sh [--db PATH] [--out DIR] [--no-smoke] [--seed] [--help]

Options:
  --db PATH       Source SQLite DB to drill. Default: data/omnisight.db
  --out DIR       Artefact directory (backup + restored + report).
                  Default: dr-artifacts/ under the project root.
  --no-smoke      Skip the pytest smoke-subset stage. Data-plane-only drill.
  --seed          Seed a synthetic OmniSight DB at --db first (6 tables
                  + 5-row audit_log hash chain). Same seed the CI
                  workflow uses — lets the drill run on empty checkouts.
  --help, -h      Show this message.

Exit codes:
  0  all stages green
  1  usage / missing input
  2  backup stage failed
  3  restore / selftest stage failed
  4  smoke-subset stage failed
  5  report stage failed

Canonical docs: see docs/ops/dr_runbook.md (G6 #5 aggregator).
USAGE
}

# ─────────────────────────────────────────────────────────────────
# Arg parsing.
# ─────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --db)
            [ $# -ge 2 ] || { usage; exit 1; }
            DB_PATH="$2"; shift 2 ;;
        --out)
            [ $# -ge 2 ] || { usage; exit 1; }
            OUT_DIR="$2"; shift 2 ;;
        --no-smoke)
            RUN_SMOKE=0; shift ;;
        --seed)
            DO_SEED=1; shift ;;
        --help|-h)
            usage; exit 0 ;;
        *)
            usage; exit 1 ;;
    esac
done

mkdir -p "${OUT_DIR}"
BACKUP_PATH="${OUT_DIR}/backup.db"
RESTORED_PATH="${OUT_DIR}/restored.db"
REPORT_PATH="${OUT_DIR}/dr-drill-report.md"

# Per-stage status tokens. Every stage writes its own token so the
# report accurately reflects WHICH stage failed, not just THAT
# something failed.
STATUS_BACKUP="skipped"
STATUS_RESTORE="skipped"
STATUS_SELFTEST="skipped"
STATUS_SMOKE="skipped"

# ─────────────────────────────────────────────────────────────────
# Stage: seed (optional; runs before everything else).
# ─────────────────────────────────────────────────────────────────
seed_synthetic_db() {
    log "seeding synthetic OmniSight DB at ${DB_PATH}"
    mkdir -p "$(dirname "${DB_PATH}")"
    DB_PATH="${DB_PATH}" python3 - <<'PY'
import hashlib
import os
import sqlite3

db_path = os.environ["DB_PATH"]
# Match the schema `scripts/backup_selftest.py` requires. If the
# selftest's REQUIRED_TABLES tuple changes, this seed must change
# with it — do not silently drop a table here.
conn = sqlite3.connect(db_path)
cur = conn.cursor()
for stmt in (
    "CREATE TABLE IF NOT EXISTS tasks "
    "(id INTEGER PRIMARY KEY, payload TEXT)",
    "CREATE TABLE IF NOT EXISTS agents "
    "(id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE IF NOT EXISTS workflow_runs "
    "(id INTEGER PRIMARY KEY, status TEXT)",
    "CREATE TABLE IF NOT EXISTS workflow_steps "
    "(id INTEGER PRIMARY KEY, run_id INT)",
    "CREATE TABLE IF NOT EXISTS episodic_memory "
    "(id INTEGER PRIMARY KEY, body TEXT)",
    "CREATE TABLE IF NOT EXISTS audit_log "
    "(id INTEGER PRIMARY KEY, hash TEXT, prev_hash TEXT)",
):
    cur.execute(stmt)
# Re-seeds are idempotent; don't double the audit chain.
existing = cur.execute("SELECT count(*) FROM audit_log").fetchone()[0]
if existing == 0:
    prev = None
    for i in range(1, 6):
        h = hashlib.sha256(f"row-{i}|{prev}".encode()).hexdigest()
        cur.execute(
            "INSERT INTO audit_log (hash, prev_hash) VALUES (?, ?)",
            (h, prev),
        )
        prev = h
    cur.executemany(
        "INSERT INTO tasks (payload) VALUES (?)",
        [(f"seed-{i}",) for i in range(20)],
    )
conn.commit()
conn.close()
PY
}

# ─────────────────────────────────────────────────────────────────
# Stage 1: primary-backup.
# Uses sqlite3.Connection.backup() — same WAL-safe API the CI
# workflow and the selftest script use. This is intentional: if the
# API call drifts, the drill drifts, and we prefer one central
# import path over three copies of equivalent code.
# ─────────────────────────────────────────────────────────────────
stage_backup() {
    log "stage 1 / primary-backup — ${DB_PATH} → ${BACKUP_PATH}"
    if [ ! -f "${DB_PATH}" ]; then
        STATUS_BACKUP="failed"
        fail 1 "source DB ${DB_PATH} not found (pass --seed to create a synthetic one)"
    fi
    if ! DB_PATH="${DB_PATH}" BACKUP_PATH="${BACKUP_PATH}" python3 - <<'PY'
import os
import sqlite3
import sys

src = sqlite3.connect(os.environ["DB_PATH"])
try:
    dst = sqlite3.connect(os.environ["BACKUP_PATH"])
    try:
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()
size = os.path.getsize(os.environ["BACKUP_PATH"])
print(f"backup wrote {size} bytes")
# Tiny-file heuristic matches backup_selftest.py's 1-KiB floor.
if size < 1024:
    print("error: backup suspiciously small", file=sys.stderr)
    sys.exit(2)
PY
    then
        STATUS_BACKUP="failed"
        fail 2 "sqlite3 online backup failed"
    fi
    STATUS_BACKUP="success"
}

# ─────────────────────────────────────────────────────────────────
# Stage 2: secondary-restore.
# Copy (not rename) so a corrupt backup cannot destroy the source;
# copying is the simulated "cross-host" hop on a local drill.
# ─────────────────────────────────────────────────────────────────
stage_restore() {
    log "stage 2 / secondary-restore — ${BACKUP_PATH} → ${RESTORED_PATH}"
    if ! cp "${BACKUP_PATH}" "${RESTORED_PATH}"; then
        STATUS_RESTORE="failed"
        fail 3 "failed to copy backup to restored path"
    fi
    if [ ! -s "${RESTORED_PATH}" ]; then
        STATUS_RESTORE="failed"
        fail 3 "restored DB is empty"
    fi
    STATUS_RESTORE="success"
}

# ─────────────────────────────────────────────────────────────────
# Stage 3: selftest — integrity_check + schema + hash chain.
# This is the load-bearing stage; see scripts/backup_selftest.py
# for the exit-code contract.
# ─────────────────────────────────────────────────────────────────
stage_selftest() {
    log "stage 3 / selftest — scripts/backup_selftest.py ${RESTORED_PATH}"
    if ! python3 "${PROJECT_ROOT}/scripts/backup_selftest.py" "${RESTORED_PATH}"; then
        STATUS_SELFTEST="failed"
        fail 3 "backup_selftest.py returned non-zero"
    fi
    STATUS_SELFTEST="success"
}

# ─────────────────────────────────────────────────────────────────
# Stage 4: smoke-subset — pytest contract for the DAG-1 CLI path.
# Mirrors the G6 #1 `smoke-subset` job. Can be skipped with
# --no-smoke when only the data-plane round-trip is of interest.
# ─────────────────────────────────────────────────────────────────
stage_smoke() {
    if [ "${RUN_SMOKE}" -eq 0 ]; then
        log "stage 4 / smoke-subset — skipped (--no-smoke)"
        STATUS_SMOKE="skipped"
        return 0
    fi
    log "stage 4 / smoke-subset — pytest test_prod_smoke_test_subset_cli.py"
    local smoke_test="${PROJECT_ROOT}/backend/tests/test_prod_smoke_test_subset_cli.py"
    if [ ! -f "${smoke_test}" ]; then
        STATUS_SMOKE="failed"
        fail 4 "smoke test file not found: ${smoke_test}"
    fi
    if ! ( cd "${PROJECT_ROOT}" && python3 -m pytest -q "${smoke_test}" ); then
        STATUS_SMOKE="failed"
        fail 4 "smoke-subset pytest returned non-zero"
    fi
    STATUS_SMOKE="success"
}

# ─────────────────────────────────────────────────────────────────
# Report. Always called (from success path AND fail() trap) so the
# operator sees the same markdown shape whether the drill went green
# or red. The table columns mirror the G6 #1 CI report so reviewers
# can scan both with the same template.
# ─────────────────────────────────────────────────────────────────
write_report() {
    local exit_code="$1"
    local overall
    if [ "${exit_code}" = "0" ]; then
        overall="success"
    else
        overall="failed (exit ${exit_code})"
    fi
    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    # HEREDOC-write the report so the file is atomic-ish (single
    # write) and the column alignment matches the CI report. Any
    # read-side (operator, annual drill, CI sibling check) can rely
    # on the identical shape.
    if ! cat > "${REPORT_PATH}" <<EOF
# OmniSight DR drill report

* Generated by  : \`scripts/dr_drill.sh\` (G6 #5 / row 1383)
* Timestamp     : ${ts}
* Source DB     : ${DB_PATH}
* Artefact dir  : ${OUT_DIR}
* Skip smoke?   : $( [ "${RUN_SMOKE}" -eq 0 ] && echo "yes (--no-smoke)" || echo "no" )
* Seeded DB?    : $( [ "${DO_SEED}" -eq 1 ] && echo "yes (--seed)" || echo "no" )

## Round-trip stages

| Stage                | Result                |
| -------------------- | --------------------- |
| primary-backup       | \`${STATUS_BACKUP}\`  |
| secondary-restore    | \`${STATUS_RESTORE}\` |
| selftest             | \`${STATUS_SELFTEST}\`|
| smoke-subset         | \`${STATUS_SMOKE}\`   |

**Overall: ${overall}**

## Interpretation

* All \`success\` → local backup path round-trips cleanly. A CI red
  on G6 #1 after this is green usually means a runner-image change,
  not a code change.
* \`primary-backup\` failed → source DB unreadable or the Python
  \`sqlite3.Connection.backup()\` API broke. Inspect the source DB
  with \`sqlite3 "${DB_PATH}" "PRAGMA integrity_check;"\`.
* \`secondary-restore\` failed → filesystem copy / permissions /
  disk-full. Verify \`${OUT_DIR}\` is writable + has space.
* \`selftest\` failed → see \`scripts/backup_selftest.py\` exit
  codes (2 = backup step, 3 = integrity_check, 4 = schema or hash
  chain). Fix the DB, not the selftest.
* \`smoke-subset\` failed → the DAG-1 CLI contract regressed;
  bisect recent commits touching \`scripts/prod_smoke_test.py\`.

## Canonical docs

* G6 #5 aggregator      : \`docs/ops/dr_runbook.md\`
* G6 #2 RTO / RPO       : \`docs/ops/dr_rto_rpo.md\`
* G6 #3 manual failover : \`docs/ops/dr_manual_failover.md\`
* G6 #4 annual drill    : \`docs/ops/dr_annual_drill_checklist.md\`
* G6 #1 daily CI drill  : \`.github/workflows/dr-drill-daily.yml\`
EOF
    then
        return 5
    fi
    log "report written to ${REPORT_PATH}"
}

# ─────────────────────────────────────────────────────────────────
# Main.
# ─────────────────────────────────────────────────────────────────
log "DR drill starting — db=${DB_PATH} out=${OUT_DIR} smoke=${RUN_SMOKE}"
if [ "${DO_SEED}" -eq 1 ]; then
    seed_synthetic_db
fi
stage_backup
stage_restore
stage_selftest
stage_smoke
if ! write_report 0; then
    # Report-stage failure is the only path that reaches exit 5.
    fail 5 "could not write ${REPORT_PATH}"
fi
log "DR drill complete — all stages green"
exit 0
