#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OmniSight — Production 環境清理腳本
#
# 開發環境遷移到 Ubuntu-26.04 後，在 Ubuntu-24.04 上執行此腳本
# 移除所有開發用 artifacts，讓 production WSL 保持乾淨
#
# 使用方式：
#   ./scripts/clean-prod-env.sh
#   ./scripts/clean-prod-env.sh --dry-run   # 只印會刪什麼
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}✅${NC} $*"; }
warn() { echo -e "${YELLOW}⚠️${NC}  $*"; }

_rm() {
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] rm -rf $*"
    else
        rm -rf "$@" 2>/dev/null && log "移除: $*" || true
    fi
}

echo -e "${BOLD}OmniSight Production 環境清理${NC}"
echo "此腳本移除開發用 artifacts，保留 Docker production 所需的一切。"
echo ""

if [ "$DRY_RUN" = true ]; then
    warn "[Dry-run 模式] 只顯示會刪除的內容，不實際執行"
    echo ""
fi

echo -e "${BOLD}將移除：${NC}"

# Node.js development artifacts
_rm node_modules
_rm .next
_rm .turbo

# Python development artifacts
_rm backend/__pycache__
_rm backend/tests/__pycache__
find . -name "*.pyc" -delete 2>/dev/null || true
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Test artifacts
_rm .pytest_cache
_rm htmlcov
_rm coverage
_rm .coverage

# Dev environment files (keep .env for production)
_rm .env.test
_rm .env.dev

# Temp files
_rm /tmp/omnisight-*

# Editor / IDE
_rm .vscode
_rm .idea

echo ""
echo -e "${BOLD}保留：${NC}"
echo "  ✅ docker-compose.prod.yml"
echo "  ✅ Dockerfile.backend + Dockerfile.frontend"
echo "  ✅ deploy/ (Caddyfile + systemd + cloudflared)"
echo "  ✅ .env (production secrets)"
echo "  ✅ scripts/deploy-prod.sh + quick-start.sh"
echo "  ✅ .git (版本控制)"
echo "  ✅ data/ (production DB — Docker volume)"
echo ""

if [ "$DRY_RUN" = false ]; then
    # Verify Docker production still works
    echo -e "${BOLD}驗證 Production 服務狀態：${NC}"
    if docker compose -f docker-compose.prod.yml ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null; then
        log "Docker services 正常"
    else
        warn "Docker services 未運行（可能需要 docker compose up -d）"
    fi
fi

echo ""
log "清理完成。此 WSL 現在是乾淨的 Production 環境。"
echo ""
echo -e "${BOLD}日常操作：${NC}"
echo "  部署更新：  ./scripts/deploy-prod.sh"
echo "  查看狀態：  docker compose -f docker-compose.prod.yml ps"
echo "  查看日誌：  docker compose -f docker-compose.prod.yml logs -f"
echo "  零停機升級：./scripts/deploy-prod.sh --tag v1.x.x"
