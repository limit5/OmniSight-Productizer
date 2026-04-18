# Multi-WSL 部署指南

## 架構總覽

```
Windows Host
├── WSL Ubuntu-26.04 — Development + Testing
│   ├── npm run dev (port 3001)
│   ├── uvicorn --reload (port 8002)
│   ├── pytest (in-process, no port)
│   └── auto-runner.py
│
├── WSL Ubuntu-22.04 — Staging
│   ├── docker-compose.staging.yml
│   ├── backend-a :8010 + backend-b :8011
│   └── frontend :3010
│
└── WSL Ubuntu-24.04 — Production
    ├── docker-compose.prod.yml
    ├── Caddy :443/:80 (ACME TLS)
    ├── backend-a :8000 + backend-b :8001
    ├── frontend :3000
    └── CF Tunnel → sora-dev.app
```

## Port 分配表

| 環境 | WSL | Frontend | Backend-A | Backend-B | HTTPS |
|---|---|---|---|---|---|
| Production | 24.04 | 3000 | 8000 | 8001 | 443 (Caddy) |
| Staging | 22.04 | 3010 | 8010 | 8011 | — |
| Dev | 26.04 | 3001 | 8002 | — | — |
| Test | 26.04 | — (in-process) | — | — | — |

## SSL 憑證

### Production (Ubuntu-24.04)

雙層 SSL：

1. **Cloudflare Edge TLS** — 瀏覽器 → Cloudflare (TLS 1.3)
   - 由 Cloudflare 自動管理
   - 免費 Universal SSL 憑證
   - 域名：sora-dev.app

2. **Caddy ACME TLS** — CF Tunnel → Caddy (Let's Encrypt)
   - Caddy 自動取得 + 自動續期
   - 設定：`deploy/reverse-proxy/Caddyfile`
   - 環境變數：
     - `OMNISIGHT_PUBLIC_HOSTNAME=sora-dev.app` → ACME cert
     - `OMNISIGHT_ACME_EMAIL=your@email.com` → Let's Encrypt 通知
   - 無 hostname → fallback 到 `tls internal`（自簽 CA）

### Staging / Dev

- 不需 SSL（內部存取 via localhost）
- 如需 staging HTTPS：Caddy `tls internal` 自簽即可

## 環境設定

### Development (Ubuntu-26.04)

```bash
# 一鍵配置
./scripts/setup-dev-env.sh

# 日常開發
os-api    # Backend :8002 (auto-reload)
os-dev    # Frontend :3001 (HMR)
os-test   # Full test suite
```

### Staging (Ubuntu-22.04)

```bash
# 首次設定
git clone <repo> ~/work/sora/OmniSight-Productizer
cd ~/work/sora/OmniSight-Productizer
cp .env.example .env.staging
# 編輯 .env.staging: 設 OMNISIGHT_ENV=staging + API key

# 啟動
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d

# 驗證
curl http://localhost:8010/readyz
curl http://localhost:3010/
```

### Production (Ubuntu-24.04)

```bash
# 日常零停機部署
./scripts/deploy-prod.sh

# 帶 tag 部署
./scripts/deploy-prod.sh --tag v1.2.0

# Rollback
git checkout v1.1.0 && ./scripts/deploy-prod.sh --skip-build
```

## Release 流程

```
Developer (26.04)          Staging (22.04)           Production (24.04)
    │                          │                          │
    │ 1. code + test           │                          │
    │ 2. pytest ✅             │                          │
    │ 3. git push dev          │                          │
    │─────────────────────────→│                          │
    │                          │ 4. git pull dev           │
    │                          │ 5. docker compose up      │
    │                          │ 6. E2E test ✅            │
    │                          │ 7. QA 驗收 ✅             │
    │                          │ 8. git tag v1.x.x        │
    │                          │ 9. git push main          │
    │                          │─────────────────────────→│
    │                          │                          │ 10. deploy-prod.sh
    │                          │                          │ 11. 零停機完成 ✅
```

## 遷移到 Ubuntu 26.04

1. `wsl --install -d Ubuntu-26.04`
2. 在 26.04 內：`git clone <repo> && ./scripts/setup-dev-env.sh`
3. 驗證 dev 環境可用
4. 在 24.04 內：`./scripts/clean-prod-env.sh` 清理 dev artifacts
5. 完成——24.04 是乾淨的 production，26.04 是開發

## Windows Auto-start

Production WSL 需在 Windows 重啟後自動啟動：

```powershell
# Windows Task Scheduler (開機時執行)
wsl -d Ubuntu-24.04 -e bash -c "cd /home/user/work/sora/OmniSight-Productizer && docker compose -f docker-compose.prod.yml up -d"
```

## 故障排除

| 問題 | 原因 | 解法 |
|---|---|---|
| Port 衝突 | 多個 WSL 用同一 port | 檢查 port 分配表 |
| WSL IP 變動 | Windows 重啟後 IP 重新分配 | CF Tunnel 用 localhost（不受影響） |
| Docker 衝突 | Docker Desktop 共用 daemon | 用原生 Docker Engine（各 WSL 獨立） |
| OOM | 多環境同時跑 | `.wslconfig` 設定 memory=68GB（已配置） |
