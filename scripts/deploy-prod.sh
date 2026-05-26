#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OmniSight — Production 零停機部署腳本
#
# 在 Ubuntu-24.04 (Production WSL) 上執行
# 實現 G2 rolling restart：一次只重啟一個 backend replica
#
# 使用方式（RT-20 — 單一主幹 release train，IMAGE-TAG-ONLY）：
# 一次 production 部署只接受「最終發行身分」：cosign 驗證過的 image DIGEST。
# 不再有 branch 部署、不再 default 到 main，也不再有 v* git tag 部署 ——
# 一個 v* git tag 會觸發既有的 ^v CI build rule 重建出「不同的」digest，
# 破壞「validated digest == shipped digest」(RT-20, ADR-0040)。所以 --tag
# 被 scripts/check_deploy_ref.sh 直接拒絕（並指回下面的 digest 指令）；
# 改用 promote pipeline 產出的 image digest 部署：
#   # Digest deploy pins BOTH images — backend + frontend are SEPARATE
#   # images, each with its own digest, so one digest cannot pin the pair
#   # (OP-1696). Pass per-image digests:
#   ./scripts/deploy-prod.sh --backend-digest=sha256:<64hex> \
#                            --frontend-digest=sha256:<64hex>
#   # (--digest=sha256:<64hex> is kept as a back-compat alias for
#   #  --backend-digest; on its own it pins ONLY the backend — pass
#   #  --frontend-digest too for a fully digest-pinned deploy.)
#   ./scripts/deploy-prod.sh --backend-digest=sha256:<64hex> \
#                            --frontend-digest=sha256:<64hex> --dry-run
#                                                    # 只印步驟不執行
#   ./scripts/deploy-prod.sh --backend-digest=sha256:<64hex> \
#                            --frontend-digest=sha256:<64hex> --alembic-mode=pg-clone
#                                                    # 在 PG clone 上 dry-validate migrations
#   # (digest 部署本就 --skip-build：image 已建好並 cosign 驗證過，
#   #  沒有 git ref 要 fetch/checkout、沒有 source 要 build。)
#
# 沒有 --insecure-skip-verify escape hatch（RT-07a 移除）：授權新發行身分的
# 唯一稽核途徑，是部署 promote pipeline 產出、cosign 驗證過的 image digest。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
TAG=""
# OP-1696: a digest deploy pins the backend + frontend PAIR — they are
# SEPARATE images with distinct digests, so one digest cannot pin both.
# --digest is retained as a back-compat alias for --backend-digest.
BACKEND_DIGEST=""
FRONTEND_DIGEST=""
SKIP_BUILD=false
DRY_RUN=false
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
        --digest=*) BACKEND_DIGEST="${arg#*=}" ;;            # OP-1696: back-compat alias for --backend-digest
        --backend-digest=*) BACKEND_DIGEST="${arg#*=}" ;;
        --frontend-digest=*) FRONTEND_DIGEST="${arg#*=}" ;;
        --skip-build) SKIP_BUILD=true ;;
        --dry-run) DRY_RUN=true ;;
        --alembic-mode=*) ALEMBIC_MODE="${arg#*=}" ;;
        --alembic-pg-clone) ALEMBIC_MODE="pg-clone" ;;
        --help|-h)
            echo "Usage: $0 --backend-digest=sha256:<64hex> --frontend-digest=sha256:<64hex> [--skip-build] [--dry-run] [--alembic-mode=apply|pg-clone]"
            echo "       (--digest=sha256:<64hex> is a back-compat alias for --backend-digest; pass both --backend-digest and --frontend-digest to pin both images by digest)"
            echo "       RT-20 (image-tag-only): --tag/v* git-tag deploys are retired — deploy the promoted image's validated digest instead (the line above)."
            exit 0 ;;
        *) err "Unknown argument: $arg" ;;
    esac
done

# RT-20 (image-tag-only): the SOLE production deploy identity is a
# cosign-verified image DIGEST. A v* git tag is never a deploy identity
# (no v* git tag is created — it would trip the ^v CI build rule and
# rebuild a DIFFERENT digest), and branch deploys + the implicit main
# default were removed in RT-07a. --tag is still parsed (below) only so an
# accidental tag deploy gets a clear, actionable digest pointer instead of
# an opaque arg error. The shape of each digest is enforced by
# check_deploy_ref.sh.
# OP-1696: a digest deploy is requested when either per-image digest is
# given. The backend + frontend are SEPARATE images with distinct digests,
# so a single digest cannot pin the PAIR — the fully digest-pinned (cosign-
# verified) deploy passes BOTH --backend-digest and --frontend-digest. A
# single digest is accepted for back-compat (it pins only that image and is
# warned about in Step 1), but the implicit main default + branch deploys
# remain removed (RT-07a).
DIGEST_DEPLOY=false
if [ -n "$BACKEND_DIGEST" ] || [ -n "$FRONTEND_DIGEST" ]; then
    DIGEST_DEPLOY=true
fi

if [ -n "$TAG" ] && [ "$DIGEST_DEPLOY" = true ]; then
    err "--tag and --digest/--backend-digest/--frontend-digest are mutually exclusive — a deploy has exactly one final identity"
fi
if [ -z "$TAG" ] && [ "$DIGEST_DEPLOY" = false ]; then
    err "release-train (RT-20, image-tag-only): a final deploy identity is required — pass a digest deploy pinning BOTH images: --backend-digest=sha256:<64hex> --frontend-digest=sha256:<64hex>. (--tag/v* git-tag deploys are retired; branch deploys and the implicit main default were removed in RT-07a.)"
fi

# RT-20 (image-tag-only): --tag is NOT a production deploy identity. No v*
# git tag is ever created, and check_deploy_ref.sh rejects --kind tag
# unconditionally — so deploy-prod.sh once advertised --tag as the primary
# path while it ALWAYS errored (an operator hit this live during v0.6.2,
# OP-1734). Reject it up-front via the same RT-20 gate, which now prints an
# actionable pointer to the digest deploy command. Doing it here — before
# the deploy banner, the pre-deploy backup, and the build — means the dead
# tag path's `git fetch --tags` / `git checkout` are never reached (F7):
# under RT-20 the only live deploy is the digest path below, which pulls a
# pre-built, cosign-verified image and does no git checkout at all.
if [ -n "$TAG" ]; then
    scripts/check_deploy_ref.sh --kind tag --ref "$TAG"   # always exits non-zero (RT-20)
fi

# Single human-readable identity for the banner / summary / SLO-monitor record.
if [ -n "$TAG" ]; then
    DEPLOY_ID="$TAG"
else
    DEPLOY_ID="${BACKEND_DIGEST:+backend@$BACKEND_DIGEST }${FRONTEND_DIGEST:+frontend@$FRONTEND_DIGEST}"
    DEPLOY_ID="${DEPLOY_ID% }"   # trim trailing space when only backend is pinned
fi

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
echo "Deploy:  $DEPLOY_ID"
echo "Alembic: $ALEMBIC_MODE"
echo ""

# ── Step 1: Pull latest code ──
step "Step 1: 拉取最新程式碼"

# RT-07a: gate the deploy identity via check_deploy_ref.sh before
# touching the working tree or any replica. The verifier enforces the
# release-train contract — final tag (vX.Y.Z) or image digest only,
# never a branch. `--dry-run` runs the shape gate (+ allowlist for tags)
# without needing the ref fetched locally. There is no insecure bypass.
verify_args=()
if [ "$DRY_RUN" = true ]; then
    verify_args+=("--allowlist-only")
fi

if [ "$DIGEST_DEPLOY" = true ]; then
    # Digest deploy: the images are already built and cosign-verified at
    # these digests (RT-06), so there is no git ref to fetch or check out.
    # Force --skip-build — the image carries the code + Alembic migrations
    # (the alembic step below runs inside that image, not the working tree).
    #
    # OP-1696: pin the backend + frontend PAIR by per-image digest. They are
    # SEPARATE images, so gate and pin each side independently and hand
    # compose full content-addressed refs (OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF
    # = <registry>/<image>@sha256:...). docker-compose.prod.yml consumes those
    # refs and falls back to the :${OMNISIGHT_IMAGE_TAG} tag path when they
    # are unset/empty, so the tag deploy path is untouched. A side that was
    # NOT given a digest is cleared (empty) so it deploys via the tag path.
    SKIP_BUILD=true

    # Registry prefix mirrors docker-compose.prod.yml's tag path
    # (${OMNISIGHT_REGISTRY}/<image>); resolve from env or .env so the
    # @sha256 refs address the same registry compose would have pulled from.
    REGISTRY="${OMNISIGHT_REGISTRY:-$(_env_file_value OMNISIGHT_REGISTRY)}"
    if [ -z "$REGISTRY" ]; then
        if [ "$DRY_RUN" = true ]; then
            REGISTRY='${OMNISIGHT_REGISTRY}'   # placeholder; a real deploy resolves it from env/.env
        else
            err "OMNISIGHT_REGISTRY is required to build per-image digest refs (set it in env or .env, mirroring docker-compose.prod.yml)."
        fi
    fi

    if [ -n "$BACKEND_DIGEST" ]; then
        scripts/check_deploy_ref.sh --kind digest --ref "$BACKEND_DIGEST" "${verify_args[@]}"
        _upsert_env "OMNISIGHT_BACKEND_IMAGE_REF" "${REGISTRY}/backend@${BACKEND_DIGEST}"
    else
        _upsert_env "OMNISIGHT_BACKEND_IMAGE_REF" ""
    fi
    if [ -n "$FRONTEND_DIGEST" ]; then
        scripts/check_deploy_ref.sh --kind digest --ref "$FRONTEND_DIGEST" "${verify_args[@]}"
        _upsert_env "OMNISIGHT_FRONTEND_IMAGE_REF" "${REGISTRY}/frontend@${FRONTEND_DIGEST}"
    else
        _upsert_env "OMNISIGHT_FRONTEND_IMAGE_REF" ""
    fi

    # A fully digest-pinned deploy pins BOTH images. Warn loudly when only
    # one side is pinned: the other falls back to the mutable :${OMNISIGHT_IMAGE_TAG}
    # path, which is NOT a content-addressed (cosign-verified) deploy.
    if [ -z "$BACKEND_DIGEST" ] || [ -z "$FRONTEND_DIGEST" ]; then
        warn "OP-1696: only one image is pinned by digest ($DEPLOY_ID); the other deploys via the mutable :\${OMNISIGHT_IMAGE_TAG} tag path. For a fully digest-pinned (cosign-verified) deploy, pass BOTH --backend-digest and --frontend-digest."
    fi
    log "Digest deploy: $DEPLOY_ID (skip-build；不碰 git 工作樹)"
fi
# NOTE (OP-1734): there is no `else` git-deploy branch any more. RT-20 is
# image-tag-only, so the only live deploy identity is the digest path
# above (DIGEST_DEPLOY is always true here — a --tag invocation already
# exited at the RT-20 gate). The retired tag path's gerrit-source detect,
# `git fetch --tags`, and `git checkout` are gone (F7); a digest deploy
# pulls a pre-built, cosign-verified image and never touches the git tree.

# OP-772: expose current/previous image tags to the persistent SLO monitor
# before any replica is restarted. The monitor uses the previous tag as
# its rollback target if three consecutive 30 s SLO windows breach.
CURRENT_IMAGE_TAG="${OMNISIGHT_IMAGE_TAG:-${TAG:-${BACKEND_DIGEST:-$FRONTEND_DIGEST}}}"
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
echo "  Version:  $DEPLOY_ID"
echo "  Backend:  backend-a :8000 + backend-b :8001"
echo "  Frontend: :3000"
echo "  Caddy:    :443 → round-robin"
echo "  Status:   $(curl -sf http://localhost:8000/api/v1/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo 'checking...')"
echo ""
echo -e "${BOLD}Rollback（release-train RT-20 — redeploy the previous validated digest）：${NC}"
echo "  $0 --backend-digest=sha256:<prev-64hex> --frontend-digest=sha256:<prev-64hex>"
echo "  # (--tag rollback is retired under RT-20 image-tag-only; redeploy the"
echo "  #  previous release's validated backend + frontend image digests.)"
