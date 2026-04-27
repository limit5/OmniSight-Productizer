# AS.0.1 — Existing Auth Surface Inventory

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Purpose**: 完整盤點 OmniSight 在進入 AS 重構（OAuth + Turnstile + token vault + auto-gen 密碼 + 8 頁 UI overhaul）之前，**既有 production user 會碰到的所有 auth 路徑**——包含互動式 user auth、機器對機器 API auth、third-party credential 儲存、自動化 client bypass 路徑。
>
> **目標讀者**：(1) 寫 AS.0.2 alembic migration 的人——需要知道哪些表 / 哪些欄位 / 哪些 enum 不能動。(2) 寫 AS.0.6 automation bypass list 的人——需要知道現有 bypass 機制。(3) 寫 AS.0.9 compat regression test suite 的人——需要知道 5 顆 critical 涵蓋哪些 surface。(4) 寫 AS.6 OmniSight 自家 dogfood 的人——需要 reuse 而非重造。
>
> **範圍邊界**：只盤點「進入 AS 之前的現狀」，不規劃 AS 之後的 to-be。AS.0.2 起的 migration plan 看下游 row。
>
> **盤點方法**：grep + Explore agent 三路平行——(A) interactive auth UI/UX surface (login / signup / password-reset / MFA / OIDC / session)、(B) machine API auth surface (bearer / api_keys / webhook signatures / CSRF exemptions / metrics token / auth_baseline allowlist)、(C) credential storage subsystems (`git_credentials` / `llm_credentials` / `codesign_store` 三個既有 vault + Fernet key 共用)。

---

## 1. Interactive Auth Surface (User-facing routes + Frontend pages)

### 1.1 Login / Session Lifecycle

| Route | Method | Handler | Auth mode | RBAC | Lockout / rate-limit | 備註 |
|---|---|---|---|---|---|---|
| `/api/v1/auth/login` | POST | `backend/routers/auth.py:135` | `open` (allowlisted) | none | IP + email limiter (`backend/auth.py` `_login_throttle`) + per-account lockout | 主 password login；返 `mfa_required=true` + `mfa_token` 進 MFA 子流程 |
| `/api/v1/auth/logout` | POST | `backend/routers/auth.py:262` | `session` | `current_user` | none | 清 session + CSRF cookie |
| `/api/v1/auth/whoami` | GET | `backend/routers/auth.py:272` | `session` | `current_user` | none | 返 user / role / auth_mode / session_id |
| `/api/v1/auth/tenants` | GET | `backend/routers/auth.py:284` | `session` | `current_user` | none | admin 看 all、其他看自家 |
| `/api/v1/auth/sessions` | GET | `backend/routers/auth.py:589` | `session` | `current_user` | none | 列自己 active sessions |
| `/api/v1/auth/sessions/presence` | GET | `backend/routers/auth.py:663` | `session` | `current_user` | none | 60s heartbeat window |
| `/api/v1/auth/sessions/{token_hint}` | DELETE | `backend/routers/auth.py:739` | `session` | `current_user` (or admin) | none | 單 session revoke；`?cascade=not_me` 觸發 peer rotation + 強制 password change |
| `/api/v1/auth/sessions` | DELETE | `backend/routers/auth.py:881` | `session` | `current_user` | none | revoke all peers |

### 1.2 Password Management

| Route | Method | Handler | Auth mode | 備註 |
|---|---|---|---|---|
| `/api/v1/auth/change-password` | POST | `backend/routers/auth.py:317` | `session` | 現密驗證 → rotate session → kick peers；含 zxcvbn ≥ 3、min 12 chars、最近 5 不重用 (`backend/auth.py:89-91`) |
| `/api/v1/auth/reset` | TBD | **未實作** | (allowlisted in `auth_baseline.py:103`) | placeholder——password-reset-request 流程尚未寫 |
| `/api/v1/auth/forgot` | TBD | **未實作** | (allowlisted in `auth_baseline.py:104`) | placeholder——同上 |

> **Gap**：`/auth/reset` + `/auth/forgot` 是 allowlist 上的鬼魂條目——allowlist 早 commit、handler 還沒寫。AS.6.1 要當作 greenfield 實作（不會撞既有 password user 行為）。

### 1.3 Signup / Registration

> **重要：OmniSight 沒有 self-serve signup**。User 由 admin 走 `POST /api/v1/users` 創建，或由 super-admin 走 `/api/v1/admin/super-admins` 升級，或由 bootstrap wizard 設定首位 admin。

| Route | Method | Handler | Auth mode | RBAC | 備註 |
|---|---|---|---|---|---|
| `/api/v1/users` | POST | `backend/routers/auth.py:472` | `session` | `require_admin` | 創 user、拒 super_admin (Y3 約束) |
| `/api/v1/users` | GET | `backend/routers/auth.py:455` | `session` | `require_admin` | list users |
| `/api/v1/users/{user_id}` | PATCH | `backend/routers/auth.py:498` | `session` | `require_admin` | 改 role / enabled / name；任一改動 → rotate sessions |
| `/api/v1/admin/super-admins` | POST | `backend/routers/admin_super_admins.py:238` | `session` | `require_super_admin` | 提升 user 為 super_admin；冪等；拒 disabled |
| `/api/v1/admin/super-admins/{user_id}` | DELETE | `backend/routers/admin_super_admins.py:352` | `session` | `require_super_admin` | 降級 super_admin → admin；含 last-super-admin 保護 |
| `/api/v1/bootstrap/admin-password` | POST | `backend/routers/bootstrap.py:68` | `open` (allowlisted) | none (找 `must_change_password=1` admin) | L2 Step 1 wizard 用 |
| `/api/v1/bootstrap/init-tenant` | POST | `backend/routers/bootstrap.py:369` | `open` (allowlisted) | none | Y7 row 1：wizard 創 tenant + super-admin |

> **AS implications**：AS 在加 OAuth login 之後，「是否開 self-serve signup」是 product decision——backend 要先支援 (AS.1 OAuth client core)、UI 要先設計 (AS.7 8 頁 overhaul)、但 tenant-level 開關走 `tenants.auth_features` (AS.0.2)。既有 admin-create-user 路徑零行為變動。

### 1.4 MFA (TOTP / WebAuthn / Backup codes)

| Route | Method | Handler | Auth mode | 備註 |
|---|---|---|---|---|
| `/api/v1/auth/mfa/status` | GET | `backend/routers/mfa.py:87` | `session` | 列出 enrolled MFA methods + verified status |
| `/api/v1/auth/mfa/totp/enroll` | POST | `backend/routers/mfa.py:101` | `session` | 返 secret + QR code |
| `/api/v1/auth/mfa/totp/confirm` | POST | `backend/routers/mfa.py:111` | `session` | 驗 TOTP code → 返 backup codes → rotate peers |
| `/api/v1/auth/mfa/totp/disable` | POST | `backend/routers/mfa.py:131` | `session` | disable TOTP → rotate peers |
| `/api/v1/auth/mfa/backup-codes/status` | GET | `backend/routers/mfa.py:151` | `session` | 剩餘 backup code 數 |
| `/api/v1/auth/mfa/backup-codes/regenerate` | POST | `backend/routers/mfa.py:156` | `session` | regenerate → rotate peers |
| `/api/v1/auth/mfa/webauthn/register/begin` | POST | `backend/routers/mfa.py:180` | `session` | WebAuthn challenge for registration |
| `/api/v1/auth/mfa/webauthn/register/complete` | POST | `backend/routers/mfa.py:194` | `session` | 完成 register → rotate peers |
| `/api/v1/auth/mfa/webauthn/{mfa_id}` | DELETE | `backend/routers/mfa.py:216` | `session` | 移除 WebAuthn cred → rotate peers |
| `/api/v1/auth/mfa/challenge` | POST | `backend/routers/mfa.py:243` | `open` (allowlisted) | 走 `mfa_token` 驗 TOTP / backup code → 創 session |
| `/api/v1/auth/mfa/webauthn/challenge/begin` | POST | `backend/routers/mfa.py:312` | `open` (allowlisted) | 起 WebAuthn auth challenge |
| `/api/v1/auth/mfa/webauthn/challenge/complete` | POST | `backend/routers/mfa.py:326` | `open` (allowlisted) | 完成 WebAuthn auth → 創 session |

### 1.5 Federation (existing OIDC)

| Route | Method | Handler | 備註 |
|---|---|---|---|
| `/api/v1/auth/oidc/{provider}` | GET | `backend/routers/auth.py:425` | redirect 到 OIDC provider；要求 `OMNISIGHT_OIDC_<PROVIDER>_AUTH_URL` 已設 |

> **AS overlap**：既有 OIDC 是 ad-hoc per-provider 寫法，AS.1 (`backend/auth/oauth_client.py`) 要把它收編進 11-vendor catalog；既有 callback URL **不能** breaking change（既有 IdP 註冊好的 redirect URI 不能動）→ 走 expand-migrate-contract: AS.1 上線後既有 `/auth/oidc/{provider}` 仍 work，新 `/auth/oauth/{provider}` 走新 client，舊 path 標 deprecated 至少 1 release。

### 1.6 Frontend pages (Next.js app router)

| Route | File | Auth status | 備註 |
|---|---|---|---|
| `/login` | `app/login/page.tsx` | public | password + MFA UI；session-revocation banners |
| `/bootstrap` | `app/bootstrap/page.tsx` | public (must-change-password gate) | first-boot wizard |
| `/invite/[token]` | `app/invite/[token]/page.tsx` | public (token-gated) | invite-link signup（admin-issued）|
| `/setup-required` | `app/setup-required/page.tsx` | public | 引導未 bootstrap 的環境 |

**No frontend pages exist for**: signup self-serve / password reset / forgot / MFA enrollment standalone (MFA enrollment 走 settings 子頁面)。AS.7 將補完整 8 頁 UI overhaul。

---

## 2. Machine-to-Machine API Auth Surface

### 2.1 Bearer / API key auth

| Mechanism | Verify | Issue / rotate | 備註 |
|---|---|---|---|
| **K6 per-service bearer (推薦)** | `backend/auth.py:1650-1669` `_validate_api_key()`；`backend/api_keys.py:206-234` `validate_bearer()` | `backend/api_keys.py:99-121` `create_key()` | format `omni_<40-char>`；SHA-256 hash 入 `api_keys` 表；scopes prefix-match (`backend/api_keys.py:64-70`)；`Authorization: Bearer <token>` |
| **Legacy `OMNISIGHT_DECISION_BEARER` env** | `backend/auth.py:1683-1689` `_legacy_bearer_matches()`；`backend/auth_baseline.py:324-328` | `backend/api_keys.py:237-279` `migrate_legacy_bearer()` (lifespan auto-migrate) | `["*"]` scope；K6 啟動時自動移到 `api_keys` 表然後 deprecation log；operator 移除 env 之後仍 work via api_keys row |
| **Legacy callers (decisions / audit / profile routers)** | `backend/routers/decisions.py:64`、`backend/routers/audit.py:38`、`backend/routers/profile.py:29` | n/a | 三個 router 各自呼叫 `os.environ.get("OMNISIGHT_DECISION_BEARER")` 做 inline 檢查——歷史殘留、與 K6 平行存在；AS.0.4 migration plan 必須處理 |
| **`OMNISIGHT_METRICS_TOKEN` (M7)** | `backend/routers/observability.py:42-54` `_check_metrics_token()` | env-based | `/metrics` endpoint；query OR `Authorization: Bearer`；unset 時 endpoint open（Next.js rewrite 限 internal）|

### 2.2 API Key management routes (admin-only)

| Route | Method | Path (full) | RBAC |
|---|---|---|---|
| `GET` | list | `/api/v1/api-keys` | `require_admin` |
| `POST` | create | `/api/v1/api-keys` | `require_admin` |
| `POST` | rotate | `/api/v1/api-keys/{key_id}/rotate` | `require_admin` |
| `POST` | revoke | `/api/v1/api-keys/{key_id}/revoke` | `require_admin` |
| `POST` | enable | `/api/v1/api-keys/{key_id}/enable` | `require_admin` |
| `DELETE` | delete | `/api/v1/api-keys/{key_id}` | `require_admin` |
| `PATCH` | update scopes | `/api/v1/api-keys/{key_id}/scopes` | `require_admin` |

Source: `backend/routers/api_keys.py:16,28,36,54,70,86,102,118` + audit logged via `backend/audit.py`。

### 2.3 Webhook signature auth (third-party callbacks)

| Service | Path | Verify | Secret |
|---|---|---|---|
| Gerrit | `/api/v1/webhooks/gerrit` | HMAC-SHA256 raw body | `gerrit_webhook_secret` (scalar) OR per-host via `git_accounts.encrypted_webhook_secret` |
| GitHub | `/api/v1/webhooks/github` | HMAC-SHA256 via `X-Hub-Signature-256` | `github_webhook_secret` |
| GitLab | `/api/v1/webhooks/gitlab` | bearer token via `X-Gitlab-Token` | `gitlab_webhook_secret` |
| Jira | `/api/v1/webhooks/jira` + `/api/v1/orchestrator/jira` | HMAC-SHA256 | `jira_webhook_secret` |

Source: `backend/routers/webhooks.py:33-300+`，全部在 `auth_baseline` allowlist，bypass session/CSRF。

### 2.4 ChatOps inbound webhook auth

| Platform | Path | Verify | Secret env |
|---|---|---|---|
| Discord | `/api/v1/chatops/webhook/discord` | Ed25519 (header `X-Signature-Ed25519` + `X-Signature-Timestamp`) | `OMNISIGHT_CHATOPS_DISCORD_PUBLIC_KEY` |
| Teams | `/api/v1/chatops/webhook/teams` | HMAC-SHA256 raw body | `OMNISIGHT_CHATOPS_TEAMS_SECRET` |
| Line | `/api/v1/chatops/webhook/line` | HMAC-SHA256 (header `X-Line-Signature`) | `OMNISIGHT_CHATOPS_LINE_CHANNEL_SECRET` |

Source: `backend/chatops/{discord,teams,line}.py` `verify()`；allowlisted。

### 2.5 CSRF exemptions

`backend/auth.py:1791-1797` `csrf_check()` 三條 short-circuit：

1. GET / HEAD / OPTIONS → 永遠 skip (read-only)
2. `Authorization: Bearer ...` 存在 → skip (token caller 不走 cookie)
3. `auth_mode() == "open"` → skip (dev mode)

CSRF cookie `omnisight_csrf` + header `X-CSRF-Token` 兩端 match check（`session` / `strict` mode 下 mutator 才需要）。

### 2.6 `auth_baseline` allowlist (paths that always skip session)

從 `backend/auth_baseline.py:80-130` 抽：

| Prefix | 理由 |
|---|---|
| `/livez`, `/readyz`, `/healthz`, `/api/v1/livez`, ... | Caddy / docker healthcheck |
| `/metrics`, `/api/v1/metrics` | Prometheus exposition (M7 token 是次層 gate) |
| `/api/v1/auth/login` | 登入入口 |
| `/auth/bootstrap`, `/auth/reset`, `/auth/forgot`, `/auth/webauthn/*` | 設定入口（`reset` / `forgot` 是空 placeholder）|
| `/api/v1/bootstrap/*` | first-boot wizard |
| `/api/v1/webhooks/*` | 第三方 callback（自帶 signature 驗證）|
| `/api/v1/chatops/webhook/*` | ChatOps inbound（自帶 signature 驗證）|
| `/api/v1/auth/oidc/*` | OIDC redirect |
| `/api/v1/events/*` | SSE（passive cookie auth at handler level）|
| `/docs`, `/redoc`, `/openapi.json` | API docs (S2-0 prod 關)|

`OMNISIGHT_AUTH_BASELINE_MODE` 三檔：`log`(預設、僅 warn) / `enforce`(401 reject) / `off`(disable)。

---

## 3. Credential Storage Subsystems (will be unified by AS.2 token vault)

### 3.1 `git_credentials` (Phase 5-2/5-3/5-4)

- **Table**: `git_accounts` (alembic 0027)
  - Columns: `id, tenant_id, platform, instance_url, label, username, encrypted_token, encrypted_ssh_key, ssh_host, ssh_port, project, encrypted_webhook_secret, url_patterns, auth_type, is_default, enabled, metadata, last_used_at, created_at, updated_at, version`
- **Encryption**: Fernet via `backend/secret_store.py:98-111`，key 來自 `OMNISIGHT_SECRET_KEY` env OR `data/.secret_key` (first-boot flock-protected gen, lines 47-95)；單一 key 跨所有 tenants
- **Read**: `backend/git_credentials.py` — `get_credential_registry`(:397), `find_credential_for_url`(:616), `get_token_for_url`(:645), `get_ssh_key_for_url`(:664), `get_webhook_secret_for_host`(:675), `get_credential_registry_async`(:763), `pick_account_for_url`(:919), `pick_default`(:1118), `pick_by_id`(:1156), `get_webhook_secret_for_host_async`(:1037)
- **Write**: `backend/routers/git_accounts.py` — `POST/PATCH/DELETE /api/v1/git-accounts[/{id}]`, `POST /api/v1/git-accounts/{id}/test`, `POST /api/v1/git-accounts/resolve` (debug)
- **Audit**: `backend.audit.log()` on every CRUD — `backend/git_accounts.py:335-366`
- **UI**: `components/omnisight/integration-settings.tsx`
- **Legacy migration**: `backend/legacy_credential_migration.py` — Settings scalars (`github_token`, `gitlab_token`, `gerrit_*`, `notification_jira_*`, ...) → `git_accounts` 行；deterministic id `ga-legacy-<platform>-<slug>`；`ON CONFLICT DO NOTHING`；kill switch `OMNISIGHT_CREDENTIAL_MIGRATE=skip`；scheduled drop: Phase 5-5

### 3.2 `llm_credentials` (Phase 5b-1/5b-2/5b-3)

- **Table**: `llm_credentials` (alembic 0029)
  - Columns: `id, tenant_id, provider, label, encrypted_value, metadata, auth_type, is_default, enabled, last_used_at, created_at, updated_at, version`
  - Providers: anthropic / google / openai / xai / groq / deepseek / together / openrouter / ollama (keyless: `metadata.base_url`, empty `encrypted_value`)
- **Encryption**: 同 `git_credentials`，共享 `backend/secret_store.py` 的 single Fernet key
- **Read**: `backend/llm_credential_resolver.py` — `get_llm_credential`(:287, async), `get_llm_credential_sync`(:338), `is_provider_configured`(:373), `_fetch_db_row`(:215), `_legacy_settings_credential`(:148)
- **Write / API**: `backend/routers/llm_credentials.py` — list / get / `POST/PATCH/DELETE /api/v1/llm-credentials[/{id}]` + `/test` (live probe)
- **Audit**: `backend/llm_credentials.py:328-341, 482-500, 597-610`
- **UI**: `components/omnisight/provider-card-expansion.tsx`
- **Legacy migration**: `backend/legacy_llm_credential_migration.py` — Settings scalars `<provider>_api_key` + `ollama_base_url` → `llm_credentials`；id `lc-legacy-<provider>`；kill switch `OMNISIGHT_LLM_CREDENTIAL_MIGRATE=skip`；scheduled drop: Phase 5b-5

### 3.3 `codesign_store` (P3 #288)

- **Storage**: file-backed JSON `data/codesign_store.json` (mode 0o600)；in-process singleton `CodesignStore`
- **Encryption**: Fernet via `backend.secret_store`；HSM-optional (`hsm_vendor` ∈ `none / aws_kms / gcp_kms / yubihsm`)
- **Read**: `backend/codesign_store.py` — `get`(:466), `decrypt_material`(:484), `decrypt_android_passwords`(:498), `list_records`(:478), `list_redacted`(:481)
- **Write**: `register_apple_cert`(:312), `register_provisioning_profile`(:354), `register_android_keystore`(:398), `delete`(:472)
- **Audit**: hash-chain `CodeSignAuditChain` 每次 sign emit `audit.log_sync()` — `backend/codesign_store.py:754-765`；tamper detect via `verify`(:729)
- **REST API**: 走 signing transport (P5 #290)，目前無直接 CRUD endpoint
- **UI**: 暫無（admin dashboard 屬未來 phase）

### 3.4 Cross-subsystem patterns to consolidate in AS.2 `token_vault`

| 重複 | 現況 | AS.2 方向 |
|---|---|---|
| Fernet master key | 三 subsystems 共用 `secret_store._fernet` | 單一 KMS-backed vault；retire 各自 Fernet |
| Tenant scoping | `tenant_id` column + `db_context.set_tenant_id()` | Vault 強制 RLS at storage layer |
| `is_default` 唯一性 | partial unique index + tx flip | Vault schema 強制 single default per (tenant, type, category) |
| Audit logging | 各自呼 `backend.audit.log()` | Vault hook 在 persistence layer |
| Fingerprinting | 各自呼 `secret_store.fingerprint()`（last-12 SHA-256）| Vault API 永遠返 fingerprint |
| LRU touch | `last_used_at` UPDATE on resolve | Vault touch-on-read atomic |
| API masking | 各 router 寫 `to_public_dict()` | Vault REST 保證 plaintext 不離 server |

---

## 4. Automation Client Bypass List (AS.0.6 input)

> **本節盤點所有「不走 user session、用其他機制 authenticate」的 client**——AS.0.6 要把它們**全部納入 bypass list**（不被 Turnstile / honeypot / OAuth gating 干擾）。漏一個就會 break production automation。

### 4.1 Internal automation scripts (built-in, ship with repo)

| Script | Auth 機制 | Endpoint 範圍 | bypass 對象 |
|---|---|---|---|
| `scripts/prod_smoke_test.py` | `OMNISIGHT_API_TOKEN` env → `Authorization: Bearer` (`scripts/prod_smoke_test.py:63,169-171`) | smoke test endpoints | session / CSRF |
| `scripts/usage_report.py` | optional `--token` CLI flag → `Authorization: Bearer` (`scripts/usage_report.py:93`) | usage report endpoints | session / CSRF |
| `scripts/check_fallback_freshness.py` | optional `Authorization: Bearer` (`scripts/check_fallback_freshness.py:69`) | fallback report endpoints | session / CSRF |
| `scripts/bootstrap_prod.sh` | 設 `OMNISIGHT_DECISION_BEARER` env (`scripts/bootstrap_prod.sh:188`) | first-boot setup | session / CSRF |
| `scripts/quick-start.sh` | Cloudflare API tokens (`Authorization: Bearer ${CF_TOKEN}`) | Cloudflare API only | n/a (external) |
| `scripts/migrate_legacy_credentials_dryrun.py` | reads env tokens (`OMNISIGHT_GITHUB_TOKEN`, `OMNISIGHT_GITLAB_TOKEN`) | local-only, no API call | n/a |

### 4.2 In-process bots / agents (act as service principals)

| Bot | 機制 | 備註 |
|---|---|---|
| `merger-agent-bot` Gerrit account | Gerrit credentials in `git_accounts` (per-account, encrypted) | O6 #269 — 唯一被授權對 merge-conflict resolution patchset 自打 +2 的 bot；O7 submit rule 仍要求 human +2 dual-sign |
| `lint-bot` / `security-bot` Gerrit accounts | 同上 | AI reviewer max +1 |
| `chatops` outbound senders | `OMNISIGHT_CHATOPS_LINE_CHANNEL_TOKEN` (Line OAuth) + Discord webhook URL + Teams incoming webhook URL | outbound only；inbound 走 4.4 webhook signatures |

### 4.3 CI / Renovate / external automation (configured per-deployment)

| Caller | 機制 | bypass 對象 |
|---|---|---|
| Renovate (`renovate.json`) | n/a — Renovate 推 PR 走 GitHub App，不直打 OmniSight backend | n/a |
| GitHub / GitLab / Gerrit / Jira webhooks | HMAC / Bearer signature (per `backend/routers/webhooks.py`) | session / CSRF / baseline (all allowlisted) |
| Discord / Teams / Line ChatOps inbound | Ed25519 / HMAC (per `backend/chatops/`) | session / CSRF / baseline (all allowlisted) |
| Prometheus scraper | optional `OMNISIGHT_METRICS_TOKEN` bearer | n/a (metrics 端點獨立) |

### 4.4 Test / dev bypass

| 機制 | 控制 | 行為 |
|---|---|---|
| `OMNISIGHT_AUTH_MODE=open` | env (`backend/auth.py:69-73`) | 所有 request 視為 anonymous super_admin (`_ANON_ADMIN`)；CSRF skip；**production-unsafe，prod 必設 `strict`** |
| `OMNISIGHT_AUTH_BASELINE_MODE=log` | env | allowlist 漏網 path 只 warn 不 reject |
| `OMNISIGHT_TEST_DOCKER_IMAGE_SIZE` | env | enable docker image size test gate |

> **沒有 `X-Test-Token` header 機制**——倉庫 grep `X-Test-Token` 0 命中。AS.0.6 列 bypass list 時不要再列這個——它是 AS roadmap 草稿時假想的、現況沒實作。

### 4.5 Bypass list（AS.0.6 草稿）— 必須讓以下 caller 跳過 Turnstile / honeypot / OAuth gating

1. **Authorization: Bearer `omni_*`**（K6 api_keys，scope-gated）
2. **Authorization: Bearer `<legacy>`**（`OMNISIGHT_DECISION_BEARER` env，K6 啟動時自動 migrate；移除前/後都要 work）
3. **Authorization: Bearer `<metrics-token>`** 對 `/metrics` 唯一
4. **`/api/v1/webhooks/*`** 走 per-service HMAC / Ed25519 / Bearer signature
5. **`/api/v1/chatops/webhook/*`** 走 per-platform signature
6. **`/api/v1/livez|readyz|healthz`** 等 probe paths
7. **`/api/v1/bootstrap/*`** first-boot wizard（必須 unauthenticated；AS Turnstile 在這裡會 break dr_drill / fresh-install）
8. **`/api/v1/auth/login`** 自身——AS.3 Turnstile 在 login 路徑要 fail-open 4 週（AS.0.5）
9. **`/api/v1/auth/mfa/challenge`** + WebAuthn challenge endpoints — Turnstile 已在 login 攔過
10. **`OMNISIGHT_AUTH_MODE=open`** 全套 disable AS gating（dev / test only）

> **AS.0.8 single-knob rollback** `OMNISIGHT_AS_ENABLED=false` 必須等價「以上 10 項目全 enabled」+「AS-added Turnstile / honeypot 行為全 noop」。

---

## 5. Encryption / secret plumbing summary

| Asset | Source | 跨 worker 一致性 |
|---|---|---|
| Fernet master key | `OMNISIGHT_SECRET_KEY` env OR `data/.secret_key` | first-boot via flock LOCK_EX (`backend/secret_store.py:47-95`)，所有 worker 從同檔讀；Answer #1 / #2 hybrid |
| `_fernet` instance | module-level cache (`backend/secret_store.py:44,98-103`) | 每 worker 自啟自享，從同 key 推同 instance — Answer #1 |
| `_DUMMY_PASSWORD_HASH` | login-path timing oracle defence (`backend/auth.py:97-103`) | 每 worker 算自己的 Argon2 dummy hash — Answer #1 |
| Argon2id `PasswordHasher()` | `_argon2_ph = _Argon2Hasher()` (`backend/auth.py:85`) | 同上 — Answer #1 |
| `_login_throttle` (rate limiter state) | in-memory dict (`backend/auth.py`) | **per-worker**；prod uvicorn `--workers N` 下不共享 — 故意（per-replica bucket，註解標明）— Answer #3 |
| `auth_baseline_mode` | env-driven module global | 每 worker 從同 env 推同值 — Answer #1 |

**AS.2 token vault implication**: AS.2 `oauth_tokens` 表 (alembic 0057) 的 `access_token_enc` / `refresh_token_enc` 必須走同一 Fernet key（避免引入第二個 master key + key-rotation hell）。`encryption_key_version` column 已在 AS spec 預留 → 為未來 KMS migration 預留 hook。

---

## 6. Critical gaps / risks discovered while inventorying

1. **`/auth/reset` + `/auth/forgot` 是 allowlist-only ghost**——allowlist 早 hardcode、handler 從未實作。AS.6.1 / AS.0.10 走 greenfield，但 allowlist 也得確認**這兩條不會 leak** unauthenticated POST 進入 backend（目前因 handler 不存在所以 404，但 baseline middleware 仍 skip）。AS.0 phase 要寫 regression test：`POST /api/v1/auth/reset` 返 404（不是 401，也不是 200）。
2. **Three independent `OMNISIGHT_DECISION_BEARER` checks**——`backend/routers/decisions.py:64`、`backend/routers/audit.py:38`、`backend/routers/profile.py:29` 各自 `os.environ.get()` inline check，與 K6 api_keys 平行。AS.0.4 migration plan 要顯式處理：保留環境變數 1 release cycle 後刪這三個 inline check（migrate_legacy_bearer 已負責資料層遷移，但 router 層不知道有 api_keys 行）。
3. **`OMNISIGHT_AUTH_MODE` 預設 `open`**——`backend/auth.py:70` 預設值是 prod-unsafe。AS.0.8 rollback knob 不能與這個衝突——`OMNISIGHT_AS_ENABLED=false` 不應改變 `OMNISIGHT_AUTH_MODE` 既有 semantics。
4. **單一 Fernet key 跨 tenant**——目前 `git_accounts` / `llm_credentials` / `codesign_store` 都共享同 key，無 per-tenant 隔離。AS.2 oauth_tokens 加入後 blast radius 變大；但 per-tenant key 是 AS.2 之後的工作（AS.0.4 不要 hard couple）。
5. **No self-serve signup means OAuth 上線後 product decision 必須先做**——既有 admin-create-user 是嚴格的 admin-controlled 路徑。AS.1 OAuth client core 先支援 backend，但「OAuth 第一次登入是否自動創 user」要走 `tenants.auth_features.oauth_signup_allowed`（建議納入 AS.0.2 alembic 0056 schema，與 `oauth_login` 並列）。
6. **`auth_features` JSONB 預設值的 audit trail**——AS.0.2 既有 tenant 預設 `oauth_login: false`，但寫法要保證「row 已存在 → 不 overwrite，row 不存在 → JSONB DEFAULT '{}'`」。建議走 server-side default + post-migration UPDATE 雙保險。

---

## 7. AS roadmap readiness check

| AS row | 是否被本次 inventory cover | 缺什麼才能往下走 |
|---|---|---|
| AS.0.2 | ✅ tenants 表已盤；alembic 編號預留 0056 | 寫 migration script + drift guard test |
| AS.0.3 | ✅ `users.auth_methods` set/array 設計可以 build on `password_history` schema | 規格確認：`auth_methods` enum: `password / oauth_<provider> / mfa_totp / mfa_webauthn / api_key` |
| AS.0.4 | ✅ 三個 inline `OMNISIGHT_DECISION_BEARER` check 找到，expand-migrate-contract 步驟可寫 | TBD：是否在同 PR 內順手砍三個 router inline check |
| AS.0.5 | ✅ allowlist 邊界已清楚 | TBD：fail-open 警告 log line format |
| AS.0.6 | ✅ 自動化 client bypass list 已草稿 (§4.5) | 確認操作員 vs CI 邊界——operator 走 cookie session、CI 走 bearer api_key、無中間態 |
| AS.0.7 | ⚠️ honeypot 須避免衝撞既有 form field name | 列 OmniSight 已有 form input names → grep 確認沒撞 |
| AS.0.8 | ✅ `OMNISIGHT_AS_ENABLED=false` ↔ §4.5 之 10 項 noop | TBD：env wiring location（建議 `backend/config.py` 加旗，AS lib 統一 read it） |
| AS.0.9 | ✅ 5 顆 critical：(1) 既有 password login (`/api/v1/auth/login`) (2) 既有 password+MFA (`/auth/mfa/challenge`) (3) API key auth (Bearer `omni_*`) (4) 無 test token bypass，改測 `OMNISIGHT_DECISION_BEARER` legacy 行為 (5) `OMNISIGHT_AS_ENABLED=false` rollback knob | regression test 寫成 `backend/tests/test_as_compat_regression.py` |
| AS.0.10 | n/a (greenfield) | core lib spec 已在 TODO row，與本 inventory 無 dependency |

---

## 8. Single-source-of-truth links

- 主 auth module: `backend/auth.py`（2170 行）
- Baseline middleware allowlist: `backend/auth_baseline.py`
- API key store: `backend/api_keys.py` + alembic 0044/0046
- MFA: `backend/mfa.py`、`backend/routers/mfa.py`
- Git creds: `backend/git_credentials.py` + `backend/git_accounts.py` + `backend/routers/git_accounts.py` + alembic 0027
- LLM creds: `backend/llm_credentials.py` + `backend/llm_credential_resolver.py` + `backend/routers/llm_credentials.py` + alembic 0029
- Code-sign store: `backend/codesign_store.py`
- Webhooks: `backend/routers/webhooks.py`、`backend/chatops/`
- Frontend login: `app/login/page.tsx`
- Frontend wizard: `app/bootstrap/page.tsx`
- Frontend invite: `app/invite/[token]/page.tsx`
- Settings UI for creds: `components/omnisight/integration-settings.tsx` (`git`)、`components/omnisight/provider-card-expansion.tsx` (`llm`)

---

**End of AS.0.1 inventory**. 下一步 → AS.0.2 alembic 0056 `tenants.auth_features` migration script + drift guard test。
