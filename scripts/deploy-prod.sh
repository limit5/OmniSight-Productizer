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
        --insecure-skip-verify) INSECURE_SKIP_VERIFY=true ;;
        --help|-h)
            echo "Usage: $0 [--branch=main] [--tag=v1.2.0] [--skip-build] [--dry-run] [--insecure-skip-verify]"
            exit 0 ;;
    esac
done

_run() {
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] $*"
    else
        eval "$@"
    fi
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

step "OmniSight Production 零停機部署"
echo "Compose: $COMPOSE_FILE"
echo "Branch:  ${TAG:-$BRANCH}"
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

if [ -n "$TAG" ]; then
    _run "git fetch origin --tags"
    scripts/check_deploy_ref.sh --kind tag --ref "$TAG" "${verify_args[@]}"
    _run "git checkout '$TAG'"
else
    _run "git fetch origin $BRANCH"
    scripts/check_deploy_ref.sh --kind branch --ref "$BRANCH" "${verify_args[@]}"
    _run "git merge origin/$BRANCH --ff-only"
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
step "Step 2.5: Alembic upgrade heads"
echo "在 rolling restart 之前用新 image 套 schema（FX.9.5 — 避免 readyz fail）..."

if [ "$DRY_RUN" = false ]; then
    if ! docker compose -f $COMPOSE_FILE run --rm --no-deps \
            -e PYTHONSAFEPATH=1 -w /app/backend \
            backend-a python -m alembic upgrade heads; then
        err "Alembic upgrade heads 失敗 — 中止部署。修正 migration 後重跑 deploy-prod.sh。"
    fi
    log "Alembic upgrade heads 完成"
else
    echo "  [dry-run] docker compose -f $COMPOSE_FILE run --rm --no-deps backend-a python -m alembic upgrade heads"
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
