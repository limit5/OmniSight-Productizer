# AS.0.4 — Credential Refactor Migration Plan

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Scope**: 規範 AS.2 `oauth_tokens` 落地、legacy `OMNISIGHT_DECISION_BEARER` 三 router 內建檢查清理、與未來 git/llm/codesign credential 集成至 token-vault 的「expand-migrate-contract」遷移流程；同步釘住 encryption-key 連續性策略以避免引入第二個 master key。
>
> **目標讀者**：(1) 寫 AS.2.1 `backend/auth/token_vault.py` + AS.2.2 alembic 0057 `oauth_tokens` 的人——本文件規範 schema shape + key-version column reservation。(2) 寫 AS.0.6 automation bypass list 的人——需要知道 legacy bearer 的雙寫 / 雙讀 timeline。(3) 未來 helper-level vault unification 設計者——本文件鎖死「contract phase 不可在同 release 引入 key rotation」這條 invariant。
>
> **不在本 row 範圍**：實際 alembic migration（AS.2.2）、token vault 程式碼（AS.2.1）、KMS / Vault provider 整合（roadmap 之外）、per-tenant Fernet key 隔離（AS-后續，刻意脫鉤）。

---

## 1. 為什麼需要 expand-migrate-contract（不是 in-place rename）

`AS.0.1` 盤點顯示 production 已有三個 credential 儲存子系統 + 一個雙重 bearer 認證 (`api_keys` 與三個 router 內 inline `OMNISIGHT_DECISION_BEARER` env check 平行存在)：

- `git_accounts` (alembic 0027 / Phase 5-2~5-4) — 4 column (`encrypted_token / encrypted_ssh_key / encrypted_webhook_secret`) Fernet 密文 + audit 鏈 + 13 個 router endpoint + UI in `integration-settings.tsx`。
- `llm_credentials` (alembic 0029 / Phase 5b-1~5b-3) — `encrypted_value` + 9 provider + UI in `provider-card-expansion.tsx` + live `/test` probe。
- `codesign_store` (P3 #288) — file-backed JSON `data/codesign_store.json` + Fernet + HSM-optional + audit hash chain。
- `api_keys` (K6 #) — bearer `omni_*` + `migrate_legacy_bearer()` lifespan auto-migrate 但 router 層三處 `os.environ.get("OMNISIGHT_DECISION_BEARER")` inline check 與 K6 平行 (`decisions.py:64` / `audit.py:38` / `profile.py:29`)，operator 看不到「token 已在 DB」。

四個子系統共用 `backend.secret_store._fernet` 一把 master key。任何 in-place rewrite（例如「把 git_accounts.encrypted_token 拆出來丟進新 oauth_tokens」）會：

- 撞到 audit chain（codesign 已有 hash chain rebuild 成本）。
- 撞到 4 個 router endpoint 的 backward-compat。
- 撞到 UI 既有 fingerprint display 契約。
- 在 alembic single-cycle 內既要 rename 又要 re-encrypt → 任一步失敗都需手動回滾、無自動 rollback path。

**Expand-migrate-contract** 把 risk 拆三段、每段獨立可 ship、可 rollback：

| 階段 | 會發生的事 | 可 rollback 嗎？ |
|---|---|---|
| **Expand** | 新 schema / 新 helper / 新 endpoint 上線、舊 schema 完全不動、callers 仍走舊路徑 | 是——alembic downgrade 砍新 column / drop 新 table；零 data 損失 |
| **Migrate** | Caller 雙寫（write-through 新+舊）、雙讀（先新後舊 fallback）、運行**至少一個 release cycle (≥ 14 天)** | 是——切回舊 caller，新表變孤兒（後 release 統一收）|
| **Contract** | 在 expand+migrate 都觀察 ≥ 14 天無 regression 後，同 PR 移除舊路徑 + drop 舊 table；alembic migration 編號鎖在這次 contract 才用 | **不可**——舊 schema 已 drop；contract PR 必須過 G4 production-readiness gate |

舊表保留**整整一個 release cycle**而不是 hotfix 後立即砍，是因為：

1. **Production data export / DR backup** 的 schema snapshot 在 release branch cut 那一刻凍結；提早砍會讓 backup → restore 路徑要 schema upgrade。
2. **Operator 手動 rollback** (`git revert` migrate-phase commit) 需要舊 schema 在 DB 上仍然存在；contract 提前會讓 revert 變成「revert + 重新 migrate forward」雙步驟。
3. **觀察窗** (≥ 14 天) 是抓「上線後第二週才出現」regression 的歷史經驗——例：低頻 webhook signature path、月結 billing 週期、跨 release 的 cron job。

---

## 2. 三條 refactor track（本 row 涵蓋的具體 expand-migrate-contract 應用）

本 row 不直接改 code；以下是「未來 PR 必須遵守的 sequencing」，AS.2 / AS.6 / 後續 vault-unification phase 只能在這個 schedule 下落地。

### Track A — AS.2 `oauth_tokens` table（純 expand，無對應舊表）

**狀態**：純 additive。沒有舊表要 migrate，因為 AS 之前的 OAuth flow 是 ad-hoc per-provider 寫法，根本沒有「OAuth token 集中儲存」的舊路徑——既有 `/api/v1/auth/oidc/{provider}` 拿 IdP token 後**不持久化**，立即 exchange → session cookie → 丟棄。

**Expand 步驟**：

| Step | Owner row | 動作 |
|---|---|---|
| A.1 | AS.2.2 | alembic `0057_oauth_tokens.py` 新表（不動既有任何表）|
| A.2 | AS.2.1 | `backend/auth/token_vault.py` helper module（read/write/refresh/revoke API）|
| A.3 | AS.0.4 (本 row) | 在 `scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER` 與 `TABLES_WITH_IDENTITY_ID` 預留 `oauth_tokens` 條目；`backend/db.py::_SCHEMA` SQLite parity；`backend/tests/test_migrator_schema_coverage.py` drift guard 自動 catch（AS.2.2 PR 必須過此 gate）|
| A.4 | AS.1 | OAuth client core 把拿到的 access/refresh token write into `oauth_tokens` via vault API |

**Migrate / Contract 不適用**——沒有舊路徑。AS.2.2 PR 直接是 expand-only。

**Schema shape**（design doc §AS.2.2 已預留，本 row 釘住）：

```sql
CREATE TABLE oauth_tokens (
  id TEXT PRIMARY KEY,                        -- ot-<uuid7>
  user_id TEXT NOT NULL REFERENCES users(id),
  provider TEXT NOT NULL,                     -- google / github / apple / microsoft
  access_token_enc TEXT NOT NULL,             -- Fernet ciphertext
  refresh_token_enc TEXT,                     -- nullable: providers that don't issue refresh
  expires_at TIMESTAMPTZ NOT NULL,
  scope TEXT NOT NULL DEFAULT '',             -- space-separated scopes
  key_version INTEGER NOT NULL DEFAULT 1,     -- §3 encryption-key continuity
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  version INTEGER NOT NULL DEFAULT 0,         -- optimistic lock (G4 pattern)
  UNIQUE (user_id, provider)                  -- one binding per user-per-provider
);
CREATE INDEX idx_oauth_tokens_expires_at ON oauth_tokens(expires_at);
```

**OAuth provider whitelist 與 AS.0.3 對齊**：`provider` column 接受值嚴格 = `account_linking._AS1_OAUTH_PROVIDERS = {google, github, apple, microsoft}`。新增 provider 必須同步改 AS.0.3 helper + AS.1 vendor catalog + AS.0.4 本文件三處 — 這是 design doc §3.3 已明文鎖的 invariant。

### Track B — Legacy `OMNISIGHT_DECISION_BEARER` 三 router inline check 清理（完整 E-M-C cycle）

**問題**（AS.0.1 §6.2）：
- `backend/api_keys.py::migrate_legacy_bearer()` lifespan 已自動把 env-based bearer migrate 進 `api_keys` row `ak-legacy-<sha[:12]>`，DB 層完成。
- 但 `decisions.py:64` / `audit.py:38` / `profile.py:29` 三 router 仍 `os.environ.get("OMNISIGHT_DECISION_BEARER")` inline check，跟 K6 api_keys 平行。
- 結果：operator 從 admin UI 看到 `legacy-bearer` row 已存在、以為移除 env var 不影響→ 移除後三 router 因為 `expected = ""` 走 `if not expected: return` early-return path → 三條 endpoint **變成 unauthenticated open**（pre-fix 行為），不是「走新 api_keys 認證」。Bug 不會 raise alarm，因為 3 個 endpoint 仍走 `require_operator` 等 RBAC、只是 token gate 消失。

**完整 E-M-C 流程**：

| Phase | 動作 | Owner row | 預估 |
|---|---|---|---|
| **Expand** | 三 router 改用 `Depends(_au.require_api_key)` (K6 helper、scope `["*"]` 或 `["decisions.write"]`)、保留**舊 inline `OMNISIGHT_DECISION_BEARER` check 作為 fallback** | AS.6 / 或獨立 hot-fix row | 0.5 day |
| **Migrate** | release N 上線；audit_log 同時記新 `api_key_id` 與舊 env-token use（區分 token vs api_key 來源）；若一個 release cycle 內 audit 看到 zero env-token use → safe to contract | AS.6 follow-up | observation |
| **Contract** | release N+1：移除三 router inline `OMNISIGHT_DECISION_BEARER` check + 移除 `backend.api_keys.migrate_legacy_bearer()` lifespan call + alembic 不需動（`api_keys` row 留著、env var 移除即無法再 migrate）；env var deprecation log | AS.6 | 0.25 day |

**Contract 的觸發條件 (gate)**：
- audit_log 連續 14 天 zero env-token-only authentication（meaning: token presented matches `OMNISIGHT_DECISION_BEARER` env BUT does **NOT** match any `api_keys.key_hash`），跨所有 production replicas。
- Operator dashboard 顯式 ack 「env var removed」（avoid silent contract）。
- 在 AS.0.8 single-knob (`OMNISIGHT_AS_ENABLED=false`) 下三 router 走 fallback path（contract phase 之前）— 與新 AS gating 解耦。

### Track C — Helper-level vault unification（roadmap 規劃，AS.0 不執行）

`AS.0.1` §3.4 列出 `git_accounts` / `llm_credentials` / `codesign_store` 七大重複模式（Fernet master key / tenant scoping / `is_default` / audit / fingerprinting / LRU touch / API masking）。長線希望走 token-vault 統一。**但本 row 刻意不排這條 timeline**，因為：

- AS.2 token_vault 第一版 scope 是 OAuth tokens（design doc §AS.2 明文）。
- git/llm/codesign 各自有完整的 router CRUD + UI + audit chain，重寫 cost 遠大於 AS.2 OAuth scope。
- 三表的 schema shape (`encrypted_*` 欄位數 / `is_default` 唯一性 / metadata JSONB) 與 `oauth_tokens` 並非 1:1，硬合會引入 super-table anti-pattern。

**規劃約定（給未來 phase）**：

1. 先在 `token_vault` 上線 ≥ 90 天觀察；確認 OAuth-only 場景下 helper API 設計穩定再考慮 unify。
2. 若決定 unify，**每個既有表獨立走完一輪 E-M-C**：
   - Expand: token_vault 加 `git_credential` / `llm_credential` / `codesign_record` record_type 區分。
   - Migrate: dual-write、old-resolver 仍是 source of truth 一個 release cycle。
   - Contract: drop old table + 移除 legacy resolver + audit_log 記載 cutover。
3. **嚴禁三表同時 contract**——一次 contract 一個子系統，避免 blast radius 重疊。

---

## 3. Encryption-key 連續性策略（hard invariant — 跨 phase 不可破）

### 3.1 策略條文

1. **單一 master Fernet key**：AS.2.2 oauth_tokens 的 `access_token_enc` / `refresh_token_enc` **必須**走 `backend.secret_store._fernet`、與 git_accounts / llm_credentials / codesign_store 共用。**禁止**為 AS.2 引入第二個 master key、第二個 `OMNISIGHT_*_SECRET_KEY` env var、或 per-row salt-derived sub-key。
2. **`key_version` column 是預留欄位、不是當下功能**：`oauth_tokens.key_version DEFAULT 1` 是給未來 KMS migration 用的 hook；當下所有 ciphertext 一律 `key_version=1`、`token_vault.decrypt(row)` 不做 key lookup（直接走 `secret_store._fernet`）。
3. **同一 release 不可同時做 schema migration + key rotation**：違反此條款的 PR 必須拆兩個 release。理由：alembic migration 失敗回滾 + key rotation 中斷 = 雙重故障，恢復路徑無法 deterministic。
4. **首次 KMS / key rotation 落地時的程序**（規劃，roadmap 之外）：
   - Phase 1 (expand): `secret_store` 加 `decrypt_with_version(ciphertext, key_version)` 雙路徑、新 ciphertext 仍走 v1。
   - Phase 2 (migrate): cron job 把 v1 ciphertext rotate 到 v2、`UPDATE ... SET key_version=2`。Read 走 v2-or-v1 fallback。
   - Phase 3 (contract): 觀察 ≥ 14 天 zero v1 read → 移除 v1 fallback + 砍 v1 key 持有。
5. **codesign_store 例外 — 不在 Fernet key rotation 範圍**：codesign 自己有 HSM-optional 路徑 (`hsm_vendor` ∈ `aws_kms / gcp_kms / yubihsm`)，與 application-layer Fernet 解耦。Vault unification 若涉及 codesign 必須**保留 codesign 自己的 key path**、`token_vault.decrypt` 對 record_type=codesign 走 `codesign_store.decrypt_material()` 而非 secret_store。

### 3.2 為什麼禁第二個 master key

- 既有 codepath grep `secret_store.encrypt`/`decrypt` 命中 ~30 處，分散於 `git_credentials.py / llm_credentials.py / codesign_store.py / chatops/*.py`；引入第二把 key 等於把 `_get_fernet()` 變成 `_get_fernet(scope)` 並 N 處改 callsite，**而 N 處 callsite 改動風險 > 第二把 key 帶來的隔離效益**。
- AS.0.1 §3.4 的 ”cross-subsystem patterns to consolidate” 表達明確：master key 是要往 KMS-backed vault 走，**不是**往 N 個 application-layer Fernet 走。第二把 key 是反方向。
- 第二把 key 的 backup / restore / on-call rotate 程序需要與第一把同步、運維 cost double；目前 ops runbook (`docs/ops/db_failover.md`) 只 cover 一把 key 的 rotation，加第二把要重寫 runbook。

### 3.3 與 AS.0.3 `users.auth_methods` 的互動

AS.0.3 不持有 ciphertext（`auth_methods` JSONB column 是明文 method tag list）。oauth_tokens row 與 users.auth_methods row 的 `oauth_<provider>` tag 是 1:1 鏡像，但**沒有 DB-level FK**（避免 cascade-delete 拖累 users 表 reaper）。一致性保證走 application 層：

- `link_oauth_after_verification(...)` 串 `add_auth_method` + `token_vault.write(...)`，同一 caller's transaction，atomic commit。
- `remove_auth_method(user_id, "oauth_google")` 必須**先**呼 `token_vault.revoke(user_id, "google")` 再 remove method tag，順序反過來會留 orphan ciphertext。AS.7 unlink button + AS.2.5 revoke endpoint 必須合在同一個 router handler 串、不可拆兩個 endpoint 給 UI 順序呼叫。

---

## 4. Rollback 策略

### 4.1 各 phase 的回滾路徑

| Phase | 回滾觸發 | 回滾動作 | 資料損失？ |
|---|---|---|---|
| Expand | 上線後即 critical bug | `alembic downgrade -1`（drop 新 column / table） | 無——新 schema 在新 caller 寫之前 rollback |
| Migrate | dual-write 出錯 | feature flag `OMNISIGHT_AS_ENABLED=false` 或 caller-level `oauth_tokens_dual_write=false` env；舊 schema 仍 source of truth | 無——dual-write loser 是新表 |
| Contract | 已 drop 舊表後發現 regression | **不能 rollback**——必須 forward fix；contract PR 過 G4 gate 是 hard requirement | 取決於 forward-fix 速度 |

### 4.2 與 AS.0.8 single-knob (`OMNISIGHT_AS_ENABLED`) 的互動

`OMNISIGHT_AS_ENABLED=false` **不**等於 alembic downgrade：

- env knob 控制 **runtime caller 走哪條 path**（new vs old），不動 schema。
- alembic schema state 永遠 forward-only（ALTER ADD COLUMN IF NOT EXISTS），運行時切 false 無 schema 副作用。
- 例：AS.2 上線後 expand phase，env knob false → OAuth login 路徑回到 `/auth/oidc/{provider}` ad-hoc 流程（不寫 oauth_tokens、不寫 auth_methods 的 oauth tag）；oauth_tokens 表 schema 存在但無新 row。

### 4.3 Migration phase 的 dual-write 失敗模式

dual-write 任一寫入失敗的處理（per design doc §G4 patterns）：

| 失敗組合 | 處理 |
|---|---|
| 舊寫成功 + 新寫失敗 | 紀錄 `as_dual_write_partial_failure` audit event，500 給 caller、不 rollback 舊寫；catch-up cron 補新表 |
| 舊寫失敗 | 直接 propagate 500、新表不寫；caller retry 與 pre-AS 行為一致 |
| 新寫成功 + 舊寫失敗（理論上不該發生因為先寫舊） | sentry alert；contract phase 之前舊表是 SoT，必須以舊寫為主 |

**dual-read fallback 順序**：read 永遠先試新表，miss → fallback 舊表，命中後**異步**寫入新表（catch-up）；若同 user 同 provider 新舊兩邊都有 row 但內容不同→ 以新表為準、舊表 row 寫入 `as_dual_read_divergence` audit。

---

## 5. Drift guards（必須在 AS.2.2 / AS.6 contract PR 之前 land）

### 5.1 Schema parity guard（已有）

`backend/tests/test_migrator_schema_coverage.py` 已是 G4 留下的範本——live alembic schema vs migrator `TABLES_IN_ORDER` vs db.py `_SCHEMA` 三方對齊。**AS.2.2 落地 PR 必須**同步：

- `scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER` 加 `oauth_tokens`
- `scripts/migrate_sqlite_to_pg.py::TABLES_WITH_IDENTITY_ID`：`oauth_tokens.id` 是 `TEXT` 不是 `INTEGER` → **不**加（與 ot-uuid7 形式對齊）
- `backend/db.py::_SCHEMA` 加 SQLite parity CREATE TABLE
- `backend/db.py::_migrate` 加 PRAGMA-guarded `ALTER TABLE` for SQLite-on-disk upgrade

### 5.2 OAuth provider whitelist 跨檔對齊 guard（新增）

四處 hardcoded 必須同步（drift guard test 在 AS.2.1 PR 內 land）：

1. `backend/account_linking._AS1_OAUTH_PROVIDERS` (AS.0.3)
2. `backend/auth/token_vault.SUPPORTED_PROVIDERS` (AS.2.1, future)
3. AS.1 vendor catalog (`backend/auth/oauth_client.py`, future) — 11-vendor list 是 superset，但其中 4 個必須與上兩處 strictly equal
4. 本文件 §2 Track A schema 的 `provider` column comment

Test pattern：
```python
def test_as_oauth_provider_whitelist_aligned():
    from backend.account_linking import _AS1_OAUTH_PROVIDERS
    from backend.auth.token_vault import SUPPORTED_PROVIDERS
    assert _AS1_OAUTH_PROVIDERS == SUPPORTED_PROVIDERS
```

### 5.3 `OMNISIGHT_DECISION_BEARER` 三 router contract guard（新增）

Contract phase 移除三 router inline check 時，必須同 PR 加 grep-based test：

```python
def test_decision_bearer_inline_checks_removed():
    """AS.0.4 contract phase: 三 router 不可再 inline read OMNISIGHT_DECISION_BEARER env."""
    for path in [
        "backend/routers/decisions.py",
        "backend/routers/audit.py",
        "backend/routers/profile.py",
    ]:
        text = pathlib.Path(path).read_text()
        assert "OMNISIGHT_DECISION_BEARER" not in text, (
            f"{path} still references legacy bearer env var; "
            f"remove inline check in favor of K6 api_keys path"
        )
```

此 test 在 expand phase 不存在（會 fail），migrate phase 仍不存在（會 fail），contract phase 落地同 PR 加進來——這也是 contract phase 的「definition of done」。

### 5.4 Encryption key non-divergence guard（新增）

防 future contributor 在 token_vault 引入第二把 Fernet key：

```python
def test_oauth_tokens_uses_secret_store_fernet():
    """AS.0.4 §3 invariant: oauth_tokens encryption 必走 backend.secret_store._fernet."""
    import inspect
    from backend.auth import token_vault
    src = inspect.getsource(token_vault)
    # 必須 import secret_store
    assert "from backend import secret_store" in src or \
           "from backend.secret_store" in src
    # 不可自己生新 Fernet key
    assert "Fernet.generate_key" not in src
    assert "OMNISIGHT_OAUTH_SECRET_KEY" not in src
```

---

## 6. Acceptance criteria per phase

### 6.1 Expand phase 完成條件

- [ ] alembic migration 過 PG dialect upgrade + downgrade roundtrip
- [ ] `test_migrator_schema_coverage.py` 仍綠（drift guard catch 任何 missing TABLES_IN_ORDER 條目）
- [ ] `backend/db.py` SQLite parity 同 PR landing
- [ ] 新 helper module 純 expand-only、不改任何既有 caller
- [ ] HANDOFF.md `Production status: dev-only` + `Next gate: deployed-inactive`

### 6.2 Migrate phase 完成條件

- [ ] dual-write code 被 `OMNISIGHT_AS_ENABLED` env 包圍 / 預設 false（既有 user 預設零行為改變、與 AS.0.2 `auth_features` 全 false default 對齊）
- [ ] catch-up cron job + audit event format land
- [ ] 至少 1 個 tenant 切 `auth_features.oauth_login=true` smoke 過
- [ ] HANDOFF.md `Production status: deployed-inactive` → `deployed-active` once tenant 切過
- [ ] 觀察窗 ≥ 14 天 zero `as_dual_write_partial_failure` event

### 6.3 Contract phase 完成條件

- [ ] 上述觀察窗滿足 + operator 顯式 ack
- [ ] `OMNISIGHT_AS_ENABLED` 預設改 true（既有 tenant 仍走 `auth_features` per-tenant gate，與 hard cutover 解耦）
- [ ] §5.3 grep guard test 加入 + 三 router inline check 移除
- [ ] alembic migration drop 舊表（若該子系統有舊表要 drop）+ migrator TABLES_IN_ORDER 移除條目 + db.py SQLite parity 同 PR
- [ ] HANDOFF.md `Production status: deployed-observed` + 24h 觀察窗 metrics clean
- [ ] **Contract PR 必過 G4 production-readiness gate**：production image rebuild + env knob wired + at least one live smoke 綠 + 24h observation 開始

---

## 7. 非目標 / 刻意不做的事

1. **Per-tenant Fernet key 隔離**——AS roadmap 之外（design doc §10 R31 衍生 risk register 條目）。本文件假設 single-tenant key model 直到 AS-后續 KMS migration phase。
2. **codesign_store schema rewrite**——P3 #288 file-backed JSON 留原狀；將 codesign 強行塞進 token_vault 是 super-table anti-pattern。
3. **同步重寫既有三大 cred 子系統**（git/llm/codesign）— Track C 規劃中、不在 AS.0 / AS.2 第一波 ship。
4. **`OMNISIGHT_AUTH_MODE=open` semantics 改動**——dev/test bypass 的 default behaviour 不變（`backend/auth.py:70`），AS.0.8 single-knob 與此正交。
5. **Webhook signature secret 收編進 vault**——`gerrit_webhook_secret` / `github_webhook_secret` 等仍在 Settings 表 + `git_accounts.encrypted_webhook_secret`，第三方 webhook 認證不在 OAuth scope。
6. **api_keys 表的 schema 改動**——K6 `omni_*` bearer 與 oauth_tokens 平行存在，目的不同（machine-to-machine vs human OAuth），不 unify。

---

## 8. Production status

* 文件本身：plan-only，code-merge `[x]` 等同 design freeze。
* 影響的程式碼：本 row 不改 code；AS.2.x / AS.6 follow-up rows 才動。
* Rollback 影響：plan 無 runtime impact、無 rollback。

**Production status: dev-only**
**Next gate**: 不適用 — 本 row 是 design doc。Schedule 由 AS.2.2 (alembic 0057) + AS.2.1 (token_vault module) PR 觸發 expand phase。

---

## 9. Cross-references

- **AS.0.1 inventory**：`docs/security/as_0_1_auth_surface_inventory.md` §3 (credential storage subsystems) + §4 (automation bypass) + §6.2 (三 router inline `OMNISIGHT_DECISION_BEARER` gap) + §6.4 (single Fernet key risk)。
- **AS.0.2 alembic 0056**：`backend/alembic/versions/0056_tenants_auth_features.py` — `auth_features.oauth_login` 是 Track A migrate phase 的 per-tenant gate。
- **AS.0.3 account-linking**：`docs/security/as_0_3_account_linking.md` §3 — `account_linking._AS1_OAUTH_PROVIDERS` 是 §5.2 OAuth provider whitelist 的 source of truth；本文件 §2 Track A schema 鏡像對齊。
- **設計 doc § AS.2 / § AS.6 / § R31**：design doc OAuth + token vault + R31 OAuth account takeover 條目。
- **G4 production-readiness gate**：`docs/sop/implement_phase_step.md` lines 136-216；contract phase PR 必過此 gate。
- **migrator schema coverage**：`backend/tests/test_migrator_schema_coverage.py` — drift guard 範本。

---

**End of AS.0.4 plan**. 下一步 → AS.0.5 Turnstile fail-open 漸進策略 (4 週 fail-open + warning log → alert → tenant opt-in fail-closed)。
