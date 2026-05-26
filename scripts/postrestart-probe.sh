#!/usr/bin/env bash
# OmniSight post-restart probe — OP-1756 / v2-⑧-3a (family8 §7).
#
# Run AFTER the prod compose stack comes up (systemd ExecStartPost / a oneshot
# ordered After= the compose unit). Within the 60 s RTO (§11), verify the stack
# reached a healthy state and emit ONE JSON verification record the operator can read:
#
#   1. Postgres  — `pg_isready` (PG accepting connections; WAL replay finished).
#   2. alembic_version — the row exists and carries a non-empty revision (DB migrated,
#      not mid-upgrade-truncated).
#   3. backend `/readyz` — HTTP 200 (app + provider chain + migrations gate all green).
#   4. DRIFT (NON-FATAL, surface only): compare the running image's expected head
#      (`/version` → `alembic_head_in_image`) to the live DB head. A mismatch is
#      SURFACED (logged + recorded `drift:true`) but does NOT fail this probe —
#      Family ⑥ (alembic drift gate + rescue CLI) owns remediation (§7.4 / §9).
#
# Exit codes:
#   0  — PG + alembic_version + /readyz all healthy within the RTO (drift, if any, surfaced).
#   1  — at least one of PG / alembic_version / /readyz did NOT reach healthy within the RTO.
#   2  — usage / preflight error (docker missing, container not found).
#
# This probe NEVER remediates: it does not run alembic, restart services, or take
# backups. It only verifies + surfaces. (family8 §7.4, §9 cross-family delegation.)
set -uo pipefail

RTO="${OMNISIGHT_POSTRESTART_RTO:-60}"
PG_CONTAINER="${OMNISIGHT_PG_CONTAINER:-omnisight-pg-primary}"
PG_USER="${OMNISIGHT_PG_USER:-omnisight}"
PG_DB="${OMNISIGHT_PG_DB:-omnisight}"
BACKEND_CONTAINER="${OMNISIGHT_BACKEND_CONTAINER:-omnisight-productizer-backend-a-1}"
READYZ_PATH="${OMNISIGHT_READYZ_PATH:-http://localhost:8000/readyz}"
VERSION_PATH="${OMNISIGHT_VERSION_PATH:-http://localhost:8000/api/v1/version}"

log()  { printf '\033[36m[postrestart-probe]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[postrestart-probe]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[31m[postrestart-probe]\033[0m %s\n' "$*" >&2; }

case "${1:-}" in
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
esac

command -v docker >/dev/null 2>&1 || { err "docker not found"; exit 2; }
docker inspect "$PG_CONTAINER" >/dev/null 2>&1 || { err "PG container '$PG_CONTAINER' not found"; exit 2; }
docker inspect "$BACKEND_CONTAINER" >/dev/null 2>&1 || { err "backend container '$BACKEND_CONTAINER' not found"; exit 2; }

dexec() { docker exec "$1" sh -c "$2" 2>/dev/null; }

deadline=$(( $(date +%s) + RTO ))
pg_ok=false; alembic_ok=false; readyz_ok=false; db_head=""; readyz_code=""

while :; do
  # 1. Postgres ready
  if ! $pg_ok && docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -q 2>/dev/null; then
    pg_ok=true; log "Postgres ready"
  fi
  # 2. alembic_version row present + non-empty revision
  if $pg_ok && ! $alembic_ok; then
    db_head="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
      'SELECT version_num FROM alembic_version' 2>/dev/null | head -1 | tr -d '[:space:]')"
    [ -n "$db_head" ] && { alembic_ok=true; log "alembic_version head: $db_head"; }
  fi
  # 3. backend /readyz == 200
  if ! $readyz_ok; then
    readyz_code="$(dexec "$BACKEND_CONTAINER" "curl -fsS -o /dev/null -w '%{http_code}' '$READYZ_PATH' || true")"
    [ "$readyz_code" = "200" ] && { readyz_ok=true; log "/readyz 200"; }
  fi
  if $pg_ok && $alembic_ok && $readyz_ok; then break; fi
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 3
done

# 4. DRIFT (non-fatal): image-expected head vs DB head
img_head="$(dexec "$BACKEND_CONTAINER" "curl -fsS '$VERSION_PATH' || true" \
  | grep -oE '"alembic_head_in_image"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -oE '"[^"]*"$' | tr -d '"')"
drift=false
if [ -n "$img_head" ] && [ -n "$db_head" ] && [ "$img_head" != "$db_head" ]; then
  drift=true
  warn "DRIFT: image expects alembic head '$img_head' but DB head is '$db_head' — SURFACED (Family ⑥ owns rescue); NOT remediating here."
fi

elapsed=$(( RTO - (deadline - $(date +%s)) ))
healthy=false; $pg_ok && $alembic_ok && $readyz_ok && healthy=true
printf '{"event":"postrestart_probe","healthy":%s,"pg_ready":%s,"alembic_ok":%s,"readyz_code":"%s","db_head":"%s","image_head":"%s","drift":%s,"rto_s":%s,"elapsed_s":%s,"ts":"%s"}\n' \
  "$healthy" "$pg_ok" "$alembic_ok" "${readyz_code:-none}" "${db_head:-none}" "${img_head:-none}" "$drift" "$RTO" "$elapsed" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if $healthy; then
  log "post-restart probe PASS (drift=$drift)"
  exit 0
fi
err "post-restart probe FAIL within ${RTO}s: pg_ready=$pg_ok alembic_ok=$alembic_ok readyz=${readyz_code:-none}"
exit 1
