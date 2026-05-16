#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OmniSight — Production 零停機部署腳本
#
# 在 Ubuntu-24.04 (Production WSL) 上執行
# 實現 G2 rolling restart：一次只重啟一個 backend replica
#
# 使用方式：
#   ./scripts/deploy-prod.sh                    # 從 main branch 部署 (Phase 1, 2026-05-05)
#   ./scripts/deploy-prod.sh --branch=develop   # 部署指定 branch (僅供 staging-style 驗證)
#   ./scripts/deploy-prod.sh --tag v1.2.0       # 部署特定 tag
#   ./scripts/deploy-prod.sh --skip-build       # 跳過 build（已有 GHCR image）
#   ./scripts/deploy-prod.sh --dry-run          # 只印步驟不執行
#   ./scripts/deploy-prod.sh --gerrit-source=gerrit
#                                               # 從 Gerrit remote fetch（預設自動偵測）
#   ./scripts/deploy-prod.sh --alembic-mode=pg-clone
#                                               # 在 PG clone 上 dry-validate migrations
#   ./scripts/deploy-prod.sh --insecure-skip-verify
#                                               # FX.7.9 emergency escape
#                                               # hatch — bypass ref allow-
#                                               # list + GPG signature check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
BRANCH="${OMNISIGHT_DEPLOY_BRANCH:-main}"
TAG=""
SKIP_BUILD=false
DRY_RUN=false
INSECURE_SKIP_VERIFY=false
GERRIT_SOURCE="${OMNISIGHT_GERRIT_SOURCE:-}"
ALEMBIC_MODE="${OMNISIGHT_ALEMBIC_MODE:-apply}"
HEALTH_RETRIES=30
HEALTH_INTERVAL=3

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}✅${NC} $*"; }
warn() { echo -e "${YELLOW}⚠️${NC}  $*"; }
err()  { echo -e "${RED}❌${NC} $*"; exit 1; }
step() { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}\n"; }

# ── CLI Args ──
for arg in "$@"; do
    case "$arg" in
        --tag=*) TAG="${arg#*=}" ;;
        --branch=*) BRANCH="${arg#*=}" ;;
        --skip-build) SKIP_BUILD=true ;;
        --dry-run) DRY_RUN=true ;;
        --gerrit-source=*) GERRIT_SOURCE="${arg#*=}" ;;
        --alembic-mode=*) ALEMBIC_MODE="${arg#*=}" ;;
        --alembic-pg-clone) ALEMBIC_MODE="pg-clone" ;;
        --insecure-skip-verify) INSECURE_SKIP_VERIFY=true ;;
        --help|-h)
            echo "Usage: $0 [--branch=main] [--tag=v1.2.0] [--skip-build] [--dry-run] [--gerrit-source=REMOTE] [--alembic-mode=apply|pg-clone] [--insecure-skip-verify]"
            exit 0 ;;
        *) err "Unknown argument: $arg" ;;
    esac
done

case "$ALEMBIC_MODE" in
    apply|pg-clone) ;;
    *) err "Unknown --alembic-mode=$ALEMBIC_MODE (expected apply or pg-clone)" ;;
esac

_run() {
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] $*"
    else
        eval "$@"
    fi
}

_run_cmd() {
    if [ "$DRY_RUN" = true ]; then
        printf '  [dry-run]'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

_detect_gerrit_source() {
    if [ -n "$GERRIT_SOURCE" ]; then
        git remote get-url "$GERRIT_SOURCE" >/dev/null 2>&1 || \
            err "--gerrit-source=$GERRIT_SOURCE is not a configured git remote"
        printf '%s\n' "$GERRIT_SOURCE"
        return 0
    fi

    if git remote get-url gerrit >/dev/null 2>&1; then
        printf 'gerrit\n'
        return 0
    fi

    local remote
    while IFS= read -r remote; do
        local url
        url="$(git remote get-url "$remote" 2>/dev/null || true)"
        case "$url" in
            *gerrit*|*sora.services*|*29418*)
                printf '%s\n' "$remote"
                return 0 ;;
        esac
    done < <(git remote)

    err "No Gerrit git remote found. Add a 'gerrit' remote or pass --gerrit-source=<remote>; refusing to fetch from a possibly stale mirror."
}

_upsert_env() {
    local key="$1"
    local value="$2"
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] set $key=$value in .env"
        return 0
    fi
    if [ ! -f .env ]; then
        warn ".env missing; skip frontend freshness metadata persistence"
        return 0
    fi
    if grep -qE "^#?${key}=" .env; then
        local escaped
        escaped=$(printf '%s' "$value" | sed 's/[\/&]/\\&/g')
        sed -i "s/^#\\?${key}=.*/${key}=${escaped}/" .env
    else
        printf '\n%s=%s\n' "$key" "$value" >> .env
    fi
}

_env_file_value() {
    local key="$1"
    grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

_prod_database_url() {
    local url="${SQLALCHEMY_URL:-${OMNISIGHT_DATABASE_URL:-${DATABASE_URL:-}}}"
    if [ -z "$url" ]; then
        url="$(_env_file_value SQLALCHEMY_URL)"
    fi
    if [ -z "$url" ]; then
        url="$(_env_file_value OMNISIGHT_DATABASE_URL)"
    fi
    if [ -z "$url" ]; then
        url="$(_env_file_value DATABASE_URL)"
    fi
    case "$url" in
        postgresql://*|postgresql+*://*) printf '%s\n' "$url" ;;
        "") err "--alembic-mode=pg-clone requires SQLALCHEMY_URL, OMNISIGHT_DATABASE_URL, or DATABASE_URL in env/.env" ;;
        *) err "--alembic-mode=pg-clone requires a PostgreSQL URL, got scheme from configured DB URL" ;;
    esac
}

_db_name_from_url() {
    local url_no_query="${1%%\?*}"
    local db_name="${url_no_query##*/}"
    [ -n "$db_name" ] && [ "$db_name" != "$url_no_query" ] || \
        err "Could not parse database name from configured PostgreSQL URL"
    printf '%s\n' "$db_name"
}

_clone_url_for_db() {
    local url="$1"
    local clone_db="$2"
    local base="${url%%\?*}"
    local query=""
    if [ "$base" != "$url" ]; then
        query="?${url#*\?}"
    fi
    printf '%s/%s%s\n' "${base%/*}" "$clone_db" "$query"
}

_run_alembic_apply() {
    if ! docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
            -e PYTHONSAFEPATH=1 -w /app/backend \
            backend-a python -m alembic upgrade heads; then
        err "Alembic upgrade heads 失敗 — 中止部署。修正 migration 後重跑 deploy-prod.sh。"
    fi
    log "Alembic upgrade heads 完成"
}

_run_alembic_pg_clone() {
    local live_url live_db clone_db clone_url
    live_url="$(_prod_database_url)"
    live_db="$(_db_name_from_url "$live_url")"
    clone_db="${OMNISIGHT_ALEMBIC_CLONE_DB:-omnisight_alembic_dryrun_$(date +%Y%m%d%H%M%S)_$$}"
    clone_url="$(_clone_url_for_db "$live_url" "$clone_db")"

    step "Step 2.5: Alembic dry-validation on PG clone"
    echo "建立 live PG database 的一次性 clone，對 clone 執行 alembic upgrade heads；live DB 不套 migration。"

    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] docker exec omnisight-pg-primary drop/create/pg_dump/pg_restore clone '$clone_db' from '$live_db'"
        echo "  [dry-run] docker compose -f $COMPOSE_FILE run --rm --no-deps -e PYTHONSAFEPATH=1 -e SQLALCHEMY_URL=<clone-url> -w /app/backend backend-a python -m alembic upgrade heads"
        echo "  [dry-run] docker exec omnisight-pg-primary dropdb clone '$clone_db'"
        return 0
    fi

    docker inspect omnisight-pg-primary >/dev/null 2>&1 || \
        err "omnisight-pg-primary container not found; cannot create PG clone for Alembic validation."

    docker exec -e CLONE_DB="$clone_db" -e LIVE_DB="$live_db" omnisight-pg-primary sh -lc '
        set -eu
        dropdb --if-exists -U "$POSTGRES_USER" "$CLONE_DB"
        createdb -U "$POSTGRES_USER" "$CLONE_DB"
        pg_dump -U "$POSTGRES_USER" -d "$LIVE_DB" -Fc | pg_restore -U "$POSTGRES_USER" -d "$CLONE_DB" --no-owner
    ' || err "PG clone 建立失敗 — live DB 未修改。"

    if ! docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
            -e PYTHONSAFEPATH=1 \
            -e SQLALCHEMY_URL="$clone_url" \
            -w /app/backend \
            backend-a python -m alembic upgrade heads; then
        docker exec -e CLONE_DB="$clone_db" omnisight-pg-primary sh -lc \
            'dropdb --if-exists -U "$POSTGRES_USER" "$CLONE_DB"' >/dev/null 2>&1 || true
        err "Alembic PG clone dry-validation 失敗 — live DB 未修改。"
    fi

    docker exec -e CLONE_DB="$clone_db" omnisight-pg-primary sh -lc \
        'dropdb --if-exists -U "$POSTGRES_USER" "$CLONE_DB"' || \
        warn "PG clone '$clone_db' cleanup failed; drop it manually after inspection."
    log "Alembic PG clone dry-validation 完成（live DB 未修改）"
}

step "OmniSight Production 零停機部署"
echo "Compose: $COMPOSE_FILE"
echo "Branch:  ${TAG:-$BRANCH}"
echo "Alembic: $ALEMBIC_MODE"
echo ""

# ── Step 1: Pull latest code ──
step "Step 1: 拉取最新程式碼"

# FX.7.9: ref allowlist + GPG signature verification.
# Strict by default — the verifier aborts the deploy unless:
#   (1) the requested ref matches a rule in deploy/prod-deploy-allowlist.txt
#   (2) the tag (annotated) or branch-tip commit is GPG-signed by a
#       fingerprint listed in deploy/prod-deploy-signers.txt
# `--dry-run` runs only Layer 1 (ref doesn't need to be locally fetched).
# `--insecure-skip-verify` is an audit-trailed emergency escape hatch.
verify_args=()
if [ "$DRY_RUN" = true ]; then
    verify_args+=("--allowlist-only")
elif [ "$INSECURE_SKIP_VERIFY" = true ]; then
    verify_args+=("--insecure-skip-verify")
fi
GERRIT_SOURCE="$(_detect_gerrit_source)"
echo "Git source: $GERRIT_SOURCE"

if [ -n "$TAG" ]; then
    _run_cmd git fetch "$GERRIT_SOURCE" --tags
    scripts/check_deploy_ref.sh --kind tag --ref "$TAG" "${verify_args[@]}"
    _run_cmd git checkout "$TAG"
else
    _run_cmd git fetch "$GERRIT_SOURCE" "$BRANCH"
    scripts/check_deploy_ref.sh --kind branch --ref "$BRANCH" "${verify_args[@]}"
    _run_cmd git merge "$GERRIT_SOURCE/$BRANCH" --ff-only
fi
log "Code 更新完成：$(git log --oneline -1)"

# BP.W3.14: persist frontend build freshness metadata for the backend
# /metrics gauge + Bootstrap wizard L7 freshness panel. No shared
# module state: every worker reads the same .env values after recreate.
MASTER_HEAD_COMMIT="$(git rev-parse HEAD)"
if [ "$SKIP_BUILD" = false ]; then
    FRONTEND_BUILD_COMMIT="$MASTER_HEAD_COMMIT"
else
    FRONTEND_BUILD_COMMIT="$(grep -E '^OMNISIGHT_FRONTEND_BUILD_COMMIT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
fi
FRONTEND_BUILD_LAG_COMMITS=0
if [ -n "${FRONTEND_BUILD_COMMIT:-}" ]; then
    FRONTEND_BUILD_LAG_COMMITS="$(git rev-list --count "${FRONTEND_BUILD_COMMIT}..${MASTER_HEAD_COMMIT}" 2>/dev/null || echo 0)"
fi
_upsert_env "OMNISIGHT_MASTER_HEAD_COMMIT" "$MASTER_HEAD_COMMIT"
if [ -n "${FRONTEND_BUILD_COMMIT:-}" ]; then
    _upsert_env "OMNISIGHT_FRONTEND_BUILD_COMMIT" "$FRONTEND_BUILD_COMMIT"
fi
_upsert_env "OMNISIGHT_FRONTEND_BUILD_LAG_COMMITS" "$FRONTEND_BUILD_LAG_COMMITS"
log "Frontend freshness metadata: build=${FRONTEND_BUILD_COMMIT:-unknown} head=$MASTER_HEAD_COMMIT lag=$FRONTEND_BUILD_LAG_COMMITS"

# OP-772: expose current/previous image tags to the persistent SLO monitor
# before any replica is restarted. The monitor uses the previous tag as
# its rollback target if three consecutive 30 s SLO windows breach.
CURRENT_IMAGE_TAG="${OMNISIGHT_IMAGE_TAG:-${TAG:-$(git rev-parse --short=12 HEAD)}}"
PREVIOUS_IMAGE_TAG="$(grep -E '^OMNISIGHT_IMAGE_TAG=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
if [ -n "${PREVIOUS_IMAGE_TAG:-}" ] && [ "$PREVIOUS_IMAGE_TAG" != "$CURRENT_IMAGE_TAG" ]; then
    _upsert_env "OMNISIGHT_PREVIOUS_IMAGE_TAG" "$PREVIOUS_IMAGE_TAG"
fi
_upsert_env "OMNISIGHT_IMAGE_TAG" "$CURRENT_IMAGE_TAG"
log "SLO monitor image tags: current=$CURRENT_IMAGE_TAG previous=${PREVIOUS_IMAGE_TAG:-unknown}"

if [ "$DRY_RUN" = false ]; then
    if systemctl --user list-unit-files omnisight-slo-monitor.service >/dev/null 2>&1; then
        systemctl --user restart omnisight-slo-monitor.service || \
            warn "omnisight-slo-monitor.service restart failed before deploy — check systemd user logs"
    else
        warn "omnisight-slo-monitor.service not installed — install deploy/systemd/omnisight-slo-monitor.service for OP-772"
    fi
else
    echo "  [dry-run] systemctl --user restart omnisight-slo-monitor.service"
fi

# ── Step 1b: WAL-safe pre-deploy backup ──
# H2 audit (2026-04-19): rolling deploys can still roll BACKWARDS in
# data integrity if a migration blows up or a code change panics on
# existing rows. The `scripts/backup_prod_db.sh` helper takes a WAL-
# safe online snapshot + optional AES-256-GCM encryption (when
# OMNISIGHT_BACKUP_PASSPHRASE is set). Skipped in --dry-run.
if [ "$DRY_RUN" = false ]; then
    step "Step 1b: Pre-deploy backup"
    if [ -x scripts/backup_prod_db.sh ]; then
        scripts/backup_prod_db.sh --label pre-deploy --prune 20 || \
            err "pre-deploy backup failed — aborting to protect data. Re-run after investigating."
    else
        warn "scripts/backup_prod_db.sh missing — proceeding WITHOUT backup"
    fi
fi

# ── Step 2: Build (optional) ──
if [ "$SKIP_BUILD" = false ]; then
    step "Step 2: Build Docker images"
    _run "docker compose -f $COMPOSE_FILE build"
    log "Build 完成"
else
    step "Step 2: Skip build (--skip-build)"
    log "使用現有 image"
fi

# ── Step 2.5: Alembic migrations against live DB ──
# FX.9.5 (2026-05-04): apply pending Alembic migrations BEFORE the
# rolling restart so the new backend replica's /readyz doesn't fail on
# schema drift. Spawns an ephemeral container from the freshly-built
# backend image via `docker compose run --rm --no-deps` — backend-a /
# backend-b stay on the OLD image until Step 3 / Step 4 restart them,
# so the live request path is unaffected during migration.
#
# Knobs mirror bootstrap_prod.sh §4:
#   PYTHONSAFEPATH=1 — defence-in-depth against any future top-level
#                     project module shadowing a stdlib module under
#                     the alembic CLI's import order.
#   -w /app/backend  — alembic.ini's `script_location = alembic` is
#                     relative to invocation CWD (alembic quirk), not
#                     to the ini's own directory.
#   `upgrade heads` (plural) — defensive: works whether the tree is
#                     single-head (post-FX.9.4 merge) or transiently
#                     multi-head (e.g. mid-merge concurrent feature
#                     branches). `head` (singular) would bail with
#                     `Multiple head revisions are present`.
#
# `--no-deps` skips backend-a's `depends_on: docker-socket-proxy` —
# the migration container only needs PG (reachable via the
# `db_ha` external network attached to backend-a's service def);
# the docker-socket-proxy gate is a runtime concern for the long-
# lived replica, not for a one-shot alembic invocation.
#
# Failure semantics: if alembic exits non-zero we abort the deploy
# BEFORE touching either replica. The DB is left in whatever partial
# state alembic reached (Alembic wraps each migration in a tx so a
# single migration is atomic; a multi-migration batch may stop part-
# way and resume on the next run). Operator re-runs `deploy-prod.sh`
# after fixing the bad migration.
if [ "$ALEMBIC_MODE" = "apply" ]; then
    step "Step 2.5: Alembic upgrade heads"
    echo "在 rolling restart 之前用新 image 套 schema（FX.9.5 — 避免 readyz fail）..."
fi

if [ "$DRY_RUN" = false ]; then
    case "$ALEMBIC_MODE" in
        apply) _run_alembic_apply ;;
        pg-clone) _run_alembic_pg_clone ;;
    esac
else
    case "$ALEMBIC_MODE" in
        apply) echo "  [dry-run] docker compose -f $COMPOSE_FILE run --rm --no-deps backend-a python -m alembic upgrade heads" ;;
        pg-clone) _run_alembic_pg_clone ;;
    esac
fi

# ── Step 3: Rolling restart backend-a ──
step "Step 3: Rolling restart — backend-a"
echo "Caddy 會自動將流量切到 backend-b..."

_run "docker compose -f $COMPOSE_FILE up -d --no-deps backend-a"

echo -n "等待 backend-a readyz..."
if [ "$DRY_RUN" = false ]; then
    for i in $(seq 1 $HEALTH_RETRIES); do
        if docker compose -f $COMPOSE_FILE exec -T backend-a curl -sf http://localhost:8000/readyz >/dev/null 2>&1; then
            echo " ✅ (attempt $i)"
            break
        fi
        echo -n "."
        sleep $HEALTH_INTERVAL
        if [ "$i" -eq "$HEALTH_RETRIES" ]; then
            echo ""
            err "backend-a 未通過 readyz。請檢查：docker compose -f $COMPOSE_FILE logs backend-a"
        fi
    done
fi
log "backend-a 更新完成 + readyz 通過"

# ── Step 4: Rolling restart backend-b ──
step "Step 4: Rolling restart — backend-b"
echo "backend-a 已接管流量，重啟 backend-b..."

_run "docker compose -f $COMPOSE_FILE up -d --no-deps backend-b"

echo -n "等待 backend-b readyz..."
if [ "$DRY_RUN" = false ]; then
    for i in $(seq 1 $HEALTH_RETRIES); do
        if docker compose -f $COMPOSE_FILE exec -T backend-b curl -sf http://localhost:8001/readyz >/dev/null 2>&1; then
            echo " ✅ (attempt $i)"
            break
        fi
        echo -n "."
        sleep $HEALTH_INTERVAL
        if [ "$i" -eq "$HEALTH_RETRIES" ]; then
            echo ""
            err "backend-b 未通過 readyz。請檢查：docker compose -f $COMPOSE_FILE logs backend-b"
        fi
    done
fi
log "backend-b 更新完成 + readyz 通過"

# ── Step 5: Update frontend + caddy ──
step "Step 5: 更新 frontend + caddy"
_run "docker compose -f $COMPOSE_FILE up -d --no-deps frontend"
_run "docker compose -f $COMPOSE_FILE up -d --no-deps caddy"
log "Frontend + Caddy 更新完成"

# ── Step 6: Smoke test ──
step "Step 6: Smoke test"

if [ "$DRY_RUN" = false ]; then
    sleep 5
    HEALTH=$(curl -sf http://localhost:8000/api/v1/health 2>/dev/null || echo '{"status":"failed"}')
    STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "failed")

    if [ "$STATUS" = "online" ]; then
        log "Smoke test 通過：${HEALTH}"
    else
        err "Smoke test 失敗：${HEALTH}"
    fi
fi

# OP-772: restart the persistent SLO monitor to cover the full post-deploy hour.
if [ "$DRY_RUN" = false ]; then
    if systemctl --user list-unit-files omnisight-slo-monitor.service >/dev/null 2>&1; then
        systemctl --user restart omnisight-slo-monitor.service || \
            warn "omnisight-slo-monitor.service restart failed — check systemd user logs"
    else
        warn "omnisight-slo-monitor.service not installed — install deploy/systemd/omnisight-slo-monitor.service for OP-772"
    fi
else
    echo "  [dry-run] systemctl --user restart omnisight-slo-monitor.service"
fi

# ── Done ──
step "🎉 零停機部署完成！"
echo ""
echo -e "${BOLD}部署摘要：${NC}"
echo "  Version:  $(git describe --tags --always 2>/dev/null || git log --oneline -1)"
echo "  Backend:  backend-a :8000 + backend-b :8001"
echo "  Frontend: :3000"
echo "  Caddy:    :443 → round-robin"
echo "  Status:   $(curl -sf http://localhost:8000/api/v1/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo 'checking...')"
echo ""
echo -e "${BOLD}Rollback：${NC}"
echo "  git checkout <previous-tag>"
echo "  $0 --skip-build"
