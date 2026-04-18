#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OmniSight — Development + Testing 環境一鍵配置
#
# 用途：在新的 WSL instance 上快速建立完整開發/測試環境
# 設計為可攜——當 Ubuntu 26.04 釋出後，在新 WSL 跑這個腳本即可
#
# 使用方式：
#   chmod +x scripts/setup-dev-env.sh
#   ./scripts/setup-dev-env.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}✅${NC} $*"; }
warn() { echo -e "${YELLOW}⚠️${NC}  $*"; }
err()  { echo -e "${RED}❌${NC} $*"; }
step() { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}\n"; }

step "OmniSight Development + Testing 環境配置"
echo "此腳本可在任何新的 Ubuntu WSL 上執行（24.04 / 26.04 +）"
echo ""

# ── Step 1: 系統套件 ──
step "Step 1: 安裝系統套件"

sudo apt-get update -qq
sudo apt-get install -y -qq \
    git curl wget jq \
    python3 python3-pip python3-venv \
    build-essential libffi-dev libssl-dev \
    docker.io docker-compose-v2 \
    2>/dev/null

# Docker group
if ! groups | grep -q docker; then
    sudo usermod -aG docker "$USER"
    warn "已加入 docker group。請執行 'newgrp docker' 或重新登入 WSL。"
fi

# Docker service
if ! systemctl is-active --quiet docker 2>/dev/null; then
    sudo systemctl start docker 2>/dev/null || warn "Docker 未以 systemd 啟動，可能需手動啟動"
    sudo systemctl enable docker 2>/dev/null || true
fi
log "Docker $(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' || echo 'unknown')"

# ── Step 2: Node.js (via nvm) ──
step "Step 2: Node.js"

if ! command -v node &>/dev/null; then
    if [ ! -d "$HOME/.nvm" ]; then
        curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
        export NVM_DIR="$HOME/.nvm"
        # shellcheck source=/dev/null
        [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
    fi
    nvm install --lts
    nvm use --lts
fi
log "Node $(node --version) + npm $(npm --version)"

# pnpm (if project uses it)
if [ -f "pnpm-lock.yaml" ] && ! command -v pnpm &>/dev/null; then
    npm install -g pnpm
    log "pnpm $(pnpm --version)"
fi

# ── Step 3: Python 依賴 ──
step "Step 3: Python 依賴"

if [ -f "backend/requirements.in" ]; then
    pip install --user -r backend/requirements.in 2>/dev/null || \
        pip install -r backend/requirements.in
    log "Python 依賴安裝完成"
fi

# ── Step 4: Node 依賴 ──
step "Step 4: Node 依賴"

if [ -f "package.json" ]; then
    if [ -f "pnpm-lock.yaml" ]; then
        pnpm install --frozen-lockfile 2>/dev/null || pnpm install
    else
        npm install
    fi
    log "Node 依賴安裝完成"
fi

# ── Step 5: 環境檔案 ──
step "Step 5: 環境設定"

# Development .env
if [ ! -f ".env" ]; then
    cp .env.example .env
    # Development defaults
    cat >> .env <<'DEVENV'

# ─── Development overrides ───
OMNISIGHT_DEBUG=true
# Development 用 ollama 或留空（rule-based fallback）
# OMNISIGHT_LLM_PROVIDER=ollama
DEVENV
    log ".env 建立完成（Development 模式）"
else
    log ".env 已存在"
fi

# Testing .env
if [ ! -f ".env.test" ]; then
    cat > .env.test <<'TESTENV'
# OmniSight Testing 環境
# pytest 自動使用 tmp_path DB，此檔僅供手動測試用
OMNISIGHT_DEBUG=true
OMNISIGHT_AUTH_MODE=open
OMNISIGHT_DATABASE_PATH=/tmp/omnisight-test.db
OMNISIGHT_LLM_PROVIDER=ollama
TESTENV
    log ".env.test 建立完成"
fi

# ── Step 6: Git hooks ──
step "Step 6: Git hooks (pre-commit test)"

if [ ! -f ".git/hooks/pre-commit" ]; then
    cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/bash
# Quick sanity before commit — runs core tests only (~10s)
echo "🧪 Pre-commit: running core tests..."
python3 -m pytest backend/tests/test_db.py backend/tests/test_config.py \
    backend/tests/test_auth.py -q --tb=line 2>&1 | tail -3
exit $?
HOOK
    chmod +x .git/hooks/pre-commit
    log "pre-commit hook 安裝完成（core tests）"
fi

# ── Step 7: 便利 alias ──
step "Step 7: Shell aliases"

ALIAS_FILE="$HOME/.omnisight_aliases"
cat > "$ALIAS_FILE" <<'ALIASES'
# OmniSight Development 快捷指令
alias os-dev='cd ~/work/sora/OmniSight-Productizer && NODE_OPTIONS="--max-old-space-size=4096" npm run dev'
alias os-api='cd ~/work/sora/OmniSight-Productizer && OMNISIGHT_DEBUG=true python3 -m uvicorn backend.main:app --port 8002 --reload'
alias os-test='cd ~/work/sora/OmniSight-Productizer && python3 -m pytest backend/tests/ --ignore=backend/tests/test_ssh_runner.py -q --tb=line'
alias os-test-quick='cd ~/work/sora/OmniSight-Productizer && python3 -m pytest backend/tests/test_db.py backend/tests/test_config.py backend/tests/test_auth.py -q'
alias os-auto='cd ~/work/sora/OmniSight-Productizer && python3 auto-runner.py'
alias os-status='echo "Backend:" && curl -s http://localhost:8002/api/v1/health 2>/dev/null || echo "DOWN"; echo "Frontend:" && curl -s -o /dev/null -w "%{http_code}" http://localhost:3001 2>/dev/null || echo "DOWN"'
ALIASES

# Add to .bashrc if not already there
if ! grep -q "omnisight_aliases" "$HOME/.bashrc" 2>/dev/null; then
    echo "" >> "$HOME/.bashrc"
    echo "# OmniSight Development" >> "$HOME/.bashrc"
    echo "[ -f $ALIAS_FILE ] && source $ALIAS_FILE" >> "$HOME/.bashrc"
fi
log "Shell aliases 安裝完成：os-dev / os-api / os-test / os-test-quick / os-auto / os-status"

# ── Step 8: 驗證 ──
step "Step 8: 環境驗證"

echo -n "Python: " && python3 --version
echo -n "Node:   " && node --version 2>/dev/null || echo "未安裝"
echo -n "Docker: " && docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' || echo "未安裝"
echo -n "Git:    " && git --version | grep -oP '\d+\.\d+\.\d+'

# Quick import test
python3 -c "from backend import config; print(f'Backend import: ✅ ({config.settings.app_name})')" 2>/dev/null || warn "Backend import 失敗"

echo ""
step "🎉 Development + Testing 環境配置完成！"
echo ""
echo -e "${BOLD}Development 使用方式：${NC}"
echo "  Terminal 1: os-api          # Backend on :8002"
echo "  Terminal 2: os-dev          # Frontend on :3001"
echo "  Browser:    http://localhost:3001"
echo ""
echo -e "${BOLD}Testing 使用方式：${NC}"
echo "  os-test-quick              # Core tests (~10s)"
echo "  os-test                    # Full suite (~5min)"
echo "  os-auto                    # Auto-runner pipeline"
echo ""
echo -e "${BOLD}遷移到新 WSL：${NC}"
echo "  1. wsl --install -d Ubuntu-26.04"
echo "  2. git clone <repo> ~/work/sora/OmniSight-Productizer"
echo "  3. cd ~/work/sora/OmniSight-Productizer"
echo "  4. ./scripts/setup-dev-env.sh"
echo "  → 完成！所有工具 + alias + hook 自動配好"
