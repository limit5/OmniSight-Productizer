#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OmniSight Productizer — 一鍵佈署腳本
# 適用：WSL2 本地主機 + Cloudflare Tunnel + GoDaddy 域名
#
# 使用方式：
#   chmod +x scripts/quick-start.sh
#   ./scripts/quick-start.sh
#   ./scripts/quick-start.sh --dry-run     # 只檢查，不執行
#   ./scripts/quick-start.sh --uninstall   # 清除容器+volumes
#
# 本腳本處理：
#   1. 前置條件檢查（Docker / Docker Compose / WSL2 systemd）
#   2. .env 自動生成（互動式問答）
#   3. Docker 容器啟動（prod 模式）
#   4. 等待 backend/frontend 健康檢查
#   5. Cloudflare Tunnel 自動建立（API token → tunnel → DNS CNAME）
#   6. cloudflared connector 安裝 + 啟動
#   7. GoDaddy NS 遷移指引（一次性手動步驟）
#   8. Bootstrap wizard 開啟
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail
# pipefail: makes `cmd | tee | tail` reflect cmd's exit code — critical so
# a failing `docker compose up` isn't masked by the trailing pager commands.

# ── 可設定參數 ──
# All three values can be overridden via environment variables so users
# running side-by-side clusters (e.g. staging + prod on one CF account) can
# use distinct domains / subdomains / tunnel names without forking the
# script. Empty string → fall back to default; whitespace is stripped before
# validation so `OMNISIGHT_DOMAIN=" foo.com "` in a .env file doesn't break.
# See _validate_domain / _validate_api_subdomain / _validate_tunnel_name
# below — invalid values exit early at Step 0 with a clear diagnostic.
DOMAIN="${OMNISIGHT_DOMAIN:-sora-dev.app}"
API_SUBDOMAIN="${OMNISIGHT_API_SUBDOMAIN:-api}"
TUNNEL_NAME="${OMNISIGHT_TUNNEL_NAME:-omnisight-prod}"
COMPOSE_FILE="docker-compose.prod.yml"
BACKEND_PORT=8000
FRONTEND_PORT=3000
HEALTH_RETRIES=45
HEALTH_INTERVAL=4
MIN_DISK_GB=5
LOG_FILE="/tmp/omnisight-quick-start-$(date +%Y%m%d-%H%M%S).log"
DRY_RUN=false
UNINSTALL=false
NON_INTERACTIVE=false
# detect non-TTY (CI, piped stdin) so interactive `read` doesn't silently
# consume empty lines and leave .env misconfigured.
if [ ! -t 0 ] || [ ! -t 1 ]; then
    NON_INTERACTIVE=true
fi

# ── 顏色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}✅${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}⚠️${NC}  $*" | tee -a "$LOG_FILE"; }
err()  { echo -e "${RED}❌${NC} $*" | tee -a "$LOG_FILE"; }
step() { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}\n" | tee -a "$LOG_FILE"; }

# Strip leading/trailing whitespace — users sometimes paste env values with
# stray spaces (especially from copy-paste of dashboard snippets), and we
# should silently tolerate that rather than emit a cryptic CF-API error.
_strip_ws() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# Accept only valid FQDNs (lowercase letters/digits/hyphens, no scheme, no
# path, at least one dot, ≤253 chars total, each label 1-63 chars, no leading
# or trailing hyphen per RFC 1035). Rejects URLs, single-label hostnames, and
# whitespace-riddled values — any of which would flow straight into CF API
# calls + CNAME values and produce silent corruption 40 seconds into the run.
_validate_domain() {
    local d="$1"
    [ -z "$d" ] && { err "OMNISIGHT_DOMAIN 不可為空"; return 1; }
    if [ "${#d}" -gt 253 ]; then
        err "域名長度超過 253 字元（RFC 1035 上限）：${d}"
        return 1
    fi
    # No scheme / no slashes / no whitespace / no uppercase — catch the most
    # common paste-errors first with a friendly message before the regex.
    case "$d" in
        *://*|*/*|*\ *|*$'\t'*)
            err "OMNISIGHT_DOMAIN 含無效字元（不可包含 ://、/、或空白）：'${d}'"
            err "  範例正確格式：sora-dev.app 或 app.example.com"
            return 1
            ;;
    esac
    if [ "$d" != "$(echo "$d" | tr '[:upper:]' '[:lower:]')" ]; then
        err "OMNISIGHT_DOMAIN 必須全小寫：'${d}'"
        return 1
    fi
    # RFC 1035-ish label regex; requires at least one dot so single-label
    # values like `localhost` are rejected early (CF would reject them anyway
    # but the error would land deep inside Step 4).
    if ! [[ "$d" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]]; then
        err "OMNISIGHT_DOMAIN 格式無效：'${d}'"
        err "  需符合 FQDN 格式（例：sora-dev.app、app.example.com），至少含一個 '.'"
        return 1
    fi
    return 0
}

# API subdomain is a single DNS label (1-63 chars, [a-z0-9-], no leading/
# trailing hyphen). Empty string is allowed via the `:-api` default earlier
# but a user-provided override must still be a valid label.
_validate_api_subdomain() {
    local s="$1"
    [ -z "$s" ] && { err "OMNISIGHT_API_SUBDOMAIN 不可為空"; return 1; }
    if ! [[ "$s" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
        err "OMNISIGHT_API_SUBDOMAIN 格式無效：'${s}'（需為單一 DNS label，例：api、v2、app）"
        return 1
    fi
    return 0
}

# CF tunnel names: letters, digits, hyphens, underscores — max 32 chars per
# CF's dashboard UI. CF API is actually more permissive but we mirror the UI
# so the tunnel shows up cleanly in the CF dashboard for the user.
_validate_tunnel_name() {
    local t="$1"
    [ -z "$t" ] && { err "OMNISIGHT_TUNNEL_NAME 不可為空"; return 1; }
    if [ "${#t}" -gt 32 ]; then
        err "OMNISIGHT_TUNNEL_NAME 超過 32 字元上限：'${t}'"
        return 1
    fi
    if ! [[ "$t" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        err "OMNISIGHT_TUNNEL_NAME 格式無效：'${t}'（僅允許字母、數字、連字號、底線）"
        return 1
    fi
    return 0
}

# Prompt for a secret without echoing to screen. Falls back to plain read
# when stdin isn't a TTY (e.g. CI) — there the caller feeds the value in
# pre-redacted anyway.
read_secret() {
    local prompt="$1" __var="$2" __tmp=""
    if [ -t 0 ]; then
        read -rsp "$prompt" __tmp
        echo ""  # newline after silent read
    else
        read -r __tmp
    fi
    printf -v "$__var" '%s' "$__tmp"
}

# ── CLI 參數 ──
for arg in "$@"; do
    case "$arg" in
        --dry-run)  DRY_RUN=true ;;
        --uninstall) UNINSTALL=true ;;
        --help|-h)
            cat <<EOF
用法: $0 [--dry-run] [--uninstall]
  --dry-run    只檢查前置條件，不實際執行
  --uninstall  清除所有 OmniSight 容器、volumes、cloudflared

環境變數（全部可選，用於覆寫預設值）：
  OMNISIGHT_DOMAIN         對外主域名                   [預設: sora-dev.app]
  OMNISIGHT_API_SUBDOMAIN  API 子網域（會變成 X.DOMAIN）[預設: api]
  OMNISIGHT_TUNNEL_NAME    Cloudflare Tunnel 名稱        [預設: omnisight-prod]

範例：
  OMNISIGHT_DOMAIN=app.example.com \\
      OMNISIGHT_TUNNEL_NAME=omnisight-staging $0
EOF
            exit 0
            ;;
    esac
done

# ── 持久化 log ──
mkdir -p "$(dirname "$LOG_FILE")"
echo "OmniSight quick-start log — $(date)" > "$LOG_FILE"
log "日誌輸出至：$LOG_FILE"

# ── 覆寫參數驗證 + 設定摘要 ──
# Runs after LOG_FILE exists so err() has somewhere to tee. Fails fast here
# (before any Docker / CF work) so a typo like OMNISIGHT_DOMAIN=https://foo.com
# doesn't produce a cryptic error 40 seconds in.
DOMAIN="$(_strip_ws "$DOMAIN")"
API_SUBDOMAIN="$(_strip_ws "$API_SUBDOMAIN")"
TUNNEL_NAME="$(_strip_ws "$TUNNEL_NAME")"
_validate_domain "$DOMAIN" || exit 1
_validate_api_subdomain "$API_SUBDOMAIN" || exit 1
_validate_tunnel_name "$TUNNEL_NAME" || exit 1

# Echo the three knobs back so operators immediately see what they'll deploy.
# This is the only place a mistyped OMNISIGHT_DOMAIN gets a second chance to
# be caught by eyeballs before CF API side-effects kick in.
echo -e "${BOLD}部署設定：${NC}" | tee -a "$LOG_FILE"
echo "  Domain:        ${DOMAIN}" | tee -a "$LOG_FILE"
echo "  API subdomain: ${API_SUBDOMAIN}.${DOMAIN}" | tee -a "$LOG_FILE"
echo "  Tunnel name:   ${TUNNEL_NAME}" | tee -a "$LOG_FILE"
if [ "${OMNISIGHT_DOMAIN:-}" = "" ] && [ "${OMNISIGHT_API_SUBDOMAIN:-}" = "" ] && [ "${OMNISIGHT_TUNNEL_NAME:-}" = "" ]; then
    echo "  （全部為預設值；可透過 OMNISIGHT_DOMAIN / OMNISIGHT_API_SUBDOMAIN / OMNISIGHT_TUNNEL_NAME 覆寫，詳見 --help）" | tee -a "$LOG_FILE"
fi

# ── Ctrl+C 清理 ──
_cleanup_on_exit() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "" | tee -a "$LOG_FILE"
        err "腳本在 Step 執行中被中斷或發生錯誤 (exit code: $exit_code)"
        echo "" | tee -a "$LOG_FILE"
        echo -e "${BOLD}診斷建議：${NC}" | tee -a "$LOG_FILE"
        echo "  1. 查看日誌：cat $LOG_FILE" | tee -a "$LOG_FILE"
        echo "  2. 查看容器狀態：docker compose -f $COMPOSE_FILE ps" | tee -a "$LOG_FILE"
        echo "  3. 查看容器日誌：docker compose -f $COMPOSE_FILE logs --tail 50" | tee -a "$LOG_FILE"
        echo "  4. 重新執行此腳本：問題修復後重跑即可（腳本支援冪等）" | tee -a "$LOG_FILE"
    fi
}
trap _cleanup_on_exit EXIT

# ── Uninstall 模式 ──
if [ "$UNINSTALL" = true ]; then
    step "解除安裝 OmniSight"
    echo "⚠️  即將刪除所有 OmniSight 容器、volumes 和 cloudflared 設定。"
    read -rp "確定要繼續嗎？[y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy] ]]; then
        echo "取消。"; exit 0
    fi
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    sudo systemctl stop cloudflared 2>/dev/null || true
    sudo systemctl disable cloudflared 2>/dev/null || true
    sudo cloudflared service uninstall 2>/dev/null || true
    log "已清除容器 + volumes + cloudflared"
    exit 0
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 0: 前置條件檢查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
step "Step 0: 前置條件檢查"

PREFLIGHT_PASS=true

# Docker 安裝
if ! command -v docker &>/dev/null; then
    err "Docker 未安裝。"
    echo "   安裝指南：https://docs.docker.com/engine/install/"
    PREFLIGHT_PASS=false
else
    log "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+' || echo 'unknown')"
fi

# Docker daemon 是否在運行
if command -v docker &>/dev/null; then
    if ! docker info &>/dev/null; then
        err "Docker daemon 未啟動。"
        echo "   請執行：sudo systemctl start docker  或啟動 Docker Desktop"
        PREFLIGHT_PASS=false
    else
        log "Docker daemon 運行中"
    fi
fi

# Docker Compose V2
if ! docker compose version &>/dev/null; then
    err "Docker Compose V2 未安裝。"
    echo "   請升級 Docker Desktop 或：sudo apt-get install docker-compose-plugin"
    PREFLIGHT_PASS=false
else
    log "Docker Compose $(docker compose version --short 2>/dev/null || echo 'unknown')"
fi

# curl
if ! command -v curl &>/dev/null; then
    err "curl 未安裝。請執行：sudo apt-get install curl"
    PREFLIGHT_PASS=false
else
    log "curl OK"
fi

# openssl（用於 tunnel secret 生成）
if ! command -v openssl &>/dev/null; then
    warn "openssl 未安裝，Cloudflare Tunnel 設定可能失敗。"
    echo "   請執行：sudo apt-get install openssl"
else
    log "openssl OK"
fi

# jq（CF API JSON 解析用）
if ! command -v jq &>/dev/null; then
    warn "jq 未安裝，嘗試自動安裝..."
    if sudo apt-get update -qq && sudo apt-get install -y -qq jq >/dev/null 2>&1; then
        log "jq 安裝成功"
    else
        err "jq 安裝失敗。Cloudflare 自動設定需要 jq。"
        echo "   請手動執行：sudo apt-get install jq"
        PREFLIGHT_PASS=false
    fi
else
    log "jq OK"
fi

# Port 檢查
for PORT in $BACKEND_PORT $FRONTEND_PORT; do
    if ss -tlnp 2>/dev/null | grep -q ":${PORT} " || \
       lsof -i ":${PORT}" &>/dev/null; then
        err "Port ${PORT} 已被佔用。"
        echo "   請釋放 port 或修改 COMPOSE_FILE 中的 port mapping"
        echo "   查看佔用者：lsof -i :${PORT} 或 ss -tlnp | grep ${PORT}"
        PREFLIGHT_PASS=false
    else
        log "Port ${PORT} 可用"
    fi
done

# 磁碟空間
AVAIL_GB=$(df -BG . 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo "0")
if [ "${AVAIL_GB:-0}" -lt "$MIN_DISK_GB" ]; then
    err "磁碟空間不足：剩餘 ${AVAIL_GB}G，需要至少 ${MIN_DISK_GB}G。"
    echo "   Docker build + images 需要約 3-5 GB 空間"
    PREFLIGHT_PASS=false
else
    log "磁碟空間：${AVAIL_GB}G 可用（需求 ≥${MIN_DISK_GB}G）"
fi

# 確認專案根目錄
if [ ! -f "$COMPOSE_FILE" ]; then
    err "找不到 $COMPOSE_FILE。請在 OmniSight-Productizer 根目錄執行此腳本。"
    PREFLIGHT_PASS=false
else
    log "專案目錄：$(pwd)"
fi

# .env.example 存在
if [ ! -f ".env.example" ]; then
    err "找不到 .env.example。專案檔案可能不完整，請重新 git clone。"
    PREFLIGHT_PASS=false
else
    log ".env.example 存在"
fi

# WSL2 systemd check — probe PID 1 being systemd, not just the `systemctl`
# binary. On WSL2 without [boot] systemd=true the binary still exists.
WSL_SYSTEMD=true
IS_WSL=false
if grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=true
    if [ -d /run/systemd/system ] && [ "$(ps -p 1 -o comm= 2>/dev/null || echo unknown)" = "systemd" ]; then
        log "WSL2 + systemd 已啟用（cloudflared 將以 systemd service 常駐）"
    else
        warn "偵測到 WSL2 但 systemd 未啟用 → cloudflared 將以 nohup 背景程序運行。"
        echo ""  | tee -a "$LOG_FILE"
        echo -e "${BOLD}建議啟用 WSL2 systemd（可選但推薦）：${NC}" | tee -a "$LOG_FILE"
        echo "  1. 編輯 /etc/wsl.conf（若不存在則建立）：" | tee -a "$LOG_FILE"
        echo "       sudo tee /etc/wsl.conf >/dev/null <<'EOF'" | tee -a "$LOG_FILE"
        echo "       [boot]" | tee -a "$LOG_FILE"
        echo "       systemd=true" | tee -a "$LOG_FILE"
        echo "       EOF" | tee -a "$LOG_FILE"
        echo "  2. 在 Windows PowerShell 執行：wsl --shutdown" | tee -a "$LOG_FILE"
        echo "  3. 重新進入 WSL 後重跑本腳本 → cloudflared 會自動升級為 systemd service" | tee -a "$LOG_FILE"
        echo "" | tee -a "$LOG_FILE"
        WSL_SYSTEMD=false
    fi
elif [ ! -d /run/systemd/system ] || [ "$(ps -p 1 -o comm= 2>/dev/null || echo unknown)" != "systemd" ]; then
    WSL_SYSTEMD=false
    warn "偵測到非 systemd init → cloudflared 將以 nohup 背景程序運行。"
fi

# 網路連線
if ! curl -sf --max-time 5 "https://api.cloudflare.com/client/v4/" >/dev/null 2>&1; then
    warn "無法連線到 Cloudflare API。Cloudflare Tunnel 設定可能失敗。"
    echo "   請確認網路連線正常。"
else
    log "網路連線正常"
fi

# 前置檢查結果
echo ""
if [ "$PREFLIGHT_PASS" = false ]; then
    err "前置條件檢查未通過。請修復上述問題後重新執行。"
    exit 1
fi
log "所有前置條件通過 ✓"

if [ "$DRY_RUN" = true ]; then
    echo ""
    log "[Dry-run] 前置條件檢查完成。加入 --dry-run 模式不會執行後續步驟。"
    exit 0
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 1: .env 生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
step "Step 1: 環境設定 (.env)"

# sed-safe 替換函式（處理 API key 中的特殊字元 / & \）
_sed_safe_replace() {
    local file="$1" key="$2" value="$3"
    # 用 awk 做替換，避免 sed 的特殊字元問題
    awk -v k="$key" -v v="$value" '
        BEGIN { found=0 }
        $0 ~ "^"k"=" { print k"="v; found=1; next }
        { print }
    ' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
}

if [ -f ".env" ]; then
    log ".env 已存在，跳過生成。"
    echo "   如需重新設定，請刪除 .env 後重新執行。"
elif [ "$NON_INTERACTIVE" = true ]; then
    cp .env.example .env
    warn "非互動模式：已複製 .env.example → .env（未填 API key）。"
    warn "請在執行完畢後編輯 .env 填入 API key，或重新以互動 TTY 執行腳本。"
else
    cp .env.example .env
    log "已從 .env.example 複製 .env"

    echo ""
    echo -e "${BOLD}選擇 LLM Provider：${NC}"
    echo "  1) Anthropic (Claude Opus 4.7) — 推薦"
    echo "  2) OpenAI (GPT)"
    echo "  3) Google (Gemini)"
    echo "  4) Ollama (本地，免 API key)"
    echo ""
    read -rp "請輸入 1-4 [預設 1]: " llm_choice
    llm_choice=${llm_choice:-1}

    case "$llm_choice" in
        1)
            _sed_safe_replace .env "OMNISIGHT_LLM_PROVIDER" "anthropic"
            read_secret "Anthropic API Key (sk-ant-..., 輸入不顯示): " api_key
            # 格式驗證
            if [ -n "$api_key" ] && [[ ! "$api_key" =~ ^sk-ant- ]]; then
                warn "API Key 格式異常（預期以 sk-ant- 開頭）。仍將寫入，但可能無法連線。"
            fi
            if [ -z "$api_key" ]; then
                warn "未輸入 API Key。系統將以 rule-based fallback 模式運行（無 AI 推理）。"
                warn "稍後可在 Bootstrap wizard 或 .env 中補填。"
            else
                _sed_safe_replace .env "OMNISIGHT_ANTHROPIC_API_KEY" "$api_key"
                _sed_safe_replace .env "ANTHROPIC_API_KEY" "$api_key"
                log "API Key 已寫入（末四碼：...${api_key: -4}）"
            fi
            ;;
        2)
            _sed_safe_replace .env "OMNISIGHT_LLM_PROVIDER" "openai"
            read_secret "OpenAI API Key (sk-..., 輸入不顯示): " api_key
            if [ -n "$api_key" ] && [[ ! "$api_key" =~ ^sk- ]]; then
                warn "API Key 格式異常（預期以 sk- 開頭）。"
            fi
            [ -n "$api_key" ] && _sed_safe_replace .env "OMNISIGHT_OPENAI_API_KEY" "$api_key"
            ;;
        3)
            _sed_safe_replace .env "OMNISIGHT_LLM_PROVIDER" "google"
            read_secret "Google API Key (輸入不顯示): " api_key
            [ -n "$api_key" ] && _sed_safe_replace .env "OMNISIGHT_GOOGLE_API_KEY" "$api_key"
            ;;
        4)
            _sed_safe_replace .env "OMNISIGHT_LLM_PROVIDER" "ollama"
            log "Ollama 模式，不需 API key。請確保 ollama serve 在運行。"
            ;;
        *)
            warn "無效選項，使用預設 Anthropic（無 key，rule-based fallback）。"
            ;;
    esac

    log ".env 設定完成"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 2: Docker 容器啟動
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
step "Step 2: 啟動 Docker 容器"

echo "📦 Building + starting containers (首次可能需要 5-10 分鐘)..."
if ! docker compose -f "$COMPOSE_FILE" up -d --build 2>&1 | tee -a "$LOG_FILE" | tail -10; then
    err "Docker 容器啟動失敗。"
    echo ""
    echo -e "${BOLD}排查步驟：${NC}"
    echo "  1. 查看完整日誌：docker compose -f $COMPOSE_FILE logs"
    echo "  2. 常見原因："
    echo "     - Dockerfile build 失敗（npm install / pip install 問題）"
    echo "     - Port 衝突（其他程序佔用 $BACKEND_PORT 或 $FRONTEND_PORT）"
    echo "     - 磁碟空間不足"
    echo "  3. 修復後重新執行此腳本即可（支援冪等）"
    exit 1
fi

log "容器已啟動"

# 等 2 秒讓容器初始化
sleep 2

# 快速確認容器狀態。使用 --services --status=running 取代 grep/jq 組合，
# 避免 `grep -c pattern || echo "0"` 在 0 筆時印出 "0\n0" 導致整數比較爆炸。
RUNNING_SVCS=$(docker compose -f "$COMPOSE_FILE" ps --services --status=running 2>/dev/null | grep -c . || true)
RUNNING_SVCS=${RUNNING_SVCS//[^0-9]/}
RUNNING_SVCS=${RUNNING_SVCS:-0}
if [ "$RUNNING_SVCS" -lt 2 ]; then
    warn "部分容器可能未成功啟動（running 服務數：${RUNNING_SVCS}）。"
    docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | tee -a "$LOG_FILE"
    echo ""
    echo "  如果看到 'Exited' 狀態的容器，請執行："
    echo "  docker compose -f $COMPOSE_FILE logs <service-name>"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 3: 健康檢查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
step "Step 3: 等待服務就緒"

_wait_for_health() {
    local name="$1" url="$2" retries="$3" interval="$4"
    echo -n "⏳ ${name} "
    for i in $(seq 1 "$retries"); do
        if curl -sf --max-time 3 "$url" >/dev/null 2>&1; then
            echo ""
            log "${name} 就緒（第 ${i}/${retries} 次檢查）"
            return 0
        fi
        echo -n "."
        sleep "$interval"
    done
    echo ""
    err "${name} 啟動超時（${retries} × ${interval}s = $((retries * interval))s）。"
    echo "   排查：docker compose -f $COMPOSE_FILE logs $(echo "$name" | tr '[:upper:]' '[:lower:]')"
    return 1
}

_wait_for_health "Backend" "http://localhost:${BACKEND_PORT}/api/v1/health" "$HEALTH_RETRIES" "$HEALTH_INTERVAL" || exit 1
_wait_for_health "Frontend" "http://localhost:${FRONTEND_PORT}/" "$HEALTH_RETRIES" "$HEALTH_INTERVAL" || exit 1

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 4: Cloudflare Tunnel 自動建立
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
step "Step 4: Cloudflare Tunnel 設定（公網 HTTPS）"

CF_READY=false
# Populated from CF zones API (.result[0].name_servers). Step 5 reads this
# to print the exact NS values the user must copy into GoDaddy. Stays empty
# if the CF setup block is skipped (no token / zone not found), in which
# case Step 5 falls back to "look them up in CF dashboard".
CF_NAMESERVERS=()

echo -e "${BOLD}你的域名：${DOMAIN}${NC}"
echo ""
echo "Cloudflare Tunnel 讓你的 WSL2 主機不需開 port、不需固定 IP，"
echo "就能透過 https://${DOMAIN} 對外服務。"
echo ""
if [ "$NON_INTERACTIVE" = true ]; then
    cf_setup="N"
    warn "非互動模式：跳過 Cloudflare Tunnel 設定。請之後在 Bootstrap wizard 中完成。"
else
    read -rp "是否現在設定 Cloudflare Tunnel？[Y/n]: " cf_setup
    cf_setup=${cf_setup:-Y}
fi

if [[ "$cf_setup" =~ ^[Yy] ]]; then
    echo ""
    echo -e "${BOLD}請提供 Cloudflare API Token${NC}"
    echo "  建立方式：https://dash.cloudflare.com/profile/api-tokens → Create Token"
    echo "  所需權限：Account:Cloudflare Tunnel:Edit + Zone:DNS:Edit + Account:Account Settings:Read"
    echo ""
    read_secret "Cloudflare API Token (輸入不顯示): " CF_TOKEN

    if [ -z "$CF_TOKEN" ]; then
        warn "未提供 Token，跳過 Cloudflare 設定。"
        warn "你稍後可在 Bootstrap wizard → Step 3 中設定。"
    else
        echo "" | tee -a "$LOG_FILE"
        echo "🔍 驗證 Token..." | tee -a "$LOG_FILE"

        # ── 驗證 Token ──
        CF_VERIFY=$(curl -sf --max-time 10 -H "Authorization: Bearer ${CF_TOKEN}" \
            "https://api.cloudflare.com/client/v4/user/tokens/verify" 2>/dev/null || echo '{"success":false}')

        if ! echo "$CF_VERIFY" | jq -e '.success' >/dev/null 2>&1; then
            err "Token 驗證失敗。"
            echo "   可能原因：Token 過期、權限不足、或格式錯誤" | tee -a "$LOG_FILE"
            echo "   驗證回應：$(echo "$CF_VERIFY" | jq -r '.errors[0].message // "unknown"' 2>/dev/null)" | tee -a "$LOG_FILE"
            warn "跳過 Cloudflare 設定。你可在 Bootstrap wizard 中重試。"
        else
            log "Token 驗證通過"

            # ── 取得 Account ID ──
            CF_ACCOUNTS=$(curl -sf --max-time 10 -H "Authorization: Bearer ${CF_TOKEN}" \
                "https://api.cloudflare.com/client/v4/accounts?page=1&per_page=5" 2>/dev/null || echo '{"result":[]}')
            CF_ACCOUNT_ID=$(echo "$CF_ACCOUNTS" | jq -r '.result[0].id // empty')
            CF_ACCOUNT_NAME=$(echo "$CF_ACCOUNTS" | jq -r '.result[0].name // "unknown"')

            if [ -z "$CF_ACCOUNT_ID" ]; then
                err "無法取得 Cloudflare Account ID。Token 可能缺少 Account Settings:Read 權限。"
                warn "跳過 Cloudflare 設定。"
            else
                log "Account: ${CF_ACCOUNT_NAME} (${CF_ACCOUNT_ID})"

                # ── 取得 Zone ID ──
                CF_ZONES=$(curl -sf --max-time 10 -H "Authorization: Bearer ${CF_TOKEN}" \
                    "https://api.cloudflare.com/client/v4/zones?name=${DOMAIN}" 2>/dev/null || echo '{"result":[]}')
                CF_ZONE_ID=$(echo "$CF_ZONES" | jq -r '.result[0].id // empty')
                CF_ZONE_STATUS=$(echo "$CF_ZONES" | jq -r '.result[0].status // "not_found"')

                if [ -z "$CF_ZONE_ID" ]; then
                    warn "域名 ${DOMAIN} 尚未加入 Cloudflare。"
                    echo ""
                    echo "  請先到 Cloudflare Dashboard 加入域名："
                    echo "    https://dash.cloudflare.com → Add a site → ${DOMAIN}"
                    echo ""
                    echo "  加入後會取得 Nameserver，再到 GoDaddy 更改（見 Step 5）。"
                    echo "  完成後重新執行此腳本即可。"
                else
                    log "Zone: ${DOMAIN} (${CF_ZONE_ID}) — status: ${CF_ZONE_STATUS}"

                    # Extract the 2 CF-assigned NS values so Step 5 can print
                    # them verbatim. Plain `while IFS= read` (no bash-4 only
                    # builtins) keeps this portable to bash 3.2. Capture jq
                    # stdout to a variable first so a jq failure doesn't
                    # silently null the array via process substitution +
                    # set -e.
                    CF_NS_RAW=$(echo "$CF_ZONES" | jq -r '.result[0].name_servers // [] | .[]' 2>/dev/null || true)
                    if [ -n "$CF_NS_RAW" ]; then
                        while IFS= read -r ns_line; do
                            [ -n "$ns_line" ] && CF_NAMESERVERS+=("$ns_line")
                        done <<< "$CF_NS_RAW"
                    fi

                    if [ "$CF_ZONE_STATUS" = "pending" ]; then
                        warn "Zone 狀態為 'pending'——Nameserver 尚未遷移或正在傳播中。"
                        warn "Tunnel 仍可建立，但 HTTPS 需等 NS 傳播完成後才能用。"
                    fi

                    # ── 建立或複用 Tunnel ──
                    echo "🔧 建立 Cloudflare Tunnel: ${TUNNEL_NAME}..." | tee -a "$LOG_FILE"

                    # 先查是否已存在
                    EXISTING=$(curl -sf --max-time 10 -H "Authorization: Bearer ${CF_TOKEN}" \
                        "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/tunnels?name=${TUNNEL_NAME}&is_deleted=false" 2>/dev/null || echo '{"result":[]}')
                    CF_TUNNEL_ID=$(echo "$EXISTING" | jq -r '.result[0].id // empty')

                    if [ -n "$CF_TUNNEL_ID" ]; then
                        log "Tunnel 已存在: ${CF_TUNNEL_ID}，複用。"
                        CF_TOKEN_RESP=$(curl -sf --max-time 10 -H "Authorization: Bearer ${CF_TOKEN}" \
                            "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/tunnels/${CF_TUNNEL_ID}/token" 2>/dev/null || echo '{"result":""}')
                        CF_TUNNEL_TOKEN=$(echo "$CF_TOKEN_RESP" | jq -r '.result // empty')
                    else
                        TUNNEL_SECRET=$(openssl rand -base64 32 2>/dev/null || head -c 32 /dev/urandom | base64)
                        CF_TUNNEL_RESP=$(curl -sf --max-time 15 -X POST \
                            -H "Authorization: Bearer ${CF_TOKEN}" \
                            -H "Content-Type: application/json" \
                            -d "{\"name\":\"${TUNNEL_NAME}\",\"tunnel_secret\":\"${TUNNEL_SECRET}\"}" \
                            "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/tunnels" 2>/dev/null || echo '{"success":false}')

                        if echo "$CF_TUNNEL_RESP" | jq -e '.success' >/dev/null 2>&1; then
                            CF_TUNNEL_ID=$(echo "$CF_TUNNEL_RESP" | jq -r '.result.id')
                            CF_TUNNEL_TOKEN=$(echo "$CF_TUNNEL_RESP" | jq -r '.result.token')
                            log "Tunnel 建立成功: ${CF_TUNNEL_ID}"
                        else
                            CF_ERR_MSG=$(echo "$CF_TUNNEL_RESP" | jq -r '.errors[0].message // "unknown error"' 2>/dev/null)
                            err "Tunnel 建立失敗: ${CF_ERR_MSG}"
                            CF_TUNNEL_ID=""
                        fi
                    fi

                    if [ -n "${CF_TUNNEL_ID:-}" ] && [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
                        # ── 設定 ingress ──
                        echo "🔧 設定 Tunnel ingress..." | tee -a "$LOG_FILE"
                        INGRESS_RESP=$(curl -sf --max-time 10 -X PUT \
                            -H "Authorization: Bearer ${CF_TOKEN}" \
                            -H "Content-Type: application/json" \
                            -d "{
                                \"config\": {
                                    \"ingress\": [
                                        {\"hostname\": \"${DOMAIN}\", \"service\": \"http://localhost:${FRONTEND_PORT}\"},
                                        {\"hostname\": \"${API_SUBDOMAIN}.${DOMAIN}\", \"service\": \"http://localhost:${BACKEND_PORT}\"},
                                        {\"service\": \"http_status:404\"}
                                    ]
                                }
                            }" \
                            "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/tunnels/${CF_TUNNEL_ID}/configurations" 2>/dev/null || echo '{"success":false}')

                        if echo "$INGRESS_RESP" | jq -e '.success' >/dev/null 2>&1; then
                            log "Ingress 設定完成"
                        else
                            err "Ingress 設定失敗：$(echo "$INGRESS_RESP" | jq -r '.errors[0].message // "unknown"' 2>/dev/null)"
                            warn "Tunnel 已建立但 ingress 未配。請在 Cloudflare Dashboard 手動設定。"
                        fi

                        # ── DNS CNAME ──
                        # Idempotent policy:
                        #   1. POST the desired CNAME (happy path).
                        #   2. On "already exists" → GET the record and compare .content.
                        #      - If content matches current tunnel → skip (true no-op).
                        #      - If content points at a DIFFERENT tunnel (e.g. stale
                        #        from a prior tunnel that got deleted out-of-band),
                        #        PATCH to update so re-runs self-heal silently.
                        #      Without the drift-check, re-running the script after a
                        #      tunnel delete+recreate would leave the CNAME pointed at
                        #      a dead tunnel and the site would 1016 forever.
                        TUNNEL_CNAME="${CF_TUNNEL_ID}.cfargotunnel.com"
                        for HOSTNAME in "$DOMAIN" "${API_SUBDOMAIN}.${DOMAIN}"; do
                            echo "🔧 DNS CNAME: ${HOSTNAME} → ${TUNNEL_CNAME}" | tee -a "$LOG_FILE"
                            DNS_RESP=$(curl -sf --max-time 10 -X POST \
                                -H "Authorization: Bearer ${CF_TOKEN}" \
                                -H "Content-Type: application/json" \
                                -d "{\"type\":\"CNAME\",\"name\":\"${HOSTNAME}\",\"content\":\"${TUNNEL_CNAME}\",\"proxied\":true}" \
                                "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records" 2>/dev/null || echo '{"success":false}')
                            if echo "$DNS_RESP" | jq -e '.success' >/dev/null 2>&1; then
                                log "  ${HOSTNAME} CNAME 已建立"
                            else
                                DNS_ERR=$(echo "$DNS_RESP" | jq -r '.errors[0].message // "unknown"' 2>/dev/null)
                                if echo "$DNS_ERR" | grep -qi "already exists"; then
                                    # Verify existing record points at current tunnel; PATCH if drifted.
                                    EXISTING_REC=$(curl -sf --max-time 10 \
                                        -H "Authorization: Bearer ${CF_TOKEN}" \
                                        "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records?type=CNAME&name=${HOSTNAME}" \
                                        2>/dev/null || echo '{"result":[]}')
                                    EXISTING_ID=$(echo "$EXISTING_REC" | jq -r '.result[0].id // empty')
                                    EXISTING_CONTENT=$(echo "$EXISTING_REC" | jq -r '.result[0].content // empty')
                                    if [ -z "$EXISTING_ID" ]; then
                                        warn "  ${HOSTNAME} CNAME 已存在但無法查詢內容，保守跳過。"
                                    elif [ "$EXISTING_CONTENT" = "$TUNNEL_CNAME" ]; then
                                        log "  ${HOSTNAME} CNAME 已存在且指向當前 tunnel，跳過"
                                    else
                                        echo "   偵測到 CNAME 漂移：目前指向 ${EXISTING_CONTENT}，更新為 ${TUNNEL_CNAME}..." | tee -a "$LOG_FILE"
                                        PATCH_RESP=$(curl -sf --max-time 10 -X PATCH \
                                            -H "Authorization: Bearer ${CF_TOKEN}" \
                                            -H "Content-Type: application/json" \
                                            -d "{\"type\":\"CNAME\",\"name\":\"${HOSTNAME}\",\"content\":\"${TUNNEL_CNAME}\",\"proxied\":true}" \
                                            "https://api.cloudflare.com/client/v4/zones/${CF_ZONE_ID}/dns_records/${EXISTING_ID}" \
                                            2>/dev/null || echo '{"success":false}')
                                        if echo "$PATCH_RESP" | jq -e '.success' >/dev/null 2>&1; then
                                            log "  ${HOSTNAME} CNAME 已更新至當前 tunnel"
                                        else
                                            PATCH_ERR=$(echo "$PATCH_RESP" | jq -r '.errors[0].message // "unknown"' 2>/dev/null)
                                            warn "  ${HOSTNAME} CNAME 更新失敗: ${PATCH_ERR}"
                                        fi
                                    fi
                                else
                                    warn "  ${HOSTNAME} CNAME 建立失敗: ${DNS_ERR}"
                                fi
                            fi
                        done

                        # ── 安裝 cloudflared ──
                        echo "🔧 安裝 cloudflared connector..." | tee -a "$LOG_FILE"
                        if ! command -v cloudflared &>/dev/null; then
                            # arch-aware download (amd64 / arm64). Cloudflare
                            # doesn't ship .deb for other arches.
                            CF_ARCH="$(uname -m)"
                            case "$CF_ARCH" in
                                x86_64|amd64) CF_DEB="cloudflared-linux-amd64.deb" ;;
                                aarch64|arm64) CF_DEB="cloudflared-linux-arm64.deb" ;;
                                *)
                                    err "不支援的 CPU 架構：${CF_ARCH}。請手動安裝 cloudflared。"
                                    CF_DEB=""
                                    CF_TUNNEL_TOKEN=""
                                    ;;
                            esac
                            echo "   下載 cloudflared (${CF_ARCH})..." | tee -a "$LOG_FILE"
                            if [ -n "$CF_DEB" ] && curl -fsSL --max-time 60 \
                                "https://github.com/cloudflare/cloudflared/releases/latest/download/${CF_DEB}" \
                                -o /tmp/cloudflared.deb; then
                                if sudo dpkg -i /tmp/cloudflared.deb >/dev/null 2>&1; then
                                    log "cloudflared 安裝成功"
                                else
                                    err "cloudflared .deb 安裝失敗。"
                                    echo "   請手動安裝：sudo dpkg -i /tmp/cloudflared.deb"
                                    warn "跳過 cloudflared 啟動。"
                                    CF_TUNNEL_TOKEN=""
                                fi
                                rm -f /tmp/cloudflared.deb
                            else
                                err "cloudflared 下載失敗。請檢查網路連線。"
                                CF_TUNNEL_TOKEN=""
                            fi
                        else
                            log "cloudflared 已安裝：$(cloudflared --version 2>&1 | head -1)"
                        fi

                        # ── 啟動 cloudflared ──
                        if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
                            if [ "$WSL_SYSTEMD" = true ]; then
                                echo "🔧 設定 cloudflared systemd service..." | tee -a "$LOG_FILE"
                                sudo cloudflared service install "$CF_TUNNEL_TOKEN" 2>/dev/null || true
                                sudo systemctl enable cloudflared 2>/dev/null || true
                                if sudo systemctl restart cloudflared 2>/dev/null; then
                                    log "cloudflared service 已啟動（systemd）"
                                else
                                    err "cloudflared service 啟動失敗。"
                                    echo "   排查：sudo systemctl status cloudflared"
                                    echo "   日誌：sudo journalctl -u cloudflared -n 20"
                                fi
                            else
                                echo "🔧 以 nohup 背景程序啟動 cloudflared..." | tee -a "$LOG_FILE"
                                # Idempotency: a stale PID file from a previous run may still point
                                # at a live cloudflared. Reuse it if so — avoids spawning parallel
                                # tunnel connectors which confuses Cloudflare's load balancer.
                                CFLARED_PID_FILE="/tmp/omnisight-cloudflared.pid"
                                CFLARED_LOG="/tmp/omnisight-cloudflared.log"
                                CFLARED_PID=""
                                if [ -f "$CFLARED_PID_FILE" ]; then
                                    OLD_PID=$(cat "$CFLARED_PID_FILE" 2>/dev/null || echo "")
                                    # PID recycling is real on WSL (short-lived shell tasks can take
                                    # a previously-used PID), so verify the process is actually
                                    # cloudflared before trusting it.
                                    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null && \
                                       ps -p "$OLD_PID" -o comm= 2>/dev/null | grep -q cloudflared; then
                                        log "cloudflared 已在背景運行（PID: ${OLD_PID}），複用。"
                                        CFLARED_PID="$OLD_PID"
                                    else
                                        rm -f "$CFLARED_PID_FILE"
                                    fi
                                fi

                                if [ -z "$CFLARED_PID" ]; then
                                    nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" \
                                        >> "$CFLARED_LOG" 2>&1 &
                                    CFLARED_PID=$!
                                    # detach from the shell's job table so Ctrl-C on the script
                                    # doesn't take the tunnel down; `|| true` because disown is
                                    # a no-op (and would exit non-zero under -e) if job control
                                    # is off, e.g. under `sh` or in some CI runners.
                                    disown "$CFLARED_PID" 2>/dev/null || true
                                    echo "$CFLARED_PID" > "$CFLARED_PID_FILE"
                                    sleep 3
                                fi

                                if kill -0 "$CFLARED_PID" 2>/dev/null; then
                                    log "cloudflared 已啟動（PID: ${CFLARED_PID}）"
                                    echo "   PID file: $CFLARED_PID_FILE" | tee -a "$LOG_FILE"
                                    echo "   日誌:    $CFLARED_LOG" | tee -a "$LOG_FILE"
                                    warn "nohup 模式：cloudflared 會在 WSL 重啟後消失，屆時需重跑此腳本。"
                                    warn "若要「開機自動啟動 + 崩潰自動重啟」，請依 Step 0 的提示啟用 systemd。"
                                else
                                    rm -f "$CFLARED_PID_FILE"
                                    err "cloudflared 啟動後立即退出。"
                                    echo "   查看日誌：cat $CFLARED_LOG"
                                fi
                            fi

                            sleep 3
                            CF_READY=true
                            log "Cloudflare Tunnel 設定完成 ✓"
                        fi
                    fi
                fi
            fi
        fi
    fi
else
    warn "跳過 Cloudflare Tunnel。你可稍後在 Bootstrap wizard 的 Step 3 設定。"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 5: GoDaddy NS 遷移指引
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GoDaddy doesn't expose a public NS-change API for consumer domains, so
# this step is manual by necessity. What we *can* do:
#   (a) Auto-detect current NS state (already on CF / still on GoDaddy /
#       partially propagated) so we don't nag users who've already done it.
#   (b) Print the exact two CF NS values (fetched from CF zones API above)
#       so they don't need to tab-switch to the CF dashboard to copy them.
#   (c) Walk them through the current GoDaddy UI with the correct menu path.
#   (d) Give them verification commands + DNSSEC/MX safety warnings.
step "Step 5: GoDaddy Nameserver 遷移（一次性手動步驟）"

# ── (a) Probe current NS state ──
# Force @1.1.1.1 so a broken /etc/resolv.conf inside WSL2 doesn't poison
# the result. +time=3 +tries=1 bounds the query to ~3s. We tolerate a
# trailing dot (dig returns "ns.cloudflare.com." with the terminator).
CURRENT_NS=""
NS_LOOKUP_TOOL=""
if command -v dig &>/dev/null; then
    NS_LOOKUP_TOOL="dig"
    CURRENT_NS=$(dig +short +time=3 +tries=1 NS "$DOMAIN" @1.1.1.1 2>/dev/null | tr -d '\r' | sort -u || true)
    # Retry without @1.1.1.1 if the public resolver was blocked (corp VPN).
    [ -z "$CURRENT_NS" ] && CURRENT_NS=$(dig +short +time=3 +tries=1 NS "$DOMAIN" 2>/dev/null | tr -d '\r' | sort -u || true)
elif command -v host &>/dev/null; then
    NS_LOOKUP_TOOL="host"
    CURRENT_NS=$(host -t NS "$DOMAIN" 2>/dev/null | awk '/name server/ {print $NF}' | sort -u || true)
elif command -v nslookup &>/dev/null; then
    NS_LOOKUP_TOOL="nslookup"
    CURRENT_NS=$(nslookup -type=NS "$DOMAIN" 2>/dev/null | awk '/nameserver/ {print $NF}' | sort -u || true)
fi

# Classify: all-CF (done), some-CF (mid-propagation), no-CF (still GoDaddy), unknown.
NS_STATE="unknown"
NS_CF_COUNT=0
NS_TOTAL_COUNT=0
if [ -n "$CURRENT_NS" ]; then
    NS_TOTAL_COUNT=$(printf '%s\n' "$CURRENT_NS" | grep -c . || true)
    NS_CF_COUNT=$(printf '%s\n' "$CURRENT_NS" | grep -ci 'ns\.cloudflare\.com' || true)
    # grep -c can print "0\n0" under set -e if no match; normalize.
    NS_CF_COUNT=${NS_CF_COUNT//[^0-9]/}; NS_CF_COUNT=${NS_CF_COUNT:-0}
    NS_TOTAL_COUNT=${NS_TOTAL_COUNT//[^0-9]/}; NS_TOTAL_COUNT=${NS_TOTAL_COUNT:-0}
    if [ "$NS_TOTAL_COUNT" -gt 0 ] && [ "$NS_CF_COUNT" -eq "$NS_TOTAL_COUNT" ]; then
        NS_STATE="all-cf"
    elif [ "$NS_CF_COUNT" -gt 0 ]; then
        NS_STATE="mixed"
    else
        NS_STATE="non-cf"
    fi
fi

case "$NS_STATE" in
    all-cf)
        # Idempotent skip — most common on re-runs.
        DETECTED_CF=$(printf '%s\n' "$CURRENT_NS" | sed 's/\.$//' | tr '\n' ' ')
        log "偵測到 NS 已指向 Cloudflare：${DETECTED_CF}"
        log "✓ NS 遷移已完成，跳過此步驟。"
        ;;
    mixed)
        DETECTED_ALL=$(printf '%s\n' "$CURRENT_NS" | sed 's/\.$//' | tr '\n' ' ')
        warn "偵測到 NS 正在傳播中（${NS_CF_COUNT}/${NS_TOTAL_COUNT} 已指向 Cloudflare）"
        echo "   目前可見的 NS：${DETECTED_ALL}" | tee -a "$LOG_FILE"
        echo "   請再等 30 分鐘 ~ 4 小時，所有 NS 都會收斂到 Cloudflare。" | tee -a "$LOG_FILE"
        echo "   驗證：dig NS ${DOMAIN} +short    或    https://www.whatsmydns.net/#NS/${DOMAIN}" | tee -a "$LOG_FILE"
        ;;
    non-cf|unknown)
        if [ "$NS_STATE" = "non-cf" ]; then
            DETECTED_ALL=$(printf '%s\n' "$CURRENT_NS" | sed 's/\.$//' | tr '\n' ' ')
            warn "偵測到 NS 尚未指向 Cloudflare（目前：${DETECTED_ALL}）"
        elif [ -z "$NS_LOOKUP_TOOL" ]; then
            warn "無 dig/host/nslookup 可用，無法自動探測 NS 狀態。"
        else
            warn "無法解析 ${DOMAIN} 的 NS 記錄（可能是新購域名未生效，或 ${NS_LOOKUP_TOOL} 查詢失敗）。"
        fi

        echo ""
        echo -e "${BOLD}▸ 遷移步驟（只需做一次；之後 DNS 永遠在 Cloudflare 管理）${NC}"
        echo ""

        # ── Step A: 取得 CF 指定的 NS ──
        echo -e "${BOLD}A. 取得 Cloudflare 為 ${DOMAIN} 指派的兩個 Nameserver${NC}"
        echo ""
        if [ "${#CF_NAMESERVERS[@]}" -gt 0 ]; then
            echo "   本腳本已自動從 Cloudflare API 查詢，請複製以下兩個 NS："
            echo ""
            for ns in "${CF_NAMESERVERS[@]}"; do
                echo -e "     ${BOLD}${CYAN}${ns}${NC}"
            done
            echo ""
        else
            echo "   （本腳本未取得 CF API Token 或 zone 尚未建立，請手動查詢）"
            echo "   1. 登入 https://dash.cloudflare.com"
            echo "   2. 點選 ${DOMAIN} 這個 site"
            echo "   3. Overview 頁 → 右側「Cloudflare 指派的 Nameserver」區塊"
            echo "   4. 會看到兩個以 .ns.cloudflare.com 結尾的名稱，複製備用。"
            echo ""
        fi

        # ── Step B: GoDaddy UI walkthrough ──
        echo -e "${BOLD}B. 到 GoDaddy 修改 Nameserver${NC}"
        echo ""
        echo "   1. 登入 GoDaddy："
        echo -e "        ${CYAN}https://dcc.godaddy.com/control/portfolio${NC}"
        echo "      （或舊版直達：https://dcc.godaddy.com/manage/${DOMAIN}/dns）"
        echo ""
        echo "   2. 找到 ${DOMAIN} → 點「⋮」或「DNS」按鈕"
        echo ""
        echo "   3. 切到「Nameservers」頁籤 → 點「Change」（變更 Nameservers）"
        echo ""
        echo "   4. 選「I'll use my own nameservers」（使用我自己的 Nameservers）"
        echo ""
        echo "   5. 填入 Step A 取得的那兩個 Cloudflare NS，儲存。"
        echo ""
        echo -e "      ${YELLOW}⚠  GoDaddy 會跳一個警告視窗說「這會讓域名暫時無法使用」${NC}"
        echo "         → 這是正常的（DNS 傳播期間），確認即可。"
        echo ""

        # ── Step C: 驗證 ──
        echo -e "${BOLD}C. 驗證 NS 傳播${NC}"
        echo ""
        echo "   傳播通常 5 分鐘 ~ 4 小時（上限 48 小時）。驗證方式："
        echo ""
        echo "   • 終端機："
        echo -e "       ${CYAN}dig NS ${DOMAIN} +short${NC}"
        echo "     → 若看到 *.ns.cloudflare.com → 完成 ✓"
        echo "     → 若仍是 *.domaincontrol.com → GoDaddy 還在，請繼續等。"
        echo ""
        echo "   • 全球節點檢視："
        echo -e "       ${CYAN}https://www.whatsmydns.net/#NS/${DOMAIN}${NC}"
        echo ""
        echo "   • Cloudflare Dashboard 上 zone status 會從「Pending」變「Active」，"
        echo "     並寄確認信至你的 CF 帳號信箱。"
        echo ""

        # ── Step D: 安全提醒 ──
        echo -e "${BOLD}▸ 切換前請先確認以下兩點，避免中斷服務${NC}"
        echo ""
        echo "   1. ${BOLD}Email / MX / TXT 記錄${NC} — 如果 ${DOMAIN} 目前有"
        echo "      email（MX）或驗證 TXT（Google Workspace、SPF、DKIM 等），"
        echo "      請先到 Cloudflare Dashboard → DNS 頁面確認 CF 已自動從 GoDaddy"
        echo "      匯入這些記錄；若沒有，請在切換 NS 之前手動補上，"
        echo "      否則 NS 一切換 email 就斷了。"
        echo ""
        echo "   2. ${BOLD}DNSSEC${NC} — 如果 GoDaddy 端有啟用 DNSSEC，"
        echo "      必須先到 GoDaddy 關閉（Domain → DNS → DNSSEC → Disable），"
        echo "      否則 NS 切換後 DNSSEC 驗證失敗 → 全球無法解析你的域名。"
        echo "      驗證指令：${CYAN}dig DS ${DOMAIN} +short${NC}（有輸出 = 有 DNSSEC）"
        echo ""

        # ── 轉送完成後如何確認整條鏈路 ──
        if [ "$CF_READY" = true ]; then
            echo -e "${BOLD}▸ 完成後${NC}"
            echo ""
            echo "   Tunnel + Ingress + DNS CNAME 都已在本腳本中自動建好。"
            echo "   NS 傳播完成後："
            echo -e "     ${CYAN}curl -I https://${DOMAIN}${NC}  → 應回 HTTP/2 200"
            echo "   若 60 分鐘後仍連不上，請重跑本腳本（腳本冪等）。"
            echo ""
        fi
        ;;
esac

if [ "$CF_READY" = true ]; then
    if [ "$NS_STATE" = "all-cf" ]; then
        log "Cloudflare Tunnel + NS 皆已就緒 → https://${DOMAIN} 立即可用 ✓"
    else
        log "Cloudflare Tunnel 已就緒。NS 傳播完成後 https://${DOMAIN} 即可使用。"
    fi
else
    warn "Cloudflare Tunnel 未設定。首次使用請在 Bootstrap wizard → Step 3 完成，或重跑本腳本。"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Step 6: 完成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
step "🎉 OmniSight Productizer 部署完成！"

echo -e "${BOLD}本地存取：${NC}"
echo "  Dashboard:  http://localhost:${FRONTEND_PORT}"
echo "  API Docs:   http://localhost:${BACKEND_PORT}/docs"
echo ""

if [ "$CF_READY" = true ]; then
    echo -e "${BOLD}公網存取（NS 傳播完成後）：${NC}"
    echo "  Dashboard:  https://${DOMAIN}"
    echo "  API:        https://${API_SUBDOMAIN}.${DOMAIN}"
    echo ""
fi

echo -e "${BOLD}下一步：${NC}"
echo "  1. 打開瀏覽器 → http://localhost:${FRONTEND_PORT}"
echo "  2. Bootstrap wizard 將引導你完成首次設定"
echo "  3. 預設管理員：admin@omnisight.local（首次登入會強制改密碼）"
echo ""
echo -e "${BOLD}常用指令：${NC}"
echo "  查看日誌：   docker compose -f ${COMPOSE_FILE} logs -f"
echo "  停止服務：   docker compose -f ${COMPOSE_FILE} down"
echo "  重啟服務：   docker compose -f ${COMPOSE_FILE} restart"
echo "  升級部署：   git pull && docker compose -f ${COMPOSE_FILE} up -d --build"
echo "  清除重裝：   $0 --uninstall"
echo ""
echo -e "${BOLD}部署日誌：${NC} $LOG_FILE"
echo ""

# 嘗試打開瀏覽器
if [ "$CF_READY" = true ]; then
    URL="https://${DOMAIN}"
else
    URL="http://localhost:${FRONTEND_PORT}"
fi

if command -v explorer.exe &>/dev/null; then
    explorer.exe "$URL" 2>/dev/null &
elif command -v xdg-open &>/dev/null; then
    xdg-open "$URL" 2>/dev/null &
fi

echo -e "${GREEN}${BOLD}✅ 部署完成！請在瀏覽器開啟 ${URL}${NC}"
