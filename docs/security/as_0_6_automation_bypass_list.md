# AS.0.6 — Automation Bypass List

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Scope**: 釘住 OmniSight 自家 Turnstile / honeypot / OAuth gating（AS.3 / AS.4 / AS.6.x） 上線後 **自動化 client 必經的三條 bypass mechanism**——API key auth、per-tenant IP allowlist、test-token header——的儲存 schema、precedence、audit row 命名、Settings UI 行為、月結報表 spec、與 AS.0.5 phase / AS.0.8 single-knob / AS.0.9 compat regression 的解耦語義。本 row 與 AS.0.5 共同 mitigate 設計 doc §10 R34（Turnstile lock 既有自動化 client）；AS.0.5 釘 Turnstile fail-open 時間軸、本 row 釘 bypass 鑑別維度。
>
> **目標讀者**：(1) 寫 AS.3.1 `backend/security/bot_challenge.py` 的人——本文件規範 `_BYPASS_PATH_PREFIXES` / `_BYPASS_CALLER_KINDS` 的 SoT inventory 與三 axis 的 precedence。(2) 寫 AS.6.3 OmniSight self login/signup/password-reset/contact form Turnstile backend wire 的人——本文件規範 caller kind 鑑別流程（先 auth 後 challenge）。(3) 寫 admin Settings UI per-tenant IP allowlist 的人——本文件規範 storage shape + 校驗規則 + audit chain。(4) 寫 AS.0.9 compat regression test #3 / #4 的人——本文件規範 test-token mechanism 的 wire 介面（design doc §3.8 已釘但實作細節在此）。(5) 寫 AS.5.2 per-tenant dashboard / 月結報表的人——本文件釘 monthly aggregate 的指標 schema。
>
> **不在本 row 範圍**：實際 `backend/security/bot_challenge.py` module（AS.3.1）、test-token env wiring 程式碼（AS.6.3 落地時挾帶）、Settings UI front-end pages（AS.7.x）、月結報表 cron 程式碼（AS.5.2）、IP allowlist matcher 的 ipaddress lib 細節（AS.3.1 內建 helper、本 row 規範 contract）。本 row 是 **plan-only design freeze**——下游 PR 必須遵守此處釘的 bypass semantics 與 schema。

---

## 1. 為什麼必須有專屬的 automation bypass design freeze

### 1.1 R34 risk + AS.0.1 §4.5 已盤點的 production caller surface

設計 doc §10 R34 釘了「Turnstile 上線後鎖既有自動化 client」這條 risk，AS.0.5 §1.1 已詳細展開：

- `scripts/prod_smoke_test.py` / `scripts/usage_report.py` / `scripts/check_fallback_freshness.py` / `scripts/bootstrap_prod.sh` 等 bearer-only caller 沒 browser context、Turnstile widget 拿不到 token；無 bypass = 全 break。
- `merger-agent-bot` / `lint-bot` / `security-bot` Gerrit account 直打 backend、走 git_accounts 內 encrypted_token；無 bypass = O6 conflict-resolution flow 全 break。
- `chatops` outbound senders / Webhook receivers 走 per-platform signature verify、自帶不依賴 session；這類 caller 走 path-based allowlist（AS.0.1 §4.5 §4-§5）已在 AS.0.5 §4 釘 precedence。
- Prometheus scraper、Cloudflare Tunnel `/livez` health probe、docker compose healthcheck — path-based bypass、與 AS.0.5 §4 釘的 precedence 一致。

AS.0.5 §4 把 bypass list 「對齊 inventory」（path / caller-kind 兩維），但**沒**把以下三維展開：

- **API key auth 的「auth-then-challenge」順序**——api_key 驗過後 Turnstile 是否還必要？bypass row 該寫 `bypass_apikey` 還是與 `pass` 並列？
- **IP allowlist 的 storage schema**——per-tenant 還是 per-api_key？JSON column 還是子表？CIDR 校驗在 application 層還是 DB constraint？
- **Test-token header 的 lifecycle**——env-based 還是 DB-based？rotation 機制？是否限 endpoint？

本 row 是這三維的 design freeze。

### 1.2 與 AS.0.5 的分工邊界

| Concern | AS.0.5 釘 | AS.0.6 釘 |
|---|---|---|
| Turnstile fail-open 時間軸 | ✓（4 phase） | ✗ |
| Bypass list path / caller-kind 對齊 inventory | ✓ | ✓（本 row 細化） |
| Bypass 三 axis（API key / IP allowlist / test-token）的儲存 schema | ✗ | ✓ |
| Bypass row 在 phase metric denominator 的處理 | ✓（不算 unverified rate） | ✓（refine：bypass kind 細分） |
| Per-tenant IP allowlist Settings UI | ✗ | ✓ |
| Monthly bypass aggregate 報表 | ✗ | ✓ |
| Test-token wire 介面 | ✗ | ✓ |
| Phase advance acceptance gate | ✓ | ✗（但 §6 列 cross-check 條件） |
| Drift guard for bypass list 對齊 | ✓（AS.0.5 §8.1） | ✓（refined 加 IP allowlist + test-token） |

**邊界 invariant**：AS.0.5 是 timeline / phase / advance gate 規範；AS.0.6 是 axis / mechanism / storage 規範。任一 PR 動 bypass 行為必同時滿足兩文件條文。

---

## 2. 三條 bypass mechanism 的精確定義

### 2.1 Mechanism A — API key auth（auth-then-challenge）

**Trigger**：request 帶 `Authorization: Bearer <token>` 且 `validate_bearer()` 回傳 non-None ApiKey OR `_legacy_bearer_matches()` 對 `OMNISIGHT_DECISION_BEARER` env 對齊。

**Why bypass**：API key 已是 `["*"]` scope authenticated principal，已過 K6 hash + audit + rate-limit；對它再加 Turnstile 等於要求「人類在自動化 client 後座按 widget」，邏輯矛盾。Browser-based widget 預期 user 互動、API key caller 沒 user。

**Bypass scope**（具體到三 layer）：

| Layer | bypass 行為 | 為何 |
|---|---|---|
| AS.3 Turnstile widget 前置驗證 | skip — `bot_challenge.verify()` 直接 return `BotChallengeResult.bypass(kind="apikey")` | API key 已 authenticated，captcha 是 anti-bot；自動化是「合法 bot」 |
| AS.4 honeypot field DOM check | skip — handler 跳過 `_honeypot_validate()` | API key caller 不走 form / 不渲 DOM，沒有 honeypot field |
| AS.6 OAuth login gating | n/a | API key 不經 login flow，OAuth UI 對它不存在 |
| AS.0.5 phase Phase 3 fail-closed | skip — Phase 3 fail-closed branch 的 401 不對 API key caller 觸發 | bypass 永遠先於 phase fail-closed |
| K6 audit chain (`api_keys.last_used_*`) | **不 skip**——continues to record `last_used_ip / last_used_at` | 這是 K6 的契約、與 AS bypass 正交 |
| `auth_baseline.AUTH_BASELINE_ALLOWLIST` | n/a — middleware 已先過（cookie OR bearer，per `auth_baseline._has_valid_bearer_token`） | baseline 是 floor；AS bypass 是 floor 之上的次層 gate |

**Caller kind 細分**：本 row 在 `bypass_apikey` 之外**不**再細分 `apikey_omni` / `apikey_legacy`（AS.0.5 §8.1 drift guard 已含 set），AS.3.1 module 內 `_BYPASS_CALLER_KINDS` 必含 `{"apikey_omni", "apikey_legacy", "metrics_token"}` 三項，audit row metadata 帶 `caller_kind` field 細分。

### 2.2 Mechanism B — Per-tenant IP allowlist（Settings UI 可加）

**Trigger**：request `X-Forwarded-For` 末段（Caddy / Cloudflare 取真實 client IP，per `backend/main.py:439` 既有 trust chain）match `tenant.auth_features.automation_ip_allowlist` 內任一 CIDR/IP。

**Why bypass**：CI runner / monitor / Renovate self-hosted instance 等**長期穩定 source IP** 的 caller，沒有 browser context、且因「定 IP」可作 strong identity proof（與 dynamic ISP IP 不同）。Settings UI 讓 tenant admin 自行管理，不走 operator-only env knob。

**Storage schema**（複用 AS.0.2 alembic 0056 既有 `tenants.auth_features` JSONB column，**不**新增 alembic）：

```jsonc
// tenants.auth_features 加入新 key（既有 keys: oauth_login / turnstile_required / honeypot_active / auth_layer）
{
  "automation_ip_allowlist": [
    // 每 entry 是字串，IPv4 / IPv6 single-IP 或 CIDR；application 層用 ipaddress.ip_network strict=False parse
    "192.0.2.42/32",                       // CI runner single IP
    "203.0.113.0/24",                      // monitor net block
    "2001:db8:abcd::/48"                   // IPv6 prefix
  ]
}
```

**為何走 JSONB 既有 column 而非新表**：

- AS.0.2 已開 `auth_features` 為「per-tenant auth knob 集中地」；automation_ip_allowlist 是 auth knob 的自然延伸、不應拆 satellite 表。
- 既有 tenant 預設 `{}` → AS.3.1 helper 讀不到 key → return empty allowlist → 行為等同「無 bypass」（與 AS.0.2 既有 zero-行為-變動 invariant 一致）。
- 拆獨立表會引入 1:N relation + RLS / audit hook 複製（per AS.0.4 §3 vault unification 反面教訓——獨立 lifecycle、獨立 contract phase）；JSONB array 對 ≤ 100 entry per tenant 完全足夠（assumption: 5×CI + 3×monitor + 10×Renovate self-hosted = 不會破百）。

**校驗規則**（Settings UI POST handler 在 application 層強制）：

1. 每 entry parseable by `ipaddress.ip_network(entry, strict=False)`；不可 parse → 422。
2. **拒絕 RFC1918 / loopback / link-local 以外的 *too-broad* CIDR**：`/0` / `/8`（IPv4）/ `/16`（IPv6）拒絕；`/24`（IPv4）/ `/48`（IPv6）警告但允許（admin opt-in，audit row 標 `wide_cidr=true`）。
3. **Per-tenant 上限 100 entry**——超過 422，避免 admin 誤把整 ASN 貼進 allowlist。
4. **Validation cycle invariant**：UI 預覽「將 bypass 的 IP 範圍」+「估計 IP 總數」，admin 顯式確認後才 PATCH。
5. **Audit chain on every change**：`audit.log(action="tenant.automation_ip_allowlist_update", actor=admin_id, before=[...], after=[...])`，with diff helper。

**Match algorithm**（AS.3.1 內建 helper 必走）：

```python
# Pseudocode — AS.3.1 PR 落地時實作
def _ip_in_tenant_allowlist(client_ip: str, tenant_id: str) -> bool:
    """Return True if client_ip falls inside any CIDR in
    tenants.auth_features.automation_ip_allowlist.
    Pre-conditions:
      * client_ip 來源是 X-Forwarded-For 末段（trust chain via Caddy + Cloudflare）
      * tenant_id 是 RLS context-bound（已過 auth_baseline / current_user 鑑別）
    """
    import ipaddress
    try:
        ip = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        return False  # 無法 parse → 永不 bypass（fail-closed for IP-axis）
    cidrs = _tenant_features(tenant_id).get("automation_ip_allowlist", [])
    for entry in cidrs:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue  # 越過 corrupt entry，不 raise（log warn）
        if ip.version == net.version and ip in net:
            return True
    return False
```

**Failure mode 釘死**：parse 失敗 / 4-vs-6 mismatch / corrupt entry → 該 entry **不**算 hit；整 allowlist match 失敗 → fall through 到下一 axis（不 fail-closed bypass，因為 bypass 是 permissive、不能因 IP 邏輯爆而拒絕請求）。

### 2.3 Mechanism C — Test-token header（CI / e2e 限定）

**Trigger**：request header `X-OmniSight-Test-Token: <token>` 且 token 與 env `OMNISIGHT_TEST_TOKEN` constant-time compare match。

**Why bypass**：CI / e2e 跑 login flow 時無法填 Turnstile widget（無 browser）也不適合走 API key（因為要驗 password 流程本身），需要一條「跳 Turnstile / honeypot 但仍走 password / MFA」的測試路徑。設計 doc §3.6 第 3 條 + §3.8 #4 已釘為 compat regression critical 之一。

**Lifecycle / security model**：

- **Env-based、不入 DB**：`OMNISIGHT_TEST_TOKEN` env var；未設或為空字串 → 整 mechanism noop（任何 header value 都不 bypass）。
- **Default unset in production image**：`scripts/deploy.sh` 不寫 `OMNISIGHT_TEST_TOKEN` env；CI runner / e2e 自帶 secret manager 注入。
- **Constant-time compare**：`secrets.compare_digest(header_value, expected)`，禁用 `==`。
- **Min length 32 chars**：env 設定但 < 32 chars → backend 啟動 fail-fast `ValueError("OMNISIGHT_TEST_TOKEN must be ≥ 32 chars or unset")`。
- **每次 set 必觀察 7 天**：operator 設 env 後必過 7 天才能 advance any phase（AS.0.5 phase advance gate 加項：no `bypass_test_token` audit row from production tenant in last 7d，per §6 below）。
- **No DB-based rotation**：rotation = 改 env + restart；不引入 token vault 介面（避免 test-token 被當作 OAuth scope 一部分擴散）。
- **Header constant**：固定為 `X-OmniSight-Test-Token`（與 design doc §3.6 一致），AS.3.1 内 `_TEST_TOKEN_HEADER` 常數匯出，drift guard test 禁 inline 字串。

**Bypass scope**（與 Mechanism A/B 對照）：

| Layer | A=apikey | B=ip_allowlist | C=test_token |
|---|---|---|---|
| Turnstile widget | bypass | bypass | bypass |
| Honeypot field | bypass（無 form） | bypass（form 走但 honeypot 跳） | bypass（form 走但 honeypot 跳） |
| OAuth gating | n/a | n/a | bypass（測 OAuth flow 時 dev 用） |
| Password / MFA verify | **走**（K6 contract） | **走** | **走**（critical：test-token 不繞過 password） |
| Audit row | `bypass_apikey` | `bypass_ip_allowlist` | `bypass_test_token` |

**Critical invariant**：test-token bypass `bot_challenge` 與 `honeypot`，**不** bypass password / MFA。也就是 e2e test 必須仍提供 valid credentials 才能拿 session。否則 test-token 等同 super-admin escalation key，違反「token 不取代 password」spec。

### 2.4 三 mechanism 一覽表

| Axis | A — API key | B — IP allowlist | C — Test token |
|---|---|---|---|
| Storage | `api_keys` 表（K6 alembic 0011） + `OMNISIGHT_DECISION_BEARER` env (legacy) | `tenants.auth_features.automation_ip_allowlist` JSONB array | `OMNISIGHT_TEST_TOKEN` env |
| Identity proof | bearer secret hash + scope match | source IP in CIDR | header secret + constant-time compare |
| Per-tenant 控制 | api_keys 表 RLS（tenant_id column） | tenant admin Settings UI | operator only（env） |
| Lifecycle | K6 admin UI CRUD + rotate / revoke | Settings UI PATCH | env set / unset + restart |
| Audit row 命名 | `bot_challenge.bypass_apikey` | `bot_challenge.bypass_ip_allowlist` | `bot_challenge.bypass_test_token` |
| Audit metadata | `caller_kind` ∈ {apikey_omni, apikey_legacy, metrics_token}, `key_id`, `key_prefix` | `cidr_match`, `client_ip_subnet`（per-AS.0.1 `_subnet_prefix` /24 or /64）, `wide_cidr` | `token_fp` (last-12 SHA-256), `widget_action` |
| Skip Turnstile | yes | yes | yes |
| Skip honeypot | yes | yes | yes |
| Skip password / MFA | **no**（K6 不變） | **no** | **no**（critical） |
| Production default | api_keys 表預設空、env unset | `auth_features.automation_ip_allowlist` 預設 `[]` | env unset |
| Phase metric denominator | excluded（per AS.0.5 §4） | excluded | excluded |
| Phase advance gate cross-check | bypass row caller_kind 落 `apikey_*` | bypass row 落 `bypass_ip_allowlist` 不可超 7-day rolling 5%（避免 IP allowlist 取代真 challenge） | bypass row in production tenant ≥ 1 = phase advance block（test-token 預期僅 dev/CI tenant 出現） |

---

## 3. Audit event canonical names（refine AS.0.5 §3）

AS.0.5 §3 已釘 5 個 `bypass_*` event：

```
bot_challenge.bypass_apikey
bot_challenge.bypass_webhook
bot_challenge.bypass_chatops
bot_challenge.bypass_bootstrap
bot_challenge.bypass_probe
```

本 row **加** 2 個 event（保留 AS.0.5 已釘的 5 個不動）：

```python
# AS.3.1 backend/security/bot_challenge.py 必須額外匯出（plan §3 釘）
EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST = "bot_challenge.bypass_ip_allowlist"  # AS.0.6 §2.2
EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN = "bot_challenge.bypass_test_token"      # AS.0.6 §2.3
```

合計 7 個 `bypass_*` event。AS.0.5 §3 表「Schema 約束」延伸補：

| Event | `metadata` 必含 |
|---|---|
| `bypass_apikey` | `caller_kind`（apikey_omni / apikey_legacy / metrics_token）, `key_id`, `key_prefix`, `widget_action` |
| `bypass_ip_allowlist` | `cidr_match`, `client_ip_subnet`（/24 IPv4 or /64 IPv6 prefix per `_subnet_prefix`）, `wide_cidr`（true if /0–/24 IPv4 or /0–/48 IPv6）, `widget_action` |
| `bypass_test_token` | `token_fp`（last-12 SHA-256 hex of token）, `widget_action`, `tenant_id_or_null`（production tenant 出現 = phase advance block trigger） |
| `bypass_webhook` / `bypass_chatops` / `bypass_bootstrap` / `bypass_probe` | per AS.0.5 §3 既定 |

**Severity 等級**：

- `bypass_apikey` / `bypass_webhook` / `bypass_chatops` / `bypass_bootstrap` / `bypass_probe` → `info`（routine）。
- `bypass_ip_allowlist` → `info`，但 `wide_cidr=true` 時 → `warn`（admin 加大網段 = 風險升高）。
- `bypass_test_token` → `warn`（test-token 在 production tenant 出現必引 ops 注意）。

---

## 4. Bypass 三 axis 的 precedence（refine AS.0.5 §4）

AS.0.5 §4 釘的 5 層 precedence（最高 → 最低）：

1. `OMNISIGHT_AUTH_MODE=open`
2. `OMNISIGHT_AS_ENABLED=false`
3. AS.0.6 bypass list（path / caller match）
4. `tenants.auth_features.turnstile_required`
5. Phase 1/2/3 共用 verify 路徑

本 row **不**改動上述 5 層；對「第 3 層 AS.0.6 bypass list」內三 axis 的 internal precedence 釘死：

**Axis 內 precedence（最高 → 最低）**：

1. **A — API key auth**（已 authenticated，最 explicit identity proof）
2. **C — Test token header**（顯式 test override，預期僅 dev/CI tenant 出現；放在 B 之前以便 e2e test 即使 source IP 不在 allowlist 仍可走 test path）
3. **B — IP allowlist**（implicit identity proof，依靠 source IP；最後判定）

**為何 A 必先於 C**：API key caller 同時可能帶 X-OmniSight-Test-Token（CI 自動化既用 api_key 又跑 e2e），audit row 應 attribute 為 `bypass_apikey`（更具體 identity）；若 C 先於 A，CI runner 看起來像「test traffic」而非「authenticated automation」。

**為何 C 必先於 B**：CI runner 可能跨 IP（GitHub Actions runner IP 池動態），其 source IP 不一定在 allowlist；test-token 顯式提供身分證明，比 IP 更可靠。

**單條 request 命中多 axis 時 audit 行為**：只寫**最高 precedence 命中** axis 的 `bypass_*` row（避免一條 request 寫 N row 污染 phase metric denominator）；audit metadata 加 `also_matched`（list of lower-precedence axis names），讓 ops grep 可推斷 belt-and-braces 配置。

```jsonc
// 範例：CI runner 同時 api_key + test-token + IP allowlist
{
  "action": "bot_challenge.bypass_apikey",
  "metadata": {
    "caller_kind": "apikey_omni",
    "key_id": "ak-7f3a9c1b2d",
    "key_prefix": "omni_a1b",
    "widget_action": "login",
    "also_matched": ["test_token", "ip_allowlist"]
  }
}
```

---

## 5. Settings UI for IP allowlist (Mechanism B)

### 5.1 Endpoint contract

| Method | Path | RBAC | 行為 |
|---|---|---|---|
| GET | `/api/v1/admin/tenants/{tenant_id}/automation-ip-allowlist` | `require_admin` (tenant scope) OR `require_super_admin` | 回 current array + last update audit row |
| PATCH | `/api/v1/admin/tenants/{tenant_id}/automation-ip-allowlist` | 同上 | body: `{"entries": [...], "reason": "free text"}`；server 校驗（§2.2）+ audit + 寫 `tenants.auth_features.automation_ip_allowlist` |
| DELETE | `/api/v1/admin/tenants/{tenant_id}/automation-ip-allowlist/{entry_index}` | 同上 | 删單條（合 PATCH 也可，但 UI 友善獨立 endpoint） |

**Body shape (PATCH)**：

```jsonc
{
  "entries": [
    {"cidr": "192.0.2.0/24", "label": "GitHub Actions self-hosted runner", "added_by_email": "ops@example.com"},
    {"cidr": "203.0.113.42", "label": "Datadog synthetic monitor", "added_by_email": "ops@example.com"}
  ],
  "reason": "Add Datadog monitor IP after migration to new region"
}
```

> **Storage 簡化**：`tenants.auth_features.automation_ip_allowlist` 只存字串 array（§2.2 schema），label/added_by/timestamp 寫 `audit_log` 不寫 JSONB（避免 column 變超表、避免每次 PATCH 觸發整 array rewrite 撞 G4 optimistic lock 噪音）。

**校驗順序**：

1. RBAC 過 → 2. body 結構 → 3. 每 entry parse `ipaddress.ip_network(strict=False)` → 4. 拒絕 too-broad（§2.2 #2）→ 5. 上限 100 → 6. 比對 before/after diff → 7. 寫 audit → 8. UPDATE `tenants.auth_features` JSONB merge（不覆蓋其他 keys）→ 9. invalidate per-tenant cache (`Y9` cache TTL 60s eventual consistency window 容忍)。

### 5.2 Audit chain shape

```jsonc
{
  "action": "tenant.automation_ip_allowlist_update",
  "actor_user_id": "u-admin-1",
  "tenant_id": "t-acme",
  "metadata": {
    "before": ["192.0.2.0/24"],
    "after": ["192.0.2.0/24", "203.0.113.42/32"],
    "added": ["203.0.113.42/32"],
    "removed": [],
    "reason": "Add Datadog monitor IP after migration to new region",
    "wide_cidr_added": false,
    "entry_count_before": 1,
    "entry_count_after": 2
  },
  "severity": "info"
}
```

`severity=warn` if `wide_cidr_added=true` 或 entry_count crosses 50（halfway to 100 cap）。

### 5.3 UI 行為（AS.7 frontend follow-up 必遵守）

- 表格顯示 entries 列、增刪按鈕、preview 按鈕（call helper 估「將 bypass 的 IP 範圍 + 預估 IP 總數」）。
- PATCH 前必經 confirmation modal，顯示 diff（added / removed）+ 「將 bypass Turnstile + honeypot」warning。
- 顯示「最近 7 天 `bypass_ip_allowlist` audit row 數」by entry，let admin 看哪些 entry 真在用、哪些可刪。
- 「Tenant 全 disable」開關走 `auth_features.turnstile_required` flip（複用 AS.0.5 phase 3 機制），不需另一 toggle。

---

## 6. Monthly summary report（per-tenant）

設計 doc §3.6 釘「全部 bypass 寫 audit row + 月結報表給 admin 審」。本 row 規範 cron + report shape。

### 6.1 Cron job spec

- **Owner**：AS.5.2 dashboard PR 落地時挾帶 `scripts/cron_automation_bypass_monthly.py`。
- **Schedule**：每月 1 日 09:00 UTC（per-tenant 同 weekly Turnstile alert 對齊；per AS.0.5 §2.3 weekly cron 為 09:00 UTC，monthly 同時段避免 cron 衝突）。
- **Scope**：對每 tenant 跑一次，無事件 tenant 也發 zero-event 報告（避免 admin 因為「沒收到報告」誤以為系統壞）。

### 6.2 Report metric schema

每月每 tenant 一份 email + dashboard 持久化 row（`audit_log` table action=`automation_bypass.monthly_summary`，metadata 載完整 stats）：

```jsonc
{
  "tenant_id": "t-acme",
  "period_start_utc": "2026-04-01T00:00:00Z",
  "period_end_utc": "2026-04-30T23:59:59Z",
  "totals": {
    "bypass_apikey": 12053,
    "bypass_ip_allowlist": 4221,
    "bypass_test_token": 0,
    "bypass_webhook": 8801,
    "bypass_chatops": 412,
    "bypass_bootstrap": 0,
    "bypass_probe": 1827392
  },
  "axis_breakdown": {
    "apikey": {
      "by_caller_kind": {"apikey_omni": 11500, "apikey_legacy": 553, "metrics_token": 0},
      "top_keys": [
        {"key_id": "ak-7f3a", "key_prefix": "omni_a1b", "count": 6801, "name": "github-actions-prod"},
        {"key_id": "ak-2c8e", "key_prefix": "omni_xz0", "count": 5252, "name": "datadog-monitor"}
      ]
    },
    "ip_allowlist": {
      "by_subnet": [
        {"subnet": "192.0.2", "count": 3201},
        {"subnet": "203.0.113", "count": 1020}
      ],
      "wide_cidr_hits": 0
    },
    "test_token": {
      "any_hit_in_production_tenant": false
    }
  },
  "anomalies": {
    "wide_cidr_warnings": [],
    "test_token_in_production": false,
    "apikey_legacy_still_used": true,  // R34 cleanup hint，per AS.0.4 Track B
    "new_top_caller_kind_this_month": null  // diff vs previous month
  },
  "phase_state_snapshot": {
    "current_phase": 2,
    "advance_eligible": true,
    "block_reasons": []
  }
}
```

### 6.3 Email body 規範

- Subject: `[OmniSight] Tenant <name> 自動化 bypass 月報 — YYYY-MM`
- Body 開頭一句話 summary（總 bypass 次數、最大占比 axis、有無 anomaly）。
- 表格：7 種 bypass event 月度計數 + 月對月 delta（首月顯示「首次報告」）。
- Anomaly 段落：wide CIDR / test-token leak to prod / legacy bearer 持續使用 / 新 caller 種類首次出現。
- 行動建議：基於 anomaly 給 1-3 條具體 next step（e.g.「`apikey_legacy_still_used=true` → 規劃移除 `OMNISIGHT_DECISION_BEARER`，per AS.0.4 Track B contract phase」）。
- 末尾：dashboard 連結（取代 email 詳細數據）+ 「停止月報」opt-out 連結（admin 顯式關，預設 opt-in）。

### 6.4 Privacy / data-handling

- `key_id` / `key_prefix` 可入 email（admin 自家資料）；**`key_hash` 永遠不入** email/dashboard。
- IP 顯示走 `_subnet_prefix`（IPv4 /24 or IPv6 /64，per `backend/auth.py:280-319`），full IP 不入 email；dashboard 給 super_admin 才顯示 full IP（debug 用，audit access）。
- `token_fp` 的 last-12 SHA-256 寫 audit、不入 email body（避免 forwarding leak token fingerprint）。

---

## 7. Acceptance criteria（per mechanism）

### 7.1 Mechanism A（API key bypass）

本 row 不新增實作——既有 K6 `validate_bearer()` + `_legacy_bearer_matches()` 已 production-ready。AS.3.1 落地時 acceptance：

- [ ] AS.3.1 `bot_challenge.verify()` 內首條 short-circuit `if request.state.api_key: return BotChallengeResult.bypass(kind="apikey")`（leverage `auth_baseline._has_valid_bearer_token` 已 cache `request.state.api_key`，避免 double-validate）。
- [ ] Audit row metadata `caller_kind` 對齊 AS.0.5 §8.1 drift guard 期望集合 `{"apikey_omni", "apikey_legacy", "metrics_token"}`。
- [ ] Phase advance gate 驗：legacy bearer 出現 → 標記 `apikey_legacy_still_used` anomaly，但**不** block phase advance（cleanup is AS.0.4 Track B 工作，不卡 phase）。
- [ ] AS.0.9 compat regression #3：API key client login flow，無 Turnstile widget、login 成功 + audit row 落 `bypass_apikey`。

### 7.2 Mechanism B（IP allowlist）

- [ ] AS.0.6 follow-up PR（**新 owner row：建議 AS.6.4 admin Settings UI block 內挾帶**，本 plan 不獨立開）：
  - alembic 不需要新 migration（複用 AS.0.2 既有 JSONB column）。
  - `backend/routers/admin_tenants.py` 加 GET / PATCH / DELETE handler（§5.1）。
  - `backend/security/bot_challenge.py::_ip_in_tenant_allowlist()` helper（§2.2 algorithm）。
  - Settings UI page (`app/admin/tenants/[tid]/automation-ip-allowlist/page.tsx`)。
  - Audit chain `tenant.automation_ip_allowlist_update` 落 row。
  - Drift guard test：`auth_features` schema 沒有 `automation_ip_allowlist` key 時 helper return empty allowlist；entry corrupt 時 helper skip 該 entry 不 raise。
- [ ] 新 tenant INSERT path（`admin_tenants.py` + `bootstrap.py`）seed `automation_ip_allowlist: []`（與 AS.0.2 既有「全開」pattern 對齊：默認空 list = 不 bypass，與「functionality on but no IP added」一致）。
- [ ] AS.0.9 compat regression：source IP 在 allowlist → bypass `bypass_ip_allowlist`、不在 → 走正常 challenge 路徑（不誤 bypass）。

### 7.3 Mechanism C（Test token）

- [ ] AS.3.1 PR 落地時 `_TEST_TOKEN_HEADER = "X-OmniSight-Test-Token"` 常數匯出。
- [ ] `_validate_test_token(header_value)` helper：env unset → return False、< 32 chars env → fail-fast on startup、constant-time compare。
- [ ] Lifespan 啟動時驗 env：`OMNISIGHT_TEST_TOKEN` 有設且 >= 32 chars 才 accept；< 32 chars 設 = `ValueError`，backend refuse to start。
- [ ] `bot_challenge.verify()` 內 axis-C short-circuit 在 axis-A 之後 axis-B 之前（per §4 precedence）。
- [ ] AS.0.9 compat regression #4：CI 設 env + 帶 header → bypass `bypass_test_token`、login 仍走 password verify、wrong header value → 403 (test-token attempted but mismatch — audit `bot_challenge.test_token_mismatch` warn) instead of bypass。
- [ ] Production deployment SOP：`scripts/deploy.sh` 不寫 `OMNISIGHT_TEST_TOKEN`；HANDOFF.md AS.0.6 entry 顯式標 production should not set it。

### 7.4 Cross-axis acceptance

- [ ] AS.0.5 §8.1 drift guard refine：`expected_kinds` 加 `{"apikey_omni", "apikey_legacy", "metrics_token", "ip_allowlist", "test_token"}` 五項。
- [ ] Phase advance helper 加 cross-check：`bypass_test_token` row 在 production tenant ≥ 1 → block phase advance；message: 「test-token leak to production — clear env + audit row not 7d old」。
- [ ] Per-tenant dashboard 顯示「最近 30 天 bypass 占比 by axis」widget（給 admin 自評 IP allowlist 是否 over-permissive）。

---

## 8. Drift guards（refine AS.0.5 §8）

### 8.1 Bypass list inventory 對齊（refine AS.0.5 §8.1）

```python
def test_as_0_6_bypass_list_aligned_with_inventory():
    """AS.0.6 §2 invariant: bot_challenge bypass mechanism 對齊 AS.0.1 §4.5 inventory + AS.0.6 三 axis."""
    from backend.security.bot_challenge import _BYPASS_PATH_PREFIXES, _BYPASS_CALLER_KINDS
    expected_paths = {
        "/api/v1/livez", "/api/v1/readyz", "/api/v1/healthz",
        "/api/v1/bootstrap/", "/api/v1/webhooks/", "/api/v1/chatops/webhook/",
        "/api/v1/auth/oidc/", "/api/v1/auth/mfa/challenge",
        "/api/v1/auth/mfa/webauthn/challenge/",
    }
    # AS.0.6 加入 ip_allowlist + test_token 兩 caller kind
    expected_kinds = {
        "apikey_omni", "apikey_legacy", "metrics_token",
        "ip_allowlist", "test_token",
    }
    assert expected_paths.issubset(_BYPASS_PATH_PREFIXES), (
        f"AS.0.5 §4 / AS.0.1 §4.5 bypass list drift: missing {expected_paths - _BYPASS_PATH_PREFIXES}"
    )
    assert expected_kinds.issubset(_BYPASS_CALLER_KINDS), (
        f"AS.0.6 §2 caller kinds drift: missing {expected_kinds - _BYPASS_CALLER_KINDS}"
    )
```

### 8.2 Audit event 7 個 bypass 常數匯出 guard

```python
def test_as_0_6_bypass_event_constants_exhaustive():
    """AS.0.6 §3 invariant: 7 個 bypass_* event 常數齊備、無 inline 字串."""
    from backend.security import bot_challenge as bc
    expected = {
        "bot_challenge.bypass_apikey",
        "bot_challenge.bypass_webhook",
        "bot_challenge.bypass_chatops",
        "bot_challenge.bypass_bootstrap",
        "bot_challenge.bypass_probe",
        "bot_challenge.bypass_ip_allowlist",  # AS.0.6 §3 新增
        "bot_challenge.bypass_test_token",    # AS.0.6 §3 新增
    }
    actual = {
        getattr(bc, name) for name in dir(bc)
        if name.startswith("EVENT_BOT_CHALLENGE_BYPASS_")
    }
    assert actual == expected, f"AS.0.6 §3 drift: {actual ^ expected}"
```

### 8.3 IP allowlist storage shape guard

```python
def test_as_0_6_automation_ip_allowlist_storage_in_jsonb():
    """AS.0.6 §2.2 invariant: automation_ip_allowlist 必走 tenants.auth_features JSONB；
    禁拆獨立表（不可有 alembic 0059+ 名為 'tenant_ip_allowlist' 之類的 migration）."""
    import pathlib, re
    versions_dir = pathlib.Path("backend/alembic/versions")
    forbidden_table_pattern = re.compile(
        r"create_table\s*\(\s*['\"](tenant_ip_allowlist|automation_ip_allowlist|tenant_bypass_ips)['\"]"
    )
    for f in versions_dir.glob("*.py"):
        text = f.read_text()
        assert not forbidden_table_pattern.search(text), (
            f"{f.name}: AS.0.6 §2.2 禁拆獨立 IP allowlist 表 — "
            f"必走 tenants.auth_features JSONB column"
        )
```

### 8.4 Test-token min-length + production unset guard

```python
def test_as_0_6_test_token_lifespan_validation():
    """AS.0.6 §2.3 invariant: OMNISIGHT_TEST_TOKEN 若設必 ≥ 32 chars，backend 啟動時驗."""
    import os, importlib
    from backend.security import bot_challenge as bc
    # 模擬 < 32 chars env
    monkeypatch_env = os.environ.copy()
    os.environ["OMNISIGHT_TEST_TOKEN"] = "shortvalue"
    try:
        importlib.reload(bc)
        # 讀 token validation helper
        try:
            bc._validate_test_token_env()
            assert False, "應 raise ValueError"
        except ValueError as e:
            assert "must be" in str(e) and "32" in str(e)
    finally:
        os.environ.clear()
        os.environ.update(monkeypatch_env)
        importlib.reload(bc)


def test_as_0_6_production_deploy_does_not_set_test_token():
    """AS.0.6 §2.3 invariant: scripts/deploy.sh / docker compose prod profile 必不 set
    OMNISIGHT_TEST_TOKEN（避免 production tenant 出現 bypass_test_token row）."""
    import pathlib, re
    deploy_sh = pathlib.Path("scripts/deploy.sh").read_text() if pathlib.Path("scripts/deploy.sh").exists() else ""
    compose_prod = ""
    for f in pathlib.Path(".").glob("docker-compose*.yml"):
        if "prod" in f.name or "production" in f.name:
            compose_prod += f.read_text()
    pattern = re.compile(r"OMNISIGHT_TEST_TOKEN\s*[=:]")
    assert not pattern.search(deploy_sh), (
        "AS.0.6 §2.3: scripts/deploy.sh 不可 set OMNISIGHT_TEST_TOKEN"
    )
    assert not pattern.search(compose_prod), (
        "AS.0.6 §2.3: production docker-compose 不可 set OMNISIGHT_TEST_TOKEN"
    )
```

### 8.5 Wide CIDR refusal guard

```python
def test_as_0_6_wide_cidr_rejected_at_validation():
    """AS.0.6 §2.2 invariant: /0 / /8 IPv4 / /16 IPv6 等 too-broad CIDR 必 422."""
    from backend.security.bot_challenge import _validate_ip_allowlist_entries
    from fastapi import HTTPException
    too_broad = ["0.0.0.0/0", "10.0.0.0/8", "::/16", "2001::/16"]
    for entry in too_broad:
        try:
            _validate_ip_allowlist_entries([entry])
            assert False, f"應拒 {entry}"
        except HTTPException as e:
            assert e.status_code == 422
            assert "too broad" in e.detail.lower() or "wide" in e.detail.lower()
```

### 8.6 Test-token header 常數匯出（禁 inline）guard

```python
def test_as_0_6_test_token_header_constant():
    """AS.0.6 §2.3 invariant: X-OmniSight-Test-Token 必走常數匯出，禁 inline 字串."""
    import pathlib, re
    from backend.security.bot_challenge import _TEST_TOKEN_HEADER
    assert _TEST_TOKEN_HEADER == "X-OmniSight-Test-Token"
    inline = re.compile(r'["\']X-OmniSight-Test-Token["\']')
    src_files = ["backend/security/bot_challenge.py", "backend/auth_baseline.py", "backend/main.py"]
    for path in src_files:
        text = pathlib.Path(path).read_text() if pathlib.Path(path).exists() else ""
        for line in text.splitlines():
            if "_TEST_TOKEN_HEADER" in line and "=" in line:
                continue  # 常數定義行允許
            if 'X-OmniSight-Test-Token' in line:
                assert not inline.search(line), (
                    f"{path}: X-OmniSight-Test-Token 必走 _TEST_TOKEN_HEADER 常數 (line: {line.strip()})"
                )
```

---

## 9. 與 AS.0.x / AS.x 其他 row 的互動 / 邊界

| 互動對象 | 邊界 |
|---|---|
| **AS.0.1 §4.5** inventory | 本 row 是 inventory 的「mechanism axis 細化」；inventory 列「caller 種類」、本 row 釘「為何 bypass / 怎麼 bypass / audit 怎麼寫」 |
| **AS.0.2** `tenants.auth_features` JSONB | **強耦合**——automation_ip_allowlist 是 auth_features 的新 key，無新 alembic（複用 AS.0.2 既有 column） |
| **AS.0.3** `users.auth_methods` + account-linking | 無交集——本 row 是 caller-level bypass、AS.0.3 是 user-level merge policy |
| **AS.0.4** credential refactor migration plan | **弱耦合**——Track B `OMNISIGHT_DECISION_BEARER` cleanup 可降低 `apikey_legacy` bypass 占比；本 row 月報 `apikey_legacy_still_used` anomaly hint 是 Track B contract phase 的 advance signal |
| **AS.0.5** Turnstile fail-open phased strategy | **強耦合**——本 row 是 AS.0.5 §4 bypass list precedence 的 mechanism 細化；§3 / §4 / §8 三條條文與 AS.0.5 §3 / §4 / §8.1 對齊 |
| **AS.0.7** honeypot field 設計 | 弱耦合——三 mechanism 的 bypass 都跳過 honeypot；honeypot 細節（field name / aria-hidden）AS.0.7 釘 |
| **AS.0.8** `OMNISIGHT_AS_ENABLED` single-knob | **強耦合**——knob false 時三 mechanism 皆 noop（AS 全套 disabled）；本 row 的 caller 行為退到 pre-AS（不寫 audit、不檢查 bypass list） |
| **AS.0.9** compat regression test suite | **強耦合**——5 顆 critical 中 #3 `apikey_omni` bypass、#4 test-token bypass 是本 row 規範的 wire 介面 acceptance |
| **AS.0.10** auto-gen password core lib | 無交集 |
| **AS.1** OAuth client core | 弱耦合——OAuth login flow 本身不在三 mechanism bypass 範圍（OAuth 走 password+OAuth dual factor），但 OAuth callback path `/auth/oidc/` 在 AS.0.1 §4.5 #6（probe path）已 bypass |
| **AS.2** token vault | 無交集——token vault 是 user-level OAuth refresh，不影響 caller-level bypass |
| **AS.3.1** `bot_challenge.py` module | **本 row 是 AS.3.1 的 spec source（與 AS.0.5 並列）**——三 axis short-circuit 順序、helper 介面、event 常數、drift guard 必對齊 |
| **AS.3.4** server-side score verification | 無交集——bypass 在 verify 之前 short-circuit |
| **AS.3.5** provider fallback chain | 無交集——bypass 跳過整 verify 路徑，fallback 不觸發 |
| **AS.4** honeypot helper | 弱耦合——bypass kind 三 axis 都跳 honeypot；helper 對 bypass-flagged request 必 short-circuit return pass |
| **AS.5.1** auth event format | **強耦合**——本 row §3 的 7 個 bypass event + §6 monthly summary action 是 AS.5.1 的 canonical naming subset |
| **AS.5.2** per-tenant dashboard | **強耦合**——本 row §6 monthly cron + dashboard widget 是 AS.5.2 的範圍；report shape 釘在本 row |
| **AS.6.3** OmniSight self Turnstile backend wire | 強耦合——caller path 4 處（login / signup / pwreset / contact）必 wire `bot_challenge.verify()` 並讓本 row 三 axis 先 short-circuit |
| **AS.6.4** admin Settings UI（建議新 row） | **本 row §5 的 落地 owner**——IP allowlist GET/PATCH endpoint + frontend page + audit chain |
| **AS.7.x** UI redesign | 弱耦合——Settings page 樣式由 AS.7 統一管 |
| **AS.8.3** ops runbook | 強耦合——三 mechanism 的「如何加 IP / 如何 rotate test-token / 如何看月報」runbook 從本 row 衍生 |

---

## 10. 非目標 / 刻意不做的事

1. **不引入 per-api_key 的 IP allowlist** — 既有 `api_keys` 表沒有 `allowed_ips` column，引入會：(a) 撞 K6 既有 schema、需 alembic 0059+ 新 migration；(b) admin UX 需在每 key 各自管 IP，重複度高；(c) 與本 row §2.2 per-tenant IP allowlist 功能重疊，違反 SoC。tenant-level 一處集中管 IP 已足夠；個別 key 走 scope 限制（K6 已支援）。
2. **不在 Mechanism C 走 DB-based test-token** — env-only。理由：(a) DB-based 會引入「test-token 的 RBAC / rotation / audit chain」=  reinvent K6 api_keys；(b) test-token lifecycle 應與 deployment 一起（CI runner secret manager 注入 env），不適合 admin UI 管理；(c) DB-based 增加「token leak 後 revoke 不及時」風險，env-based 改 env+restart instant。
3. **不支援「temp bypass」/「self-service bypass」** — 不開放 admin user 為自己的 session 暫時 bypass Turnstile。任何 bypass 都需要 explicit credential（api_key 或 source IP 或 env-injected token）。理由：self-service bypass 等於 admin 可隨時關自己的 captcha，違反 Phase 3 fail-closed 設計初衷；admin 想 disable 整 tenant 走 `auth_features.turnstile_required=false`（per AS.0.5 §2.4 per-tenant rollback）。
4. **不引入「known-bot user-agent」自動 bypass** — 不基於 User-Agent 字串判斷自動化 client（e.g. `curl/*`、`Renovate/*`）。理由：UA 可偽造、無 cryptographic identity proof、會誘使 bot 偽裝合法 UA；本 row 三 axis 都需要「實質 secret 或 routing 證明」。
5. **不規範跨 tenant 共用 IP allowlist** — 每 tenant 各自管自己。理由：tenant 邊界是 OmniSight 多租戶安全模型基石；CI runner 若服務多 tenant 必走 multi-tenant api_key（per K6 scope 機制）而非 IP 共用。
6. **不規範 IPv6 NAT 場景** — 本 row 假設 caller 來自有穩定 IPv4 / IPv6 prefix 的 source；不為「動態 NAT64 / Tailscale / Cloudflare WARP」這類動態路由場景額外處理。AS.0.5 §10 #6 大陸區可達性是 user-side 問題、本 row 是 automation-side 問題、tenant admin 自選 fixed-IP routing 是 deploy 條件。
7. **不規範 bypass 的「opt-out per endpoint」** — bypass 是全 endpoint applies。不引入「`/api/v1/auth/login` bypass 但 `/api/v1/auth/change-password` 不 bypass」這類 per-endpoint 矩陣。理由：bypass 鑑別維度是「caller 身分」，不是「endpoint 敏感度」；endpoint-level 嚴格性走 K6 scope（per-key endpoint allowlist）+ RBAC（require_admin / require_super_admin）兩層既有機制。
8. **不規範 CIDR 變更的「N 天 grace period」** — 即時生效。理由：grace period 等於「allowlist 已移除 entry 但實際還能用」，違反「audit log 反映當下 state」原則；admin 移除 entry 即斷該 IP 自動化（catch 反向：誤刪 = 立即修復、不誤以為已生效）。

---

## 11. Module-global state audit (per docs/sop/implement_phase_step.md Step 1)

1. **本 row 全 doc / 零 code 變動** — 不引入任何 module-level singleton / cache / global；plan §3 釘的「2 個新 EVENT_* 常數」是規範未來 AS.3.1 module 必須匯出的常數，當下 `backend/audit_events.py` 既有 `EVENT_*` 常數模式（每 worker 從同 source file 推同 immutable string、Answer #1）保持不動。
2. **未引入新 module-level global / singleton / cache**。
3. **未來 AS.3.1 / AS.6.4 module-global state 預先標註**：
   - `_BYPASS_PATH_PREFIXES` / `_BYPASS_CALLER_KINDS` / `_TEST_TOKEN_HEADER` 是 module-level immutable set/str（Answer #1，每 worker 同推）。
   - `_TEST_TOKEN_VALUE_CACHE` 不應存在——`OMNISIGHT_TEST_TOKEN` 應每次 request 從 `os.environ.get()` 讀（與 `auth_baseline_mode()` 模式一致），避免 test 之間 monkeypatch 失效；trade-off 是每 request 一次 env read，cost 可忽略。
   - `_TENANT_FEATURES_CACHE`（per-tenant `auth_features` cache）走 Y9 既有 60s TTL（Answer #2，per-PG-coordinated cache invalidation）；本 row 的 IP allowlist read 必同走此 cache，禁額外 in-memory dict（避免 stale window 不一致）。
   - 任何「per-worker rolling counter for `bypass_test_token` in production tenant alert」（若 AS.3.1 加）走 per-replica bucket（Answer #3，與 `_login_throttle` 同 pattern），cron 月結報表才走 PG 聚合（不依賴 in-memory 跨 worker 一致）。
   - AS.3.1 / AS.6.4 PR 落地時 Step 1 必再次驗證。

---

## 12. Read-after-write timing audit

- **本 row 改動**：純 doc 落地，無 schema / 無 caller / 無 transaction 行為變化；不適用 timing 分析。
- **plan 文件本身對未來 PR 的 timing 約束**：
  - **Mechanism B IP allowlist PATCH → 下一 request 看到新 list**：admin PATCH `tenants.auth_features.automation_ip_allowlist` → audit row 寫 → JSONB UPDATE → Y9 60s cache invalidate；下一 request 走 cache miss 重 query DB 看到新值（per-worker），最壞 60s eventual consistency window。本 row 容忍此 window，理由：(a) IP allowlist 是 「opt-in expansion」非「safety control」，60s 內舊 entry 仍可 bypass、不致 user 被 lock；(b) 60s window 與 Y9 既有 cache 行為一致、不引入新 timing 異常。
  - **Mechanism C test-token env 變更 → restart-only**：env-based、無 cache、改 env 必 restart backend (`docker compose restart backend-a backend-b`)，重新讀 env；不存在 read-after-write 問題（restart = full state reset）。
  - **Mechanism A api_key revoke → 下一 request 立即 401**：`validate_bearer()` 每次走 `SELECT ... WHERE enabled=1`、無 cache（per K6 既有設計），revoke 後立即生效；本 row 不改 K6 timing。
  - **`audit.log()` for `bypass_*` 必走 same-transaction emit**：bypass 判定 → audit row 寫 → handler return 前 commit，與 AS.0.5 §3 emit 順序一致（先 verify-or-bypass、後 audit、再 handler logic）；caller 不會看到「audit 已寫但 bypass 結果未生效」的中間態。

---

## 13. Pre-commit fingerprint grep（per SOP Step 3）

- 對 `docs/security/as_0_6_automation_bypass_list.md`：`_conn()` / `await conn.commit()` / `datetime('now')` / `VALUES (?,...)` 全 0 命中（doc 本身只有規範描述 + Python test pattern 範例，無可執行 SQL/code 殘留；Python pattern 為 documentation only、不會被 import 執行）。
- 對 `TODO.md` 改動 hunk：1 行單 row 狀態翻 `[ ]` → `[x]` + reference 條目，無 fingerprint。
- 對 `HANDOFF.md` 改動 hunk：plan-only entry header + 範圍 + contract + 設計決策，無 fingerprint。
- Runtime smoke：本 row 不適用 — 純 plan doc，無 code path 可 smoke。drift guard tests（§8.1 - §8.6）在 AS.3.1 / AS.6.4 PR 落地、屆時各自有自己的 smoke。

---

## 14. Production status

* 文件本身：plan-only design freeze。
* 影響的程式碼：本 row 不改 code；AS.3.1 / AS.5.2 / AS.6.3 / AS.6.4 follow-up rows 才動。
* Rollback 影響：plan 無 runtime impact、無 rollback。

**Production status: dev-only**
**Next gate**: 不適用 — 本 row 是 design doc。Schedule 由 AS.3.1 (`backend/security/bot_challenge.py`) PR 觸發三 mechanism 中 A + C 的 short-circuit；AS.6.4（建議新 row）admin Settings UI + IP allowlist endpoint PR 觸發 mechanism B 落地；AS.5.2 dashboard PR 觸發月結報表 cron + email。

---

## 15. Cross-references

- **AS.0.1 inventory**：`docs/security/as_0_1_auth_surface_inventory.md` §4.5 (10-item bypass list — 本 row §2 對齊 source) + §6 (test-token 「現況沒實作」comment 是本 row §2.3 預期實作的對照基線)。
- **AS.0.2 alembic 0056**：`backend/alembic/versions/0056_tenants_auth_features.py` — `auth_features` JSONB column 是 §2.2 `automation_ip_allowlist` storage 的 schema source；本 row 不需新 alembic。
- **AS.0.4 credential refactor**：`docs/security/as_0_4_credential_refactor_migration_plan.md` Track B — `OMNISIGHT_DECISION_BEARER` cleanup 是月報 `apikey_legacy_still_used` anomaly 的 SoT；本 row 月報該 hint 是 Track B contract phase 的 advance signal。
- **AS.0.5 Turnstile fail-open**：`docs/security/as_0_5_turnstile_fail_open_phased_strategy.md` §3 / §4 / §8.1 — 本 row §3 / §4 / §8.1 對齊 source，event 常數匯出 / precedence / drift guard 三條 invariant 與 AS.0.5 同一 set。
- **設計 doc § 3.6 / § 3.7 / § 3.8 / § 10 R34**：`docs/design/as-auth-security-shared-library.md` — automation bypass list / single-knob rollback / compat regression test / R34 risk register 四處原始 design source；本 row §1.1 / §2.3 / §7.4 對齊。
- **既有 K6 api_keys**：`backend/api_keys.py` + `backend/alembic/versions/0011_api_keys.py` — Mechanism A 的 SoT（本 row 不改 K6，僅在 AS.3.1 short-circuit 時 leverage `request.state.api_key`）。
- **既有 `auth_baseline._has_valid_bearer_token`**：`backend/auth_baseline.py:271-331` — Mechanism A 的 baseline floor 機制；AS.3.1 short-circuit 必 reuse 已 cache 的 `request.state.api_key` 避免 double-validate。
- **既有 `_subnet_prefix`**：`backend/auth.py:280-319` — Mechanism B audit row metadata `client_ip_subnet` 的 helper source；本 row § 3 / §6.4 直接複用。
- **G4 production-readiness gate**：`docs/sop/implement_phase_step.md` lines 136-216；任何 mechanism B Settings UI 落地 PR 必過此 gate（image rebuild + env knob wired + at least one live smoke + 24h observation）。

---

**End of AS.0.6 plan**. 下一步 → AS.0.7 honeypot field 設計細節（rare field name + CSS hide + tabindex=-1 + autocomplete=off + aria-hidden）— 把本 row 規範的「三 mechanism bypass 行為」延伸到「honeypot field 在 bypass 路徑跳過 / 在非 bypass 路徑強制」的 form-DOM-level invariant。
