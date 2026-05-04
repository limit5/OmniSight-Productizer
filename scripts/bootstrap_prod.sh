#!/usr/bin/env bash
# bootstrap_prod.sh — first-time OmniSight production stack via
# docker-compose.prod.yml (Path B per docs/ops/multi-wsl-deployment.md).
#
# This host's role: WSL Ubuntu-24.04 = Production.
#
# NOT a re-deploy tool — for every subsequent upgrade use
# `scripts/deploy-prod.sh` (G2 rolling-restart, preserves uptime).
# This script only exists because the rolling-restart path assumes a
# previously-successful `up -d` + alembic-applied DB, which a first
# boot does not have.
#
# What it does (in order):
#   §0 Pre-flight        repo / docker / ports / disk / env / compose
#   §1 Volume handling   --fresh wipes omnisight-* volumes
#   §2 Image build       `docker compose build` (or --skip-build to reuse)
#   §3 Backends up       backend-a + backend-b only (caddy/frontend wait)
#   §4 Alembic           upgrade head inside backend-a (shared volume)
#   §5 Readiness poll    /readyz on :8000 + :8001 must return ready=true
#   §6 Caddy + frontend  up; wait caddy healthy + frontend :3000 200
#   §7 Smoke tests       local liveness + https via caddy
#   §8 Summary           compose ps + next-step checklist
#
# Safety / self-heal:
#   · retry wrapper on build + alembic (cmd-failure rc propagated)
#   · ERR trap dumps `docker compose logs <LAST_SVC>` on failure
#   · all destructive actions gated by `(( DRY_RUN ))` returns
#   · EXIT trap uses `${VAR:-}` for set -u compatibility
#
# Usage:
#   scripts/bootstrap_prod.sh                    # interactive, confirm before acting
#   scripts/bootstrap_prod.sh --yes              # unattended
#   scripts/bootstrap_prod.sh --dry-run          # print plan, no changes
#   scripts/bootstrap_prod.sh --fresh            # wipe omnisight-* volumes first
#   scripts/bootstrap_prod.sh --skip-build       # reuse existing local images

set -Eeuo pipefail
shopt -s inherit_errexit

# ═════════════════════════════════════════════════════════════════════
# Args
# ═════════════════════════════════════════════════════════════════════
YES=0 DRY_RUN=0 FRESH=0 SKIP_BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)        YES=1; shift;;
    -n|--dry-run)    DRY_RUN=1; YES=1; shift;;
    --fresh)         FRESH=1; shift;;
    --skip-build)    SKIP_BUILD=1; shift;;
    -h|--help)       sed -n '2,37p' "$0"; exit 0;;
    *)               echo "unknown arg: $1" >&2; exit 1;;
  esac
done

# ═════════════════════════════════════════════════════════════════════
# Locate repo + logging
# ═════════════════════════════════════════════════════════════════════
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[[ -f docker-compose.prod.yml ]] || { echo "not at repo root" >&2; exit 1; }

COMPOSE=(docker compose -f docker-compose.prod.yml)
TS="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="$REPO/data/deploy-logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/compose-bootstrap-$TS.log"
{ ls -1t "$LOG_DIR"/compose-bootstrap-*.log 2>/dev/null || true; } | tail -n +11 | xargs -r rm -f

if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m' C_DIM=$'\033[2m' C_OK=$'\033[32m'
  C_WARN=$'\033[33m' C_ERR=$'\033[31m' C_OFF=$'\033[0m'
else
  C_BOLD= C_DIM= C_OK= C_WARN= C_ERR= C_OFF=
fi

step() { printf '\n%s═══ %s ═══%s\n' "$C_BOLD" "$*" "$C_OFF"; }
ok()   { printf '  %s[OK]%s   %s\n' "$C_OK"   "$C_OFF" "$*"; }
info() { printf '  %s[..]%s   %s\n' "$C_DIM"  "$C_OFF" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '  %s[FAIL]%s %s\n' "$C_ERR"  "$C_OFF" "$*" >&2; exit "${2:-2}"; }

exec > >(tee -a "$LOG") 2>&1
printf '%s# bootstrap log — %s (pid %s, args: yes=%s dry=%s fresh=%s skip-build=%s)%s\n\n' \
  "$C_DIM" "$(date -Iseconds)" "$$" "$YES" "$DRY_RUN" "$FRESH" "$SKIP_BUILD" "$C_OFF"

# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════
run() {
  printf '  %s$%s %s\n' "$C_DIM" "$C_OFF" "$*"
  (( DRY_RUN )) && return 0
  "$@"
}

retry() {
  # retry N CMD... — bash gotcha fix: `cmd && return 0` preserves $?;
  # the `if cmd; then ...; fi` pattern silently clobbers it to 0.
  local tries="$1"; shift
  if (( DRY_RUN )); then
    printf '  %s$%s %s  (retry x%s)\n' "$C_DIM" "$C_OFF" "$*" "$tries"; return 0
  fi
  local a=1 rc=0
  while (( a <= tries )); do
    "$@" && return 0
    rc=$?
    (( a < tries )) && { warn "attempt $a/$tries rc=$rc — retry in $((2*a))s"; sleep $((2*a)); }
    a=$((a+1))
  done
  return "$rc"
}

confirm() {
  (( YES )) && return 0
  read -rp "  Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || die "declined by user" 1
}

# Track which compose service is in flight so ERR trap can dump its logs.
LAST_SVC=""
TMP="$(mktemp -d)"

cleanup_exit() { rm -rf "${TMP:-}"; }
on_err() {
  local rc=$?
  local line="${BASH_LINENO[0]:-?}"
  printf '\n%s[✗]%s aborted at %s:%s (rc=%s)\n' "$C_ERR" "$C_OFF" \
    "$(basename "${BASH_SOURCE[0]}")" "$line" "$rc" >&2
  if [[ -n "${LAST_SVC:-}" ]]; then
    printf '\n%s── docker compose logs %s --tail 60 ──%s\n' "$C_DIM" "$LAST_SVC" "$C_OFF" >&2
    "${COMPOSE[@]}" logs --tail 60 "$LAST_SVC" 2>&1 | sed 's/^/    /' >&2 || true
  fi
  printf '\nLog: %s\n' "$LOG" >&2
  printf 'Recovery hints:\n  · env issue → edit .env; re-run (idempotent)\n  · build failed → inspect images: docker images | grep omnisight\n  · migration failed → retry with --fresh to wipe DB volume\n' >&2
  cleanup_exit
  exit "$rc"
}
trap on_err ERR
trap cleanup_exit EXIT

# Inspect a compose service's container status. Handles both v2 naming
# schemes (hyphen and underscore). Returns state string or "missing".
container_state() {
  local svc="$1" field="${2:-State.Status}"
  for name in "omnisight-productizer-${svc}-1" "omnisight-productizer_${svc}_1"; do
    if docker inspect -f "{{.${field}}}" "$name" >/dev/null 2>&1; then
      docker inspect -f "{{.${field}}}" "$name"; return 0
    fi
  done
  echo missing
}

# ═════════════════════════════════════════════════════════════════════
# §0 Pre-flight
# ═════════════════════════════════════════════════════════════════════
step "§0 Pre-flight"

# Docker daemon + compose plugin + group
docker info >/dev/null 2>&1 || die "Docker daemon unreachable (sudo systemctl start docker?)"
"${COMPOSE[@]}" version >/dev/null 2>&1 || die "docker compose v2 plugin missing"
id -nG | tr ' ' '\n' | grep -qx docker \
  || die "user $(id -un) not in 'docker' group — sudo usermod -aG docker $(id -un) && re-login"
ok "Docker $(docker --version | grep -oE '[0-9][0-9.]*' | head -1)  /  Compose $("${COMPOSE[@]}" version --short 2>/dev/null)"

# Ports must be free (or we'd collide on up)
for p in 80 443 3000 8000 8001; do
  if ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
    who="$(ss -ltnp "sport = :$p" 2>/dev/null | sed -nE 's/.*users:\(\("([^"]+)".*/\1/p' | head -1)"
    die "port $p in use by ${who:-?}"
  fi
done
ok "ports 80 / 443 / 3000 / 8000 / 8001 all free"

# Disk ≥ 10 GB (image + volumes + build cache)
DISK_G="$(df -BG --output=avail . | tail -1 | tr -dc '0-9')"
(( DISK_G >= 10 )) || die "only ${DISK_G}G free, need ≥ 10G"
ok "disk: ${DISK_G}G free"

# Git state — warn only (operator may be deploying a local tag)
git fetch origin master --quiet 2>/dev/null || warn "git fetch failed (offline?)"
BEHIND="$(git rev-list --count HEAD..origin/master 2>/dev/null || echo 0)"
[[ "$BEHIND" == "0" ]] || warn "local is $BEHIND commit(s) behind origin/master"
ok "HEAD: $(git rev-parse --short HEAD) — $(git log -1 --format=%s | head -c 60)"

# .env exists + has all required strict-mode keys
[[ -f .env ]] || die ".env missing — see backup/deploy-attempts branch for a template"
chmod 600 .env
for k in OMNISIGHT_ANTHROPIC_API_KEY ANTHROPIC_API_KEY OMNISIGHT_ADMIN_EMAIL \
         OMNISIGHT_ADMIN_PASSWORD OMNISIGHT_DECISION_BEARER \
         OMNISIGHT_COOKIE_SECURE OMNISIGHT_FRONTEND_ORIGIN OMNISIGHT_LLM_PROVIDER; do
  grep -qE "^$k=" .env || die ".env missing required key: $k"
done
PW_LEN="$(grep -E '^OMNISIGHT_ADMIN_PASSWORD=' .env | cut -d= -f2- | wc -c)"
(( PW_LEN > 12 )) || die "OMNISIGHT_ADMIN_PASSWORD < 12 chars (strict refuses)"
ok ".env has strict-mode required keys + admin password strong enough"

# Compose config valid
"${COMPOSE[@]}" config --quiet 2>&1 | grep -vE 'version.*obsolete' || true
ok "docker-compose.prod.yml parses clean"

# Existing volumes (empty is fine — first boot; non-empty flags --fresh)
DATA_USED="$(docker run --rm -v omnisight-productizer_omnisight-data:/d busybox \
             sh -c 'du -sk /d 2>/dev/null | cut -f1 || echo 0' 2>/dev/null || echo 0)"
if (( DATA_USED > 4 )) && ! (( FRESH )); then
  warn "omnisight-data volume already has ${DATA_USED}K — re-using (pass --fresh to wipe)"
fi
ok "volume state: ${DATA_USED}K in omnisight-data"

# Show the plan
printf '\n%sPlan:%s\n' "$C_BOLD" "$C_OFF"
printf '  · build backend + frontend images  (~5–10 min first time)\n'
(( FRESH )) && printf '  · wipe omnisight-{data,artifacts,sdks} volumes\n'
printf '  · up backend-a + backend-b  (skip caddy/frontend — they depend on healthy)\n'
printf '  · exec alembic upgrade head  (inside backend-a, shared volume)\n'
printf '  · up caddy + frontend\n'
printf '  · verify /readyz + /api/v1/health + https:// via Caddy internal cert\n'
confirm

# ═════════════════════════════════════════════════════════════════════
# §1 Volume handling
# ═════════════════════════════════════════════════════════════════════
step "§1 Volumes"
if (( FRESH )); then
  # Only wipe after prompting confirmation already happened in §0
  for v in omnisight-productizer_omnisight-data \
           omnisight-productizer_omnisight-artifacts \
           omnisight-productizer_omnisight-sdks; do
    if docker volume inspect "$v" >/dev/null 2>&1; then
      run docker volume rm -f "$v"
    fi
  done
  ok "fresh volumes (will be recreated on up)"
else
  ok "keeping existing volumes"
fi

# ═════════════════════════════════════════════════════════════════════
# §2 Build images
# ═════════════════════════════════════════════════════════════════════
step "§2 Build images"
if (( SKIP_BUILD )); then
  warn "--skip-build — assuming images already present"
else
  # GHCR pull is gated on OMNISIGHT_GHCR_NAMESPACE being set; with the
  # default placeholder `your-org` the pull fails fast and compose-build
  # takes over. We skip pull entirely and build locally — deterministic.
  retry 1 run "${COMPOSE[@]}" build
fi
ok "images ready"

# ═════════════════════════════════════════════════════════════════════
# §3 Start backend-a ONLY (avoid first-boot SQLite WAL lock race)
# ═════════════════════════════════════════════════════════════════════
# Why sequential: backend-a + backend-b share the same SQLite volume. On
# first boot the DB file doesn't exist; both try to create it and run
# `PRAGMA journal_mode=WAL` simultaneously → EXCLUSIVE lock contention
# → one crashes with "database is locked". Start A alone, let its
# lifespan finish the WAL switch + alembic applies schema; THEN B
# opens an already-initialised DB with no contention.
step "§3 Start backend-a (alone, first-boot WAL-safe)"
LAST_SVC="backend-a"
run "${COMPOSE[@]}" up -d backend-a
for i in $(seq 1 30); do
  state="$(container_state backend-a)"
  [[ "$state" == "running" ]] && { ok "backend-a container running after ${i}s"; break; }
  (( DRY_RUN )) && { ok "backend-a would be running (dry-run)"; break; }
  sleep 1
  [[ $i -eq 30 ]] && die "backend-a never reached 'running' state (last: $state)"
done

# ═════════════════════════════════════════════════════════════════════
# §4 First-time alembic migrations
# ═════════════════════════════════════════════════════════════════════
# alembic.ini has `script_location = alembic` which is relative to the
# INVOCATION CWD (not the ini's own directory — alembic quirk). So we
# set CWD to /app/backend via `-w`.
#
# Note: pre-FX.9.3, cd-ing to /app/backend put it at sys.path[0]
# (Python auto-adds CWD) and `backend/platform.py` then SHADOWED
# stdlib `platform`, breaking SQLAlchemy's `import platform` at
# top-level. FX.9.3 renamed the module to `backend/platform_profile.py`
# so the shadow is gone. We keep PYTHONSAFEPATH=1 here as
# defence-in-depth — it prevents any future top-level project
# module name from accidentally shadowing a stdlib module under the
# alembic CLI's import order. (Belt-and-suspenders; can be removed
# safely once we're confident no future module name will collide.)
step "§4 Alembic migrations"
LAST_SVC="backend-a"
retry 2 run "${COMPOSE[@]}" exec -T \
  -e PYTHONSAFEPATH=1 -w /app/backend backend-a \
  python3 -m alembic upgrade head

REV="$(run "${COMPOSE[@]}" exec -T backend-a python3 -c \
  'import sqlite3; c=sqlite3.connect("/app/data/omnisight.db"); print(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])' 2>&1 | tail -1)"
ok "alembic head: $REV"

# ═════════════════════════════════════════════════════════════════════
# §5a Wait for backend-a /readyz
# ═════════════════════════════════════════════════════════════════════
step "§5a backend-a readiness"
LAST_SVC="backend-a"
for i in $(seq 1 90); do
  if (( DRY_RUN )); then ok "backend-a would poll /readyz"; break; fi
  body="$(curl -sSf 'http://127.0.0.1:8000/readyz' 2>/dev/null)" || { sleep 1; [[ $i -eq 90 ]] && die "backend-a never ready"; continue; }
  if echo "$body" | python3 -c 'import sys,json; sys.exit(0 if json.loads(sys.stdin.read()).get("ready") else 1)' 2>/dev/null; then
    ok "backend-a /readyz ready=true after ${i}s"; break
  fi
  sleep 1
  [[ $i -eq 90 ]] && die "backend-a never reported ready=true — body: $body"
done

# ═════════════════════════════════════════════════════════════════════
# §5b Start backend-b (WAL already established by backend-a)
# ═════════════════════════════════════════════════════════════════════
step "§5b Start backend-b (DB already in WAL mode)"
LAST_SVC="backend-b"
run "${COMPOSE[@]}" up -d backend-b
for i in $(seq 1 30); do
  state="$(container_state backend-b)"
  [[ "$state" == "running" ]] && { ok "backend-b container running after ${i}s"; break; }
  (( DRY_RUN )) && { ok "backend-b would be running (dry-run)"; break; }
  sleep 1
  [[ $i -eq 30 ]] && die "backend-b never reached 'running' state (last: $state)"
done
for i in $(seq 1 90); do
  if (( DRY_RUN )); then ok "backend-b would poll /readyz"; break; fi
  body="$(curl -sSf 'http://127.0.0.1:8001/readyz' 2>/dev/null)" || { sleep 1; [[ $i -eq 90 ]] && die "backend-b never ready"; continue; }
  if echo "$body" | python3 -c 'import sys,json; sys.exit(0 if json.loads(sys.stdin.read()).get("ready") else 1)' 2>/dev/null; then
    ok "backend-b /readyz ready=true after ${i}s"; break
  fi
  sleep 1
  [[ $i -eq 90 ]] && die "backend-b never reported ready=true — body: $body"
done
LAST_SVC=""

# ═════════════════════════════════════════════════════════════════════
# §6 Caddy + frontend
# ═════════════════════════════════════════════════════════════════════
step "§6 Caddy + frontend"
LAST_SVC="caddy"
run "${COMPOSE[@]}" up -d caddy frontend

for i in $(seq 1 60); do
  (( DRY_RUN )) && { ok "caddy would become healthy"; break; }
  state="$(container_state caddy "State.Health.Status")"
  [[ "$state" == "healthy" ]] && { ok "caddy healthy after $((i*2))s"; break; }
  sleep 2
  [[ $i -eq 60 ]] && die "caddy never healthy (last state: $state)"
done

LAST_SVC="frontend"
for i in $(seq 1 60); do
  (( DRY_RUN )) && { ok "frontend would respond on :3000"; break; }
  if curl -sSf http://127.0.0.1:3000/ >/dev/null 2>&1; then
    ok "frontend :3000 200 after $((i*2))s"; break
  fi
  sleep 2
  [[ $i -eq 60 ]] && die "frontend never responded on :3000"
done
LAST_SVC=""

# ═════════════════════════════════════════════════════════════════════
# §7 Smoke tests
# ═════════════════════════════════════════════════════════════════════
step "§7 Smoke tests"
if ! (( DRY_RUN )); then
  for ep in /livez /readyz /api/v1/health; do
    body="$(curl -sSf "http://127.0.0.1:8000$ep" 2>&1 | head -c 150)"
    printf '  %s→%s backend-a %-18s %s\n' "$C_DIM" "$C_OFF" "$ep" "$body"
  done
  # Caddy :443 self-signed cert — Caddy's internal CA only issues for the
  # hostnames declared in the site block (see Caddyfile), so we must send
  # SNI=localhost via --resolve. A raw https://127.0.0.1 TLS handshake
  # would fail `internal error 80` because Caddy has no cert for that IP.
  body="$(curl -sSfk --resolve 'localhost:443:127.0.0.1' 'https://localhost/readyz' 2>&1 | head -c 150)"
  printf '  %s→%s caddy    %-18s %s\n' "$C_DIM" "$C_OFF" 'https://localhost' "$body"

  # HTTP :80 should 301 → https. `-f` makes curl fail on 301 so use -I + grep
  http_code="$(curl -sI -o /dev/null -w '%{http_code}' 'http://127.0.0.1/' 2>/dev/null)"
  [[ "$http_code" == "301" ]] \
    && ok "caddy http :80 → 301 redirect (correct)" \
    || warn "caddy http :80 unexpected code: $http_code"
else
  info "dry-run: would curl /livez /readyz /api/v1/health + https://127.0.0.1/readyz"
fi

# ═════════════════════════════════════════════════════════════════════
# §8 Summary + next-step checklist
# ═════════════════════════════════════════════════════════════════════
step "§8 Status"
run "${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'

step "§9 Next steps"
PUBLIC="$(grep -E '^OMNISIGHT_FRONTEND_ORIGIN=' .env | cut -d= -f2-)"
cat <<EOF
  1. First admin login — expect HTTP 428 must_change_password; rotate it.
     Local (self-signed):
       curl -sSk -X POST https://127.0.0.1/api/v1/auth/login \\
         -H 'Content-Type: application/json' \\
         -d "{\"email\":\"\$OMNISIGHT_ADMIN_EMAIL\",\"password\":\"\$OMNISIGHT_ADMIN_PASSWORD\"}"

  2. Cloudflare Tunnel — this host already runs 'ai_tunnel'. To expose
     OmniSight at ${PUBLIC}, ADD an ingress rule in that tunnel's config:
       ${PUBLIC} → http://localhost:80   (Caddy)
     then: docker exec ai_tunnel cloudflared tunnel ingress validate
     (or run the wizard: docs/operations/cloudflare_tunnel_wizard.md)

  3. Comprehensive smoke: python3 scripts/prod_smoke_test.py --url ${PUBLIC}

  4. From now on, UPGRADES use: scripts/deploy-prod.sh
     (G2 rolling-restart — keeps uptime. NOT this script.)

  5. Change log — docs/ops/production_deploy.md §10:
     | $(date -u +%Y-%m-%d) | $(id -un) | first-time bootstrap (Path B) | $(git rev-parse --short HEAD) |

Log of this run: $LOG
EOF

printf '\n%s✓ Bootstrap complete.%s\n' "$C_OK" "$C_OFF"
