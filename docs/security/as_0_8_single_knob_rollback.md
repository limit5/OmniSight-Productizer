# AS.0.8 — Single-knob Rollback (`OMNISIGHT_AS_ENABLED`)

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Scope**: 釘住 `OMNISIGHT_AS_ENABLED` env knob 的精確語義——env wiring location（`backend/config.py` Settings field）+ default value + valid value set + boot-time lifespan validate + 「false 時 AS 全套 disabled」對 AS.1 / AS.2 / AS.3 / AS.4 / AS.5 / AS.6 / AS.7 七個子系統的逐一 noop 行為矩陣 + 與 `OMNISIGHT_AUTH_MODE` / `tenants.auth_features.*` / alembic schema state 三層既有 gating 的 precedence 與解耦關係 + 6 個 drift guard 模式 + rollback runbook（flip / restart / observe）。本 row 把分散在 AS.0.1 §4.5 註腳、AS.0.4 §4.2、AS.0.5 §7.2、AS.0.6 §11、AS.0.7 §9 / §10 五個 sibling design freeze doc 中的「knob false 時行為」碎片整合成一張完整的 AS-wide kill switch 行為表，作為下游 AS.1—AS.7 PR 必遵的 single-knob contract source。
>
> **目標讀者**：(1) 寫 AS.3.1 `backend/security/bot_challenge.py` 的人——本文件規範 module top 的 `_AS_ENABLED()` helper 介面、`BotChallengeResult.passthrough()` short-circuit 行為、不寫 audit row 的條件。(2) 寫 AS.4.1 `backend/security/honeypot.py` 的人——本文件規範 `validate_honeypot()` 在 knob false 時直接 return passthrough、不查 `auth_features.honeypot_active`、不寫 audit。(3) 寫 AS.1 / AS.2 OAuth client + token vault 的人——本文件規範 OAuth login 路徑 fallback 到 `/auth/oidc/{provider}` ad-hoc 流程（per AS.0.4 §4.2），oauth_tokens 表 schema 存在但無新 row。(4) 寫 AS.5.2 dashboard 的人——本文件規範 knob false 期間 dashboard banner「AS globally disabled — phase metrics paused」+ 月結 cron skip emit。(5) 寫 AS.6.x backend wire 的人——本文件規範 4 處 caller path 在 knob false 時 import `_AS_ENABLED()` short-circuit 順序。(6) 寫 AS.7.x frontend 的人——本文件規範 `OMNISIGHT_AS_FRONTEND_ENABLED`（NOT 自動 mirror）的獨立 frontend env 與 backend knob 的解耦。(7) 寫 AS.0.9 compat regression test #5 的人——本文件規範 5 顆 critical test 中 rollback knob 顆的 expected behaviors。
>
> **不在本 row 範圍**：實際 `backend/config.py` Settings field 落地（AS.3.1 PR land 時順便加，per AS.0.5 §7.2 既有 plan）、`backend/security/bot_challenge.py` 的 `_AS_ENABLED()` helper 實作（AS.3.1）、frontend `OMNISIGHT_AS_FRONTEND_ENABLED` 的 Next.js public env wiring（AS.7.x）、env-aware ops runbook（AS.8.3）、per-tenant rollback（AS.0.5 §7.1 已釘 per-tenant `turnstile_required=false` 走 row revert，與 single-knob 解耦）、alembic schema rollback（hard 否定——本 row §3.3 釘住 single-knob 永不觸發 schema 變更）。本 row 是 **plan-only design freeze**——下游 PR 必須遵守此處釘的 env 語義、noop 矩陣、precedence、drift guard。

---

## 1. 為什麼必須有 single-knob rollback design freeze

### 1.1 「全套 AS disable」需要 single source of truth

AS roadmap（TODO § AS）共 11 個 phase epic（AS.0—AS.7 + AS.10）會新增至少 8 個 backend module（`bot_challenge.py` / `honeypot.py` / `oauth_client.py` / `token_vault.py` / `password_generator.py` / `auth_features` 讀取 helper / 月結 cron / dashboard widget）+ 4 處 caller wire（login / signup / pwreset / contact）+ 整套 frontend redo（AS.7 8 頁）。沒有 single-knob 的後果：

- **回退路徑碎片化**：critical bug（譬如 Turnstile 全球故障 / OAuth provider 大規模 down / honeypot 誤判合法 user 大量 ban）出現時 ops 必逐 module / 逐 caller 各自 disable，回退時間從「flip env + restart」（30s）拉到「revert 8 處 commit + 重 build image」（30 min+），此期間合法 user 全部受影響。
- **Each-module-each-knob anti-pattern**：若 AS.1 / AS.2 / AS.3 各自有獨立 env knob（`OMNISIGHT_OAUTH_ENABLED` / `OMNISIGHT_TOKEN_VAULT_ENABLED` / `OMNISIGHT_BOT_CHALLENGE_ENABLED`），ops 需熟記 N 個 knob 的互相依賴（譬如 `OAUTH_ENABLED=true` 但 `TOKEN_VAULT_ENABLED=false` → OAuth login 寫不進 oauth_tokens → 500），運維 cost 線性放大。Single-knob 是「one operator action」的 SLO 設計。
- **Compat regression suite 缺一致 baseline**：AS.0.9 5 顆 critical test 必有「knob false → 完全等於 pre-AS 行為」的 oracle，沒有 single-knob 等於沒有 baseline，regression test 寫不出來。
- **Phase advance gate 的「pause」狀態無法表達**：knob false 期間 AS.0.5 phase metric denominator 應暫停，dashboard banner 必顯示「metrics paused」（per AS.0.5 §7.2）；若沒 single-knob，dashboard 無 ground truth 可判斷「目前是 AS off 還是 AS on but zero traffic」，admin 易誤判 unverified rate=0 是好事。

「single-knob」不是「方便」的選項，而是 **AS roadmap 的 pre-condition**——本 row 是 design freeze，把已散在 5 sibling doc 的條文整合成可被 AS.3.1 / AS.4.1 / AS.6.x 多 PR 同步引用的 SoT。

### 1.2 為何不沿用 `OMNISIGHT_AUTH_MODE=open` 作為 AS rollback

既有 `OMNISIGHT_AUTH_MODE` 三值 enum（`open` / `session` / `strict`，per `backend/auth.py:69-73` + `backend/config.py:770-792`）已是現役 auth gating 的 dev/prod 切換，但**不能**取代 AS.0.8 single-knob：

- **語義錯位**：`AUTH_MODE=open` 等於「所有 request 視為 anonymous super_admin（`_ANON_ADMIN`），CSRF skip，session 不驗，dashboard 視 user 為 admin」（per AS.0.1 §4.4），這是 dev/test 的「無認證」shortcut；而 AS.0.8 的需求是「**保留現有 password+session+MFA+API key 認證鏈不變**，只 disable AS 新增的 Turnstile / honeypot / OAuth 三層」。AUTH_MODE 是「auth off」、AS.0.8 是「AS off」，兩者語義正交。
- **production safety 不同**：`AUTH_MODE=open` 在 production deployment 是 hard error（`backend/config.py:788-792`，`ENV=production` + `AUTH_MODE!=strict` → exit 78 EX_CONFIG），因為它會關掉所有認證；AS.0.8 single-knob 必須在 production 也可 flip，才有「production 災難回退」的價值。
- **解耦既有 audit/test surface**：`AUTH_MODE` 影響既有 ~50 處 router auth gate；若 AS.0.8 復用此 knob，flip 同時關掉 AS + 既有 auth，blast radius 失控。

正確語義：**`OMNISIGHT_AS_ENABLED=false` 必等價「AS 1.0 之前的 OmniSight 行為」**——password / session / MFA / API key / webhook signature 全照舊，**只**有 AS roadmap 新加的 Turnstile / honeypot / OAuth client / token vault / 自動化 password 生成 / 月結 cron / phase advance 7 條 noop。AUTH_MODE 與 AS_ENABLED 互不取代、互不衝突。

### 1.3 與 AS.0.x sibling doc 的整合關係

| Sibling doc | knob false 時釘的子行為 | 本 row 整合條文位置 |
|---|---|---|
| AS.0.1 §4.5 註腳 | 「knob false ↔ §4.5 之 10 項目全 enabled + AS-added 行為全 noop」 | §3.1 noop 矩陣 row「Turnstile + honeypot module-level passthrough」 |
| AS.0.4 §4.2 | 「knob false 不等於 alembic downgrade」+「runtime caller 走 old vs new path」 | §3.3 schema decoupling invariant |
| AS.0.5 §7.2 | 「bot_challenge module short-circuit + dashboard pause banner + DB tenant state 不變」 | §3.1 row「bot_challenge」+ §6 dashboard pause |
| AS.0.6 §11 + §9 | 「三 axis bypass 全 noop（不檢查 list、不寫 bypass_* audit）」 | §3.1 row「automation bypass list」 |
| AS.0.7 §9 + §10 | 「honeypot module noop（validate_honeypot 直接 return passthrough）+ 不查 auth_features.honeypot_active」 | §3.1 row「honeypot」 |

本 row **不**改動 sibling doc 既有條文；本 row 是 sibling doc 的 unifier，當 sibling doc 之間出現 knob 行為描述衝突時，**以本 row §3 為準**。Sibling doc 後續 PR 修改時必須引用本 row 章節編號（避免 drift）。

---

## 2. Env knob 規格

### 2.1 Field declaration

`OMNISIGHT_AS_ENABLED` 必加為 `backend/config.py::Settings` field，**`bool` type、default `True`**、走 pydantic `extra='forbid'` 既有檢查。

```python
# backend/config.py — AS.3.1 PR land 時加（per AS.0.5 §7.2 既有 plan）
class Settings(BaseSettings):
    ...
    # AS.0.8 single-knob global rollback. Default True (AS active);
    # flip to False to disable the entire AS roadmap layer at runtime
    # without touching schema or other env knobs. See
    # docs/security/as_0_8_single_knob_rollback.md for the full noop
    # matrix. Module-global state audit (per implement_phase_step.md
    # Step 1, Answer #1): immutable Settings literal derived once at
    # process boot from env / .env — every uvicorn worker computes
    # the same value from the same source so cross-worker consistency
    # is automatic.
    as_enabled: bool = True
```

**為何 `bool` 而非 `str` enum**：

- AS.0.8 行為是 binary（active / disabled），無中間 state（不需要「partial AS」/「only honeypot」這類組合——這正是 single-knob 的設計初衷，per §1.1 反對 each-module-each-knob）。
- pydantic `bool` 自動 parse `OMNISIGHT_AS_ENABLED=true|false|1|0|yes|no` 大小寫、無 typo 風險；若用 `str` enum 必須 hard-code 校驗 + 容錯邏輯，徒增 cognitive load。
- 與既有 `gerrit_enabled` / `release_enabled` / `docker_enabled` 等 Settings bool field 命名/型別 pattern 一致。

**為何 default `True`（AS active）**：

- AS roadmap landing 後，新 deployment 應預設享受 AS 全套（OAuth + Turnstile + honeypot + auto-gen password），而既有 production tenant 受 `tenants.auth_features` per-tenant gate 預設全 false 保護（per AS.0.2）——env knob 預設 True 不會 break 既有 user，per-tenant gate 才是 user-facing 的 opt-in 開關。
- Default False 等於「AS 永遠 disabled 直到 ops 主動 flip」，違反 AS roadmap landing 後 default-on for new tenant 的設計初衷（per AS.0.2 alembic 0056 新 tenant 預設全開）。
- env knob 預設 True 但 per-tenant gate 預設 false（既有 tenant），雙層 gate 預設組合 = 「既有 tenant 行為零變動 + 新 tenant 全套享受」，這是 AS roadmap 的 compat 設計核心。

### 2.2 Boot-time lifespan validate

AS.3.1 / AS.4.1 / AS.6.x landing PR 必在 `backend/main.py::lifespan` 加 validation hook（與既有 `validate_startup_config` 同 pattern，per `backend/config.py:675`）：

```python
# backend/main.py lifespan — AS.3.1 PR land 時加
@asynccontextmanager
async def lifespan(app: FastAPI):
    ...
    # AS.0.8: single-knob validate at boot
    from backend.config import settings
    if settings.as_enabled:
        # AS active → ensure AS-required env knobs are coherent
        # (turnstile site secret + at least one OAuth provider config
        # + bot_challenge module imports cleanly)
        validate_as_runtime_prerequisites()  # AS.3.1 helper, raises ConfigValidationError on hard failure
    else:
        # AS disabled → log a one-line WARN (ops dashboard surface)
        # so it's grep-able from journalctl when the global rollback
        # was activated. Do NOT exit non-zero — knob false IS a valid
        # production state (catastrophic rollback path).
        _startup_logger.warning(
            "AS.0.8 single-knob: OMNISIGHT_AS_ENABLED=false — "
            "AS roadmap globally disabled at runtime. "
            "Bot challenge / honeypot / OAuth / token vault / "
            "auto-gen password are all noop. Per-tenant "
            "auth_features state preserved in DB."
        )
    yield
    ...
```

**Lifespan validate 順序**：必走在 `validate_startup_config()` **之後**（既有），以便 `OMNISIGHT_AUTH_MODE=open + ENV=production` 已 hard-fail（exit 78）的 case 不會被 AS knob false 「假裝 OK」蓋過。三層 startup validate 順序：

```
1. validate_startup_config()       — 既有，AUTH_MODE / admin_password / ENV=production gates
2. AS.0.8 lifespan AS validate     — 本 row 規範，as_enabled true → AS prerequisites; false → WARN only
3. validate_as_runtime_prerequisites()  — AS.3.1 內部，as_enabled=true 時才 call
```

### 2.3 Runtime read pattern

AS-related 模組 / caller 必走 `_as_enabled()` helper（**禁直接 `os.environ.get` 或 `settings.as_enabled` inline read**），以確保 single-knob 與下游 module 的耦合走唯一介面：

```python
# AS.3.1 backend/security/bot_challenge.py 必匯出（plan §2.3 釘）
from backend.config import settings

def _as_enabled() -> bool:
    """Single-knob global rollback gate (AS.0.8).

    Module-global state audit (per implement_phase_step.md Step 1,
    Answer #1): reads the immutable `settings.as_enabled` boolean,
    which is derived once at process boot from the env / .env file.
    Every uvicorn worker computes the same value from the same source
    so cross-worker consistency is automatic. No in-memory cache
    layer; trade-off is one attribute read per call (cost negligible,
    < 50ns) in exchange for monkeypatch-friendly semantics in tests.
    """
    return bool(settings.as_enabled)
```

**為何禁 inline read**：

1. **monkeypatch 一致性**——test 必走 `monkeypatch.setattr("backend.config.settings", "as_enabled", False)`，inline `os.environ.get` 在 test 環境改 env 不會反映到 settings instance，flaky test 來源。
2. **single-knob 介面唯一性**——4 個 backend module + N 處 caller 都從同 helper 讀，drift guard test 可只 grep 一條 import statement（per §8.4），降低 review 成本。
3. **未來擴充 hook 點**——若 future 加 `settings.as_enabled_per_tenant_override` 等高階 feature，只需改 `_as_enabled()` body，不需 N 處改 caller。

### 2.4 設定變更生效方式

`OMNISIGHT_AS_ENABLED` flip 必走 **restart-only** 生效（與 AS.0.5 §7.2 / AS.0.7 §10 既有 invariant 一致）：

| 操作 | 生效時機 | Restart 範圍 |
|---|---|---|
| `.env` flip + `docker compose restart backend-a backend-b` | 重啟完成（~10s） | 整 backend cluster（雙 replica） |
| `.env` flip + 不 restart | 永遠不生效 | n/a — Settings 是 boot-time literal |
| `kubectl set env deployment/backend OMNISIGHT_AS_ENABLED=false` | rolling restart 完成 | 取決於 deployment 策略 |

**禁 hot-reload**：

- 不引入 `SIGHUP` reload AS knob 機制——hot-reload 等於跨 worker 不同步（worker A reloaded, worker B 還是舊值），合法 user 同 session 看到行為跳變、debug 困難。
- 不引入 `/api/v1/admin/as-knob` runtime flip endpoint——admin user 可改 knob 等於「self-DoS」（admin 誤點 disable → 後續所有 user 失去 OAuth），違反 single-knob 是 ops-side hard rollback 的設計初衷。

**正確操作**：knob 屬 ops 級設定，必走 deployment pipeline（`.env` change + container restart）；不開放 admin / dashboard 自助 flip。

### 2.5 與 frontend 的解耦

frontend Next.js 不直接讀 `OMNISIGHT_AS_ENABLED`——必由 **獨立 env** `OMNISIGHT_AS_FRONTEND_ENABLED`（或 Next.js naming `NEXT_PUBLIC_OMNISIGHT_AS_FRONTEND_ENABLED`）控制 frontend bundle 是否載入 OAuth button / Turnstile widget script / 自動 password generator UI：

| Backend `as_enabled` | Frontend env | 結果 |
|---|---|---|
| true | true | AS 全套 active（normal） |
| true | false | backend ready，frontend 不渲 → user 看不到 OAuth/Turnstile/password gen，只能 password 登入 |
| false | true | frontend 載入 OAuth widget 但 backend reject → 500 / 401（**禁——boot-time validate 必抓**）|
| false | false | AS 全 disabled（rollback target state） |

**為何必獨立 env**：

- frontend bundle 是 build-time artifact（Next.js export），env 改 必 rebuild；backend 是 runtime artifact（restart 即可）。兩者 deploy cadence 不同，knob 必各自獨立。
- catastrophic rollback 場景（backend 必 restart at T+30s）若 frontend 也必 rebuild（T+10min），整體 rollback 時間從 30s 拉到 10min；獨立 env 讓 backend 立即 noop（reject AS request）+ frontend 短暫 mismatch（用 try/catch 容忍 backend 401，guide user fallback password login），rollback 速度提升 20×。
- AS.7.x landing PR 必加 frontend 對 backend AS reject 的 graceful-degrade UI（譬如 OAuth button click → 收到 503 → 顯示「OAuth temporarily unavailable, please use password」+ fallback 既有 password form）。

**Drift guard**：boot-time validate 必偵測「backend `as_enabled=false` + frontend env `true`」mismatch 場景，emit WARN（不 hard-fail，因為 ops 在 catastrophic rollback 時可能來不及 rebuild frontend）；同步 mismatch 時間 ≥ 60s 視為 ops 行為延遲、log severity 從 WARN 升 ERROR（per AS.5.2 dashboard 顯示）。詳 §8.5。

---

## 3. Knob false 行為矩陣（**本 row 核心**——AS-wide noop 規範）

### 3.1 Per-subsystem noop table

| 子系統 | knob true 行為 | knob false 行為（**本 row 釘死**）|
|---|---|---|
| **AS.1 OAuth client** (`backend/auth/oauth_client.py`) | OAuth provider catalog active；PKCE + state + nonce 完整流程；refresh rotation 走 token vault；audit `oauth.{login_init,login_callback,refresh,unlink}` | OAuth login 路徑 fallback 到既有 `/auth/oidc/{provider}` ad-hoc 流程（per AS.0.4 §4.2）；oauth_client 模組 import 仍 OK 但 `OAuthProviderClient.is_enabled() == False`；`/api/v1/auth/oauth/login/{provider}` endpoint 返 503 `{"error": "as_disabled"}`；不寫 audit |
| **AS.2 Token vault** (`backend/auth/token_vault.py`) | per-user / per-provider Fernet-encrypted oauth_tokens 寫入；refresh 60s 前自動觸發；revoke endpoint active | `token_vault.write/read/revoke` 所有 method 直接 `raise RuntimeError("AS disabled")`；caller 必由 try/except 容忍（與 OAuth login 503 path 一致）；oauth_tokens 表 schema 仍存在（per §3.3 schema decoupling）但無新 row；既有 row 不刪、不讀 |
| **AS.3 Bot challenge** (`backend/security/bot_challenge.py`) | Turnstile / reCAPTCHA / hCaptcha 完整 verify + score check + fallback chain + 5-phase metric | `bot_challenge.verify()` 直接 `return BotChallengeResult.passthrough()`；不查 `auth_features.turnstile_required`；不查 bypass list（per AS.0.6 §11）；不寫任何 `bot_challenge.*` audit row；not 走 phase advance gate |
| **AS.4 Honeypot** (`backend/security/honeypot.py`) | 4 處 form 渲 honeypot field（5 維 attribute）；server `validate_honeypot()` 5 步流程；3 個 EVENT_HONEYPOT_* audit | `validate_honeypot()` 直接 `return HoneypotResult(pass_=True, bypass_kind="as_disabled", field_name_used=None, failure_reason=None)`；4 處 form caller 收到 pass 不寫 audit；frontend `<HoneypotField>` JSX 仍渲（frontend 不知 backend knob，per §2.5）但 server 不檢查 |
| **AS.5.1 Auth event format** (`backend/audit_events.py` 新增 EVENT_OAUTH_* / EVENT_BOT_CHALLENGE_* / EVENT_HONEYPOT_*) | 常數 export 仍 active；caller 走 helper emit | 常數 export 仍 active（不影響）；但無 caller emit（因為 AS.1—AS.4 都 short-circuit pre-emit）；既有 EVENT_AUTH_* / EVENT_USER_* 不受影響 |
| **AS.5.2 Per-tenant dashboard** | phase metric 計算 + alert email cron + dashboard widget | dashboard widget 顯示 banner「AS globally disabled — phase metrics paused」（per AS.0.5 §7.2）；月結 cron skip emit（per AS.0.6 §6 月報空跑）；既有 user audit dashboard 不受影響 |
| **AS.6.x backend caller wire** (login / signup / pwreset / contact) | 4 處 caller 走 `bot_challenge.verify()` + `validate_honeypot()` + `oauth_client.callback()` | 4 處 caller 走 `_as_enabled()` short-circuit return `pass`（不 call AS.3 / AS.4 / AS.1 helper）；既有 password / MFA / API key / session 認證鏈完全不受影響 |
| **AS.7.x frontend redo** | 8 頁全套浮誇視覺 + OAuth button + Turnstile widget + auto-gen password UI | frontend 受 `OMNISIGHT_AS_FRONTEND_ENABLED` 控制（per §2.5），與 backend knob 獨立；mismatch 場景 frontend graceful-degrade 到 既有 password form |
| **AS.0.10 Auto-gen password core lib** (`backend/auth/password_generator.py`) | 3 種 style（Random / Diceware / Pronounceable）API active；`/api/v1/auth/generate-password` endpoint 返 password | endpoint 返 503 `{"error": "as_disabled"}`；module import 仍 OK（純函數）但不在 frontend 暴露 UI |
| **`tenants.auth_features.{oauth_login, turnstile_required, honeypot_active, automation_ip_allowlist}`** | runtime caller 讀此 column 決定 per-tenant 行為 | column 仍存在（per §3.3）但 AS module 全 short-circuit 不讀；既有 tenant 預設 false 行為與 knob false 行為**等價**（既有 tenant 看不出差異）；但 knob false 全 tenant（包含新 tenant 預設全開的 tenant）皆退到「pre-AS 行為」 |
| **既有 password / MFA / session / API key / webhook signature 認證鏈** | 不受 AS 影響（AS additive） | **完全不受影響**——既有 `backend/auth.py` / `backend/api_keys.py` / `backend/auth_baseline.py` / `backend/routers/webhooks.py` 路徑零變動 |

**核心 invariant**：knob false **不**動既有認證鏈，**只**讓 AS roadmap 新增的 7 個子系統全 noop。任何 AS-wide PR 必過 §8.6 drift guard test，驗證「knob false 時 既有 password login + MFA + API key + webhook 5 個 critical path 行為與 pre-AS 完全一致」。

### 3.2 Module short-circuit 規範

下游 AS module 必在 module top 加 short-circuit pattern：

```python
# AS.3.1 backend/security/bot_challenge.py 範本
from backend.config import settings

def _as_enabled() -> bool:
    return bool(settings.as_enabled)

async def verify(request, ...) -> BotChallengeResult:
    if not _as_enabled():
        # AS.0.8 single-knob: AS globally disabled — passthrough,
        # do not write audit, do not check bypass list, do not check
        # auth_features.turnstile_required.
        return BotChallengeResult.passthrough(reason="as_disabled")
    # ...rest of verify logic
```

```python
# AS.4.1 backend/security/honeypot.py 範本
from backend.config import settings

def _as_enabled() -> bool:
    return bool(settings.as_enabled)

def validate_honeypot(request, form_path, tenant_id, submitted) -> HoneypotResult:
    if not _as_enabled():
        # AS.0.8 single-knob: AS globally disabled — pass without
        # checking field, do not write audit, do not check
        # auth_features.honeypot_active.
        return HoneypotResult(
            pass_=True,
            bypass_kind="as_disabled",
            field_name_used=None,
            failure_reason=None,
        )
    # ...rest of validate logic
```

```python
# AS.1 backend/auth/oauth_client.py 範本
from backend.config import settings

class OAuthProviderClient:
    @classmethod
    def is_enabled(cls, provider: str) -> bool:
        if not bool(settings.as_enabled):
            return False
        # ...rest of provider catalog check

# Endpoint handler
@router.get("/api/v1/auth/oauth/login/{provider}")
async def oauth_login_init(provider: str):
    if not bool(settings.as_enabled):
        raise HTTPException(status_code=503, detail={"error": "as_disabled"})
    # ...rest of OAuth init
```

**Short-circuit 必加在 module/handler 入口最早可能位置**——禁先做 IO（譬如 query DB 取 `auth_features`）再 short-circuit，否則 knob false 期間仍走無謂 DB query，violates「全 noop」invariant。

### 3.3 Schema decoupling invariant（hard）

`OMNISIGHT_AS_ENABLED=false` **絕對不**觸發 alembic schema 變更。本 row 釘死下列 hard invariants：

1. **knob flip false ≠ alembic downgrade**——knob 是 runtime caller path 切換，schema state 永遠 forward-only（per AS.0.4 §4.2）。
2. **既有 AS-related schema column / table 在 knob false 時保留**：
   - `tenants.auth_features` JSONB column（per AS.0.2 alembic 0056）— 保留，per-tenant state 不變
   - `users.auth_methods` JSONB column（per AS.0.3 alembic 0058）— 保留，既有 oauth_<provider> tag 不刪
   - `oauth_tokens` table（per AS.2.2 alembic 0057）— 保留，既有 row 不刪不讀
   - 任何 future AS migration 加的 column / table — 同 pattern 保留
3. **knob flip true → false → true 行為對稱**：flip false 期間 DB state 不變；flip true 後 AS module 重新讀 DB（per-tenant `auth_features.turnstile_required` 仍是 flip 前的值），無需「重新 advance phase」或「重新 link OAuth」（per AS.0.5 §7.2）。
4. **knob flip 永不寫 audit row 表示「AS now disabled / now re-enabled」**——startup log 已記錄（per §2.2 lifespan WARN），不重複；audit chain 為「user-initiated action」設計，knob flip 是 ops-deploy action 不入 audit。
5. **knob flip 永不觸發 module reload / cache invalidate**——restart-only 生效（per §2.4），新 process boot 時讀新 settings 自然 fresh。

**Why hard**：schema state 是 disaster recovery 的最後一條保線；若 knob flip 觸發 schema 變更，rollback 就不再「30 秒 reversible」、變成「需要 backup restore」的災難。本 invariant 是 single-knob 設計的最後一道防火牆。

---

## 4. 與其他 gating layer 的 precedence

### 4.1 4 層 gating precedence（最高 → 最低）

per AS.0.5 §4 + AS.0.6 §4 既有 5 層 precedence，本 row **不**改既有層次但補充第 1 / 2 層的 hard invariants：

| 層次 | Gating | 行為 | knob 互動 |
|---|---|---|---|
| 1 | `OMNISIGHT_AUTH_MODE=open` | 全 anonymous super_admin、all auth bypassed（既有 dev/test mode）| 不受 AS knob 影響——AUTH_MODE=open 永遠跳過 AS（reason: AS 是 auth-on 之上的層，AUTH_MODE off 時 AS 無對象可保護）|
| 2 | **`OMNISIGHT_AS_ENABLED=false`（本 row）** | AS 全套 noop，既有 password/MFA/API key/session/webhook 不變 | 與 AUTH_MODE 正交、與 auth_features 解耦 |
| 3 | AS.0.6 三 axis bypass（API key / IP allowlist / test token） | bypass Turnstile + honeypot；不繞 password / MFA | knob false 時三 axis 全 noop（AS.0.6 §11） |
| 4 | `tenants.auth_features.{turnstile_required, honeypot_active, oauth_login}` per-tenant gate | per-tenant 行為（fail-closed / fail-open / OAuth on-off）| knob false 時不讀 column（短路在 AS module 入口）|
| 5 | AS.3.1 phase verify path（widget verify + score + fallback chain + honeypot fallback） | 完整 challenge 邏輯 | knob false 時不執行 |

### 4.2 Precedence 矩陣 truth table

下表釘住 4 層組合的 final 行為（**本 row 釘死**——drift guard test 必驗 §8.1 全 16 行）：

| AUTH_MODE | AS_ENABLED | AS.0.6 bypass match | auth_features.turnstile_required | Final 行為 |
|---|---|---|---|---|
| open | * | * | * | 跳過所有 auth + AS（dev/test only） |
| not-open | true | true | * | bypass + 寫 `bot_challenge.bypass_*` audit |
| not-open | true | false | false | Phase 1/2 fail-open OR Phase 3 fail-closed depending on phase |
| not-open | true | false | true | Phase 3 fail-closed（per-tenant opt-in） |
| not-open | false | * | * | **AS 全套 noop**——既有 password/MFA/API key 既有路徑不變、不寫 AS audit、phase metric 暫停 |

**Multi-axis match 行為**：knob false 期間 AS.0.6 bypass list 完全不檢查（AS module short-circuit 在前），caller 不會看到 `bot_challenge.bypass_*` audit row；caller 既有 API key / webhook signature / probe path 認證仍照舊（既有 `auth_baseline.py` 路徑），只是不再有「AS-specific bypass」概念。

### 4.3 與 `auth_features` 雙層 gate 的 precedence

`OMNISIGHT_AS_ENABLED` 是 **global gate**，`tenants.auth_features.*` 是 **per-tenant gate**：

| Global knob | Per-tenant gate（譬如 `turnstile_required`）| Final 行為 |
|---|---|---|
| true | true（new tenant 預設 OR existing opt-in） | per-tenant gate 生效（AS active for this tenant）|
| true | false（既有 tenant 預設） | AS code path 走但 caller-level fail-open（既有 tenant 行為零變動）|
| false | true | **AS 全 noop**（global knob wins）|
| false | false | AS 全 noop（兩層皆 off）|

**Critical invariant**：global knob false 時**永不讀 per-tenant gate**——in-line short-circuit 順序為 `if not _as_enabled(): return passthrough`，後續不 query DB。原因：

- 防呆：global knob false 是 catastrophic rollback、ops 不希望「某 tenant per-tenant gate=true 還是觸發 AS」（即使 AS bug 是 tenant-localized，knob false 必須 universal）。
- Performance：knob false 期間每 request 都 query DB 取 `auth_features` 是浪費；short-circuit 讓 catastrophic rollback 也順便降低 backend load（rollback 期間通常 backend 已壓力大）。
- Test simplicity：drift guard §8.6 只需要驗 「knob false → AS module 不 query DB」，不需驗 「knob false + per-tenant true → 仍 noop」這類 cross-product。

---

## 5. Audit 行為矩陣

per §3.1 各 module noop 規範，knob false 時各類 audit row 行為釘死：

| Audit event family | knob true 行為 | knob false 行為 |
|---|---|---|
| `bot_challenge.{pass, unverified_*, blocked_lowscore, jsfail_*}` | 寫（per AS.0.5 §3） | **不寫**——bot_challenge 整 module short-circuit |
| `bot_challenge.bypass_{apikey, webhook, chatops, bootstrap, probe, ip_allowlist, test_token}` | 寫（per AS.0.6 §3） | **不寫**——bypass list 不檢查 |
| `bot_challenge.{phase_advance_*, phase_revert_*}` | dashboard 觸發 helper emit（per AS.0.5 §3） | **不寫**——dashboard widget 顯示 banner「paused」、advance/revert 按鈕 disabled |
| `honeypot.{pass, fail, form_drift, jsfail_*}` | 寫（per AS.0.7 §3.4） | **不寫**——validate_honeypot short-circuit |
| `oauth.{login_init, login_callback, refresh, unlink}` | 寫（per AS.5.1 future） | **不寫**——oauth_client return 503 |
| `tenant.honeypot_active_flip` / `tenant.turnstile_required_flip` / `tenant.automation_ip_allowlist_update` | 寫（per AS.0.6 §5.2） | **可寫**（admin Settings UI 仍可改 column——per §3.3 schema decoupling）但下次 AS module 不會用到該 column（因 short-circuit）|
| 既有 `user.{login_success, login_failure, password_change}` / `auth.{session_invalidate, mfa_verify}` / `api_key.*` / `webhook.*` | 寫（既有路徑） | **照舊寫**——既有路徑零變動 |

**為何不寫 `as_disabled` audit row**：

- knob false 是 ops-deploy action 不是 user action（per §3.3 invariant #4）；audit chain 是 user-initiated 設計。
- 若每 request 都寫一條「AS disabled」row，audit table 體積爆炸（catastrophic rollback 期間流量可能高）；startup WARN log（per §2.2）已是 ops side ground truth。
- AS.5.2 dashboard banner（per §6）是 ops 側 visibility，audit row 是 user 側 trail——兩者目的不同、knob false 走前者不走後者。

**例外（少數 case 仍寫 audit）**：

- 若 user 觸發 OAuth login → backend 503 `as_disabled`：寫 `auth.oauth_login_blocked_as_disabled` row（**by user-initiated action**），讓 user 排查為何 OAuth button 不 work；但這是 endpoint handler 收到 user request 後的 audit，不是 AS module 內部。
- 若 admin flip per-tenant `turnstile_required` 時 knob 已 false：admin Settings UI 顯示 warning「AS knob currently false — your flip will only take effect when global AS is re-enabled」，但 audit 仍寫（admin action 必入 trail）。

---

## 6. Dashboard banner + 月結 cron 行為

### 6.1 AS.5.2 Dashboard banner

knob false 期間 dashboard 必顯示 persistent banner（**per AS.0.5 §7.2 既有規範本 row 重申**）：

```
┌─────────────────────────────────────────────────────────────┐
│ ⚠️ AS roadmap globally disabled (OMNISIGHT_AS_ENABLED=false)│
│                                                             │
│ Bot challenge / honeypot / OAuth / token vault / phase      │
│ metrics are paused. Per-tenant settings preserved in DB.    │
│                                                             │
│ Set OMNISIGHT_AS_ENABLED=true and restart backend to        │
│ resume. See docs/security/as_0_8_single_knob_rollback.md.   │
└─────────────────────────────────────────────────────────────┘
```

**Banner 行為釘死**：

- 出現位置：dashboard 主頁頂部 sticky banner（不被 user dismiss）；admin Settings 頁同步顯示。
- 數據來源：frontend 走 `GET /api/v1/runtime/as-status` endpoint 讀 backend `_as_enabled()` 結果（**禁** frontend 自行 read env，per §2.5 解耦）；endpoint return `{"as_enabled": bool, "as_disabled_since": isoformat | null}`。
- 「`as_disabled_since`」timestamp：backend lifespan 啟動時若 `as_enabled=false` 寫 `runtime_state` table（既有 K8 schema）一筆 `as_disabled_at = now()`；下次 lifespan 啟動 `as_enabled=true` 時清此 row。Banner 顯示「disabled since YYYY-MM-DD HH:MM UTC」。
- 「unverified rate / jsfail rate / phase metric」widget：knob false 期間 grey-out，顯示 "metrics paused"，**禁顯示 0**（防 admin 誤判）；hover tooltip 顯示「metrics paused due to global AS rollback」。
- 「Advance to Phase 2 / Phase 3」按鈕：knob false 期間 disabled + tooltip「cannot advance while AS is globally disabled」。

### 6.2 AS.0.6 月結 cron skip

per AS.0.6 §6 月結 cron（每月 1 日 09:00 UTC）行為：

| knob 狀態 | Cron 行為 | Email / dashboard 行為 |
|---|---|---|
| true（active） | normal — per-tenant 跑 + emit `automation_bypass.monthly_summary` audit + email send | normal |
| false 整月（如 2026-04 全月 disabled） | cron 跑但 emit 一條 `automation_bypass.monthly_summary` row with `metadata.skipped=true, reason="as_disabled"`；不 emit per-tenant rows | email 走 zero-event 報告 + 顯示「AS disabled this month — no automation bypass events recorded」|
| false 部分月（譬如月中 flip false 後又 flip true） | cron 跑但 metadata 加 `partial_disabled=true` + `disabled_periods=[(start, end), ...]` | email 標 banner「AS was disabled for X% of the month」|

**Cron 永不 skip**——必跑、emit row、發 email（即使 zero event），保證「月初該收 email 一定收到」是 ops baseline 信任機制；skip 等於 silent failure。

### 6.3 AS.5.1 weekly Turnstile alert cron

per AS.0.5 §6.2 weekly alert cron 行為：

| knob 狀態 | Cron 行為 |
|---|---|
| true | normal — 計算 unverified_rate + email |
| false | cron 跑但 emit 一條 `bot_challenge.weekly_alert_skipped` row + email 走「AS globally disabled — weekly alert skipped」 stub email；不計算 metric（避免 0 row 觸發 false alarm） |

---

## 7. Acceptance criteria + Drift guards

### 7.1 Acceptance criteria（給 deploy gate 用）

**knob false → AS noop 順過 5 顆 critical（per AS.0.9 5 顆 critical）**：

- [ ] 既有 password login（`/api/v1/auth/login`）— knob false 時行為與 pre-AS 完全一致：response 200 + session cookie + audit `user.login_success`；不寫 `bot_challenge.*` 任何 row；frontend 不渲 honeypot field（per `OMNISIGHT_AS_FRONTEND_ENABLED=false` 同步），knob false 但 frontend env 仍 true 時 server 收到 honeypot field 也忽略不寫 audit
- [ ] 既有 password+MFA（`/api/v1/auth/mfa/challenge`）— 同上，MFA challenge 走既有 `backend/mfa.py` 路徑、與 AS knob 完全解耦
- [ ] API key auth（Bearer `omni_*`）— knob false 時 `auth_baseline._has_valid_bearer_token` 仍走既有路徑、`request.state.api_key` 仍 cache、不觸發 `bot_challenge.bypass_apikey` audit
- [ ] OAuth login attempt — knob false 時 `/api/v1/auth/oauth/login/{provider}` 返 503 `{"error": "as_disabled"}`；既有 `/auth/oidc/{provider}` ad-hoc legacy path（若有）仍 work
- [ ] knob flip true → false → true 行為對稱：disable 期間 per-tenant `auth_features` state 不變、re-enable 後 phase advance state 不重置、不重新 link OAuth

### 7.2 Drift guards（給未來 PR 必須維護的對齊關係）

#### 7.2.1 Settings field declared guard

```python
def test_as_0_8_settings_field_declared():
    """AS.0.8 §2.1 invariant: as_enabled 是 backend.config.Settings 的 bool field、default True."""
    from backend.config import Settings
    assert hasattr(Settings, "model_fields") and "as_enabled" in Settings.model_fields, (
        "as_enabled field not declared in Settings — AS.0.8 §2.1 violation"
    )
    field = Settings.model_fields["as_enabled"]
    assert field.annotation is bool, (
        f"as_enabled field type must be bool, got {field.annotation}"
    )
    assert field.default is True, (
        f"as_enabled default must be True (AS active for new deploy), got {field.default}"
    )
```

#### 7.2.2 No inline env read guard

```python
def test_as_0_8_no_inline_as_enabled_read():
    """AS.0.8 §2.3 invariant: AS-related modules 必走 _as_enabled() helper，禁 inline os.environ.get('OMNISIGHT_AS_ENABLED')."""
    import pathlib, re
    inline = re.compile(r'os\.environ\.get\([\'"]OMNISIGHT_AS_ENABLED[\'"]\)')
    src_files = [
        "backend/security/bot_challenge.py",
        "backend/security/honeypot.py",
        "backend/auth/oauth_client.py",
        "backend/auth/token_vault.py",
        "backend/auth/password_generator.py",
    ]
    for path in src_files:
        p = pathlib.Path(path)
        if not p.exists():
            continue  # module 尚未落地，跳過
        text = p.read_text()
        assert not inline.search(text), (
            f"{path}: 必走 _as_enabled() helper，禁 inline os.environ.get('OMNISIGHT_AS_ENABLED')"
        )
```

#### 7.2.3 Module short-circuit at entry guard

```python
def test_as_0_8_module_short_circuit_first():
    """AS.0.8 §3.2 invariant: AS module 入口 short-circuit 必先於 IO（DB / cache / env read）."""
    import ast
    import pathlib
    targets = {
        "backend/security/bot_challenge.py": "verify",
        "backend/security/honeypot.py": "validate_honeypot",
    }
    for src_path, fn_name in targets.items():
        p = pathlib.Path(src_path)
        if not p.exists():
            continue
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
                # 第一條語句必為 short-circuit pattern
                first_stmt = node.body[0] if node.body else None
                # if not _as_enabled(): return ...
                assert isinstance(first_stmt, ast.If), (
                    f"{src_path}::{fn_name} 入口必為 if not _as_enabled(): return passthrough"
                )
                test_src = ast.dump(first_stmt.test)
                assert "_as_enabled" in test_src, (
                    f"{src_path}::{fn_name} 入口 short-circuit 必呼 _as_enabled()"
                )
```

#### 7.2.4 No alembic migration on knob flip guard

```python
def test_as_0_8_no_alembic_on_knob_flip():
    """AS.0.8 §3.3 invariant: knob flip 永不觸發 alembic migration、永不 import alembic env."""
    import pathlib, re
    src = pathlib.Path("backend/main.py").read_text()
    # Lifespan 中 'as_enabled=false' 分支不可 import alembic
    # 簡單 grep：若 same line / 5 lines 內出現 alembic upgrade/downgrade，視為 violation
    pattern = re.compile(r'(as_enabled.*alembic|alembic.*as_enabled)', re.DOTALL)
    assert not pattern.search(src), (
        "backend/main.py lifespan 不可耦合 as_enabled flip 與 alembic migration (AS.0.8 §3.3)"
    )
```

#### 7.2.5 Frontend / backend env mismatch guard

```python
def test_as_0_8_frontend_env_separate():
    """AS.0.8 §2.5 invariant: frontend 必有獨立 OMNISIGHT_AS_FRONTEND_ENABLED env、不直接讀 backend knob."""
    import pathlib, re
    # frontend Next.js client code 不可 import / read 'OMNISIGHT_AS_ENABLED'（必走 NEXT_PUBLIC_OMNISIGHT_AS_FRONTEND_ENABLED）
    forbidden = re.compile(r'OMNISIGHT_AS_ENABLED(?!_FRONTEND)')  # negative lookahead — allow OMNISIGHT_AS_FRONTEND_ENABLED
    for p in pathlib.Path("app").rglob("*.tsx"):
        text = p.read_text()
        assert not forbidden.search(text), (
            f"{p}: frontend 禁直接讀 OMNISIGHT_AS_ENABLED，必走 OMNISIGHT_AS_FRONTEND_ENABLED (AS.0.8 §2.5)"
        )
    for p in pathlib.Path("app").rglob("*.ts"):
        text = p.read_text()
        assert not forbidden.search(text), (
            f"{p}: frontend 禁直接讀 OMNISIGHT_AS_ENABLED，必走 OMNISIGHT_AS_FRONTEND_ENABLED (AS.0.8 §2.5)"
        )
```

#### 7.2.6 Knob false → existing auth path unchanged guard

```python
def test_as_0_8_existing_auth_unchanged_when_knob_false(monkeypatch):
    """AS.0.8 §7.1 critical: knob false 時既有 password / MFA / API key / webhook 認證鏈完全不變."""
    from backend.config import settings
    monkeypatch.setattr(settings, "as_enabled", False)
    # 1. 既有 password login 走既有路徑
    from backend.auth import _login_handler  # 名字以實際為準
    # ... assert response shape == pre-AS shape, no bot_challenge.* audit row written

    # 2. API key auth 不觸發 bypass_apikey audit
    from backend.auth_baseline import _has_valid_bearer_token
    # ... assert audit table 無 bot_challenge.bypass_* row in last call

    # 3. webhook signature 走既有 backend.routers.webhooks
    # ... assert response 200 + audit 'webhook.received' row 不變

    # （test 細節 AS.0.9 PR 落地時補 fixture）
```

> Drift guard §7.2.6 是 AS.0.9 5 顆 critical regression test 中第 5 顆「rollback knob」的核心 oracle；本 row plan-only，不實作 test，但釘住 test contract（given knob false、expected 既有 auth 完全不變）。

---

## 8. Module-global state audit (per `docs/sop/implement_phase_step.md` Step 1)

1. **本 row 全 doc / 零 code 變動** — 不引入任何 module-level singleton / cache / global；plan §2.1 釘的 `Settings.as_enabled` field 是規範 AS.3.1 PR 落地時必加，當下 `backend/config.py::Settings` 不動。
2. **未引入新 module-level global / singleton / cache**。
3. **未來 AS.3.1 / AS.4.1 / AS.1 / AS.2 module-global state 預先標註**：
   - `Settings.as_enabled` 是 immutable boolean derived once at process boot from env / .env（Answer #1，per `backend/config.py` 既有 Settings field pattern 一致；每 worker 從同 env 推同 bool，cross-worker consistency 自動）。
   - `_as_enabled()` helper 是純函數 `return bool(settings.as_enabled)`，無 in-memory cache（Answer #1，cost < 50ns per call、無 monkeypatch 失效風險）。
   - 任何 AS module 的 `_BYPASS_PATH_PREFIXES` / `_BYPASS_CALLER_KINDS` / `_FORM_PREFIXES` / `_RARE_WORD_POOL` / `_TEST_TOKEN_HEADER` / `_PROVIDER_SECRET_ENVS` / `OS_HONEYPOT_CLASS` 全 immutable 常數（Answer #1）。
   - `runtime_state` table 中 `as_disabled_at` 欄位（per §6.1）走 PG row + Y9 既有 60s cache（Answer #2，per-PG-coordinated cache invalidation）；本 row 不引入新 cache。
   - knob flip 走 restart-only（per §2.4），新 process boot 時讀新 settings 自然 fresh，不存在 hot-reload module-global state 問題。
4. **AS.3.1 / AS.4.1 / AS.1 / AS.2 / AS.6.x PR 落地時 Step 1 必再次驗證**——本 row 是 plan-only contract，下游 PR 自有 implementation-level audit。

---

## 9. Read-after-write timing audit

- **本 row 改動**：純 doc 落地，無 schema / 無 caller / 無 transaction 行為變化；不適用 timing 分析。
- **plan 文件本身對未來 PR 的 timing 約束**：
  - **`OMNISIGHT_AS_ENABLED` env flip → restart-only（per §2.4）**：env 改 必 restart backend (`docker compose restart backend-a backend-b`)，重新讀 env；不存在 read-after-write 問題（restart = full state reset）。
  - **knob flip true → false 期間 in-flight request 行為**：restart 期間既有 worker process 走完手上 request（worker graceful shutdown，per `uvicorn --timeout-graceful-shutdown 30` 既有設定）；新 worker boot 後讀新 settings、新 request 走 noop path。中間 ≤ 30s 的 in-flight request 走 knob true 行為（仍 emit AS audit row），ops 側 banner 顯示「AS disabled」可能滯後 ~30s——容忍，理由：rollback 場景下 ≤ 30s 的 audit row 殘留不致影響 metric trend。
  - **dashboard widget banner 顯示 timing**：`/api/v1/runtime/as-status` endpoint 走 settings literal read（無 cache），request-by-request real-time；frontend banner refresh 走 既有 dashboard polling（per AS.5.2，~5s cadence）；最壞 5s eventual consistency。容忍。
  - **per-tenant `auth_features` admin flip + knob false 互動**：admin 在 knob false 期間改 `turnstile_required=true` → audit row 立即寫 + JSONB UPDATE + Y9 cache invalidate；下一 request AS module short-circuit 在前不讀 column → 行為仍 noop；admin 可能困惑「我設了 turnstile 為何沒生效」，UI 必加 warning「AS knob currently false — your change will only take effect when global AS is re-enabled」（per §5）。
  - **AS module short-circuit ordering invariant**：所有 AS module 的 `_as_enabled()` check **必先於** 任何 DB query / cache lookup / env read（per §3.2 module 入口最早可能位置）；違反此 ordering 會在 knob false 期間仍走 DB query，違反「全 noop」invariant。drift guard §7.2.3 enforce。

---

## 10. Pre-commit fingerprint grep（per SOP Step 3）

- 對 `docs/security/as_0_8_single_knob_rollback.md`：`_conn()` / `await conn.commit()` / `datetime('now')` / `VALUES (?,...)` 全 0 命中（doc 本身只有規範描述 + Python test pattern 範例，無可執行 SQL/code 殘留；Python pattern 為 documentation only、不會被 import 執行）。
- 對 `TODO.md` 改動 hunk：1 行單 row 狀態翻 `[ ]` → `[x]` + reference 條目，無 fingerprint。
- 對 `HANDOFF.md` 改動 hunk：plan-only entry header + 範圍 + contract + 設計決策，無 fingerprint。
- Runtime smoke：本 row 不適用 — 純 plan doc，無 code path 可 smoke。drift guard tests（§7.2.1 - §7.2.6）在 AS.3.1 / AS.4.1 / AS.1 / AS.2 / AS.6.x PR 落地、屆時各自有自己的 smoke。

---

## 11. 非目標 / 刻意不做的事

1. **不引入 hot-reload knob 機制** — knob flip 走 restart-only（per §2.4）。理由：(a) hot-reload 跨 worker 不同步、(b) admin 自助 flip = self-DoS 風險、(c) restart 30s 在 catastrophic rollback 場景已足夠快、(d) 與既有 Settings literal pattern（boot-time derived）一致。
2. **不引入「partial AS disable」/「only Turnstile off, OAuth still on」** — single-knob 是 binary，per §1.1 拒絕 each-module-each-knob anti-pattern。理由：partial disable 排列組合爆炸（2^7 種狀態）、ops 心智模型崩、test matrix 失控。tenant-level 細粒度走 `auth_features` per-tenant gate（per §4.3），不在 global knob 層處理。
3. **不引入 admin UI runtime flip endpoint** — 無 `POST /api/v1/admin/as-knob/flip` endpoint。理由：(a) admin user 可改 knob = self-DoS，(b) ops 級設定屬 deployment pipeline、走 `.env` + restart，(c) UI flip 與 audit chain 設計衝突（admin action 寫 audit ≠ ops deploy action）。
4. **不引入 per-tenant single-knob** — 無 `tenants.auth_features.as_enabled_per_tenant`。理由：(a) 與 `turnstile_required` / `oauth_login` / `honeypot_active` 三 per-tenant gate 重疊、SoC 違反；(b) per-tenant 細粒度走既有三 column；(c) global knob 是 ops catastrophic rollback 工具，per-tenant 是 admin 業務工具，兩者混用會誤導 ops。
5. **不規範 knob flip 的「audit row」表示** — knob flip 走 startup log，不寫 audit（per §3.3 invariant #4）。理由：knob flip 是 ops-deploy action 不是 user action；audit chain 是 user-initiated 設計；ops side ground truth 是 startup log + dashboard banner，不必重複。
6. **不規範 frontend 「AS disabled」graceful-degrade UI 細節** — 留給 AS.7.x 落地。理由：(a) frontend graceful-degrade 涉及具體 UI component / Tailwind class / motion / a11y，不在 plan-only design freeze 範圍；(b) AS.7.x 必引用本 row §2.5 解耦語義 + §7.2.5 frontend env drift guard。
7. **不規範跨 deployment/region 的「partial knob false」** — 譬如 region-a backend knob false / region-b knob true。理由：(a) 多 region OmniSight 部署是 ops topology 問題、不在 single-knob scope；(b) global knob 是「整 deployment 一致」、跨 region 不一致 = ops mistake、本 row 不為這類 anti-pattern 提供 first-class support；(c) 真實多 region 場景必走 region-level deploy pipeline、各自 flip env、各自 monitor。
8. **不規範 knob false 時的「monthly cron skip」/「weekly cron skip」邊界** — per §6.2 / §6.3 cron 必跑、emit row、發 email；不論 month-fully-disabled 或 partial-disabled 都按表行事。理由：cron 邏輯複雜化（skip / partial-skip / always-emit）會引入分支爆炸；統一「always emit」+ metadata 標記 disabled period 是最簡 invariant。
9. **不替代 disaster recovery 的 backup / restore 流程** — knob false 是 30 秒級的「可逆 rollback」；schema 級 / data-level 災難仍走既有 `docs/ops/db_failover.md` 15 節 runbook。理由：兩個機制 scope 不同——knob 是 code path 切換、backup 是 schema/data state 還原；ops runbook 必明確指出何時用哪個。
10. **不規範 AS.0.10 auto-gen password core lib 在 knob false 時的「既有 admin password gen 行為」** — `OMNISIGHT_ADMIN_PASSWORD` 既有 bootstrap 行為與 AS.0.10 完全解耦（per `backend/auth.py::ensure_default_admin`）；knob false 時 AS.0.10 endpoint 返 503，但 bootstrap admin password 仍走既有 password 機制。理由：bootstrap admin 是 first-boot 強制流程，必獨立於 AS roadmap。

---

## 12. 與 AS.0.x / AS.x 其他 row 的互動 / 邊界

| 互動對象 | 邊界 |
|---|---|
| **AS.0.1** §4.5 inventory + §6 註腳 | 本 row 是 §4.5 註腳「`OMNISIGHT_AS_ENABLED=false` ↔ §4.5 之 10 項目全 enabled + AS-added 行為全 noop」的 design freeze；§4.5 inventory 不變、本 row §3.1 noop 矩陣對齊 |
| **AS.0.2** alembic 0056 `tenants.auth_features` JSONB | 強耦合——本 row §3.1 / §3.3 / §4.3 規範 knob false 時不讀 `auth_features` column；既有 column 預設 全 false 與 knob false 行為等價（既有 tenant 看不出差異），但 knob false 影響全 tenant、`auth_features` 影響 per-tenant |
| **AS.0.3** `users.auth_methods` + account-linking | 弱耦合——knob false 時 OAuth login 路徑 503、不走 `account_linking.link_oauth_after_verification()`；既有 password+MFA 帳戶完全不變 |
| **AS.0.4** credential refactor migration plan | 強耦合——本 row §3.3 schema decoupling invariant 與 AS.0.4 §4.2「knob false ≠ alembic downgrade」對齊；本 row 統一條文 source |
| **AS.0.5** Turnstile fail-open phased strategy | 強耦合——本 row §3.1 row「bot_challenge」+ §6 dashboard pause 與 AS.0.5 §7.2 對齊；本 row 統一條文 source |
| **AS.0.6** automation bypass list | 強耦合——本 row §3.1 row「automation bypass list」+ §4.2 truth table 與 AS.0.6 §11 / §9 對齊；本 row 統一條文 source |
| **AS.0.7** honeypot field 設計 | 強耦合——本 row §3.1 row「honeypot」與 AS.0.7 §9 / §10 對齊；本 row 統一條文 source |
| **AS.0.9** compat regression test suite | **本 row 釘 AS.0.9 第 5 顆 critical test 的 oracle**——「knob false → 既有 5 critical path 完全等於 pre-AS」；§7.2.6 drift guard 是 AS.0.9 PR test fixture contract |
| **AS.0.10** auto-gen password core lib | 弱耦合——knob false 時 endpoint 返 503，但 lib 函數本身（純算）仍可 import；既有 admin bootstrap password 流程不受影響 |
| **AS.1** OAuth client core | **強耦合**——本 row §3.1 row「OAuth client」釘 AS.1 module short-circuit interface；503 endpoint contract 與 §4.5 既有 `/auth/oidc/{provider}` ad-hoc fallback path 對齊 |
| **AS.2** Token vault | **強耦合**——本 row §3.1 row「Token vault」釘 `token_vault.write/read/revoke` 在 knob false 時 raise；oauth_tokens schema 保留（per §3.3）|
| **AS.3.1** `bot_challenge.py` module | **本 row 是 AS.3.1 的 spec source（與 AS.0.5 / AS.0.6 並列）**——`_as_enabled()` helper interface + `BotChallengeResult.passthrough()` short-circuit 必對齊本 row §3.2 |
| **AS.4.1** `honeypot.py` module | **本 row 是 AS.4.1 的 spec source（與 AS.0.7 並列）**——`validate_honeypot()` short-circuit return passthrough 必對齊本 row §3.2 |
| **AS.5.1** auth event format | 弱耦合——knob false 時不寫任何 AS audit event；既有 EVENT_AUTH_* / EVENT_USER_* 常數不受影響 |
| **AS.5.2** per-tenant dashboard | **強耦合**——本 row §6.1 / §6.2 / §6.3 規範 dashboard banner + 月結 cron + weekly alert cron 在 knob false 時的行為；AS.5.2 PR 落地必對齊 |
| **AS.6.x** OmniSight self backend wire | **強耦合**——本 row §3.1 row「AS.6.x backend caller wire」釘 4 處 caller 走 `_as_enabled()` short-circuit 順序；AS.6.3 / AS.6.4 PR 落地必對齊 |
| **AS.7.x** UI redesign | 強耦合——本 row §2.5 釘 frontend 獨立 env + §7.2.5 drift guard；AS.7.x PR 落地必加 graceful-degrade UI |
| **AS.8.3** ops runbook | 強耦合——本 row 是 ops runbook 的 design source；AS.8.3 把 §6 dashboard banner + §13 rollback runbook 翻成 step-by-step ops SOP |

---

## 13. Rollback runbook（給 ops 用）

### 13.1 「AS catastrophic rollback」標準操作（≤ 60 秒）

**Trigger 場景**：

- Turnstile 全球故障 / Cloudflare 邊緣節點問題 → 大量 user 卡 challenge 不能登入
- OAuth provider（Google / GitHub / Microsoft）大規模 down → 大量 user 卡 OAuth callback 不能登入
- AS.4 honeypot 誤判合法 user 大量 ban → ban list 滿、合法 user 不能登入
- AS.3 phase metric 失準 → false positive 大量 401（Phase 3 fail-closed bug）

**Operator 動作**：

```bash
# 1. SSH 進 ops admin host
ssh ops@omnisight-prod

# 2. 編輯 .env，flip AS_ENABLED
sed -i 's/^OMNISIGHT_AS_ENABLED=.*/OMNISIGHT_AS_ENABLED=false/' /opt/omnisight/.env
# 或 export OMNISIGHT_AS_ENABLED=false 加進 docker-compose.override.yml environment block

# 3. Restart backend cluster
docker compose -f /opt/omnisight/docker-compose.yml restart backend-a backend-b

# 4. 觀察 startup log 確認 WARN 出現
docker compose logs --tail 50 backend-a | grep "AS.0.8 single-knob"
# Expect: "OMNISIGHT_AS_ENABLED=false — AS roadmap globally disabled"

# 5. Smoke test
curl -X POST https://omnisight.example.com/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "..."}'
# Expect: 200 + session cookie，無 bot_challenge.* audit row（grep audit log 確認）

# 6. 通知 dashboard banner 已顯示
# 開 https://omnisight.example.com/admin/dashboard 確認 banner 出現
```

**全程預期 ≤ 60 秒**——SSH 5s + sed 1s + restart 30s（含 graceful shutdown）+ smoke 10s。

### 13.2 「AS re-enable」標準操作（≤ 60 秒，需驗證 fix 已 deploy）

```bash
# 0. 先確認 fix 已 deploy 到 backend image / dependencies
# （視 trigger 場景而定：Turnstile 復活 / OAuth provider 復活 / 修 phase metric bug）

# 1. flip back
sed -i 's/^OMNISIGHT_AS_ENABLED=.*/OMNISIGHT_AS_ENABLED=true/' /opt/omnisight/.env

# 2. Restart
docker compose restart backend-a backend-b

# 3. 確認 startup 不再有 WARN
docker compose logs --tail 50 backend-a | grep "AS.0.8 single-knob"
# Expect: 0 命中（knob true 時 lifespan 不 emit WARN）

# 4. 確認 dashboard banner 消失
# 開 https://omnisight.example.com/admin/dashboard 確認 banner 不見

# 5. 觀察 1 小時 — phase metric 是否恢復、unverified rate 是否合理
```

### 13.3 「半邊 backend rollback」反模式 — 禁

**禁**：只 flip 一個 backend replica（譬如 backend-a knob false / backend-b knob true）、不 restart。

理由：跨 worker 行為不一致 → user request 走到哪個 worker 行為跳變、debug 困難、audit chain 不一致；與 §2.4 restart-only invariant 衝突。

正確做法：必同時 flip + 同時 restart 兩 replica。

### 13.4 與 disaster recovery (db_failover.md) 的邊界

| 場景 | 用工具 |
|---|---|
| AS code path 故障（Turnstile / OAuth / honeypot bug）| **AS.0.8 knob flip** — 30s 級 reversible |
| PG primary failover / read replica 失聯 | `docs/ops/db_failover.md` — 分鐘級 |
| Schema migration 失敗 | `alembic downgrade` — 必 backup restore |
| Encryption key compromise | secret_store 重生 + 全表 re-encrypt — 小時級 |

**禁**：用 AS knob 解決 schema/data 級災難（per §11 #9 既釘）。AS knob 只覆蓋 code path 級。

---

## 14. Production status

* 文件本身：plan-only design freeze。
* 影響的程式碼：本 row 不改 code；AS.3.1 / AS.4.1 / AS.1 / AS.2 / AS.5.2 / AS.6.x / AS.7.x follow-up rows 才動。
* Rollback 影響：plan 無 runtime impact、無 rollback。

**Production status: dev-only**
**Next gate**: 不適用 — 本 row 是 design doc。Schedule 由 AS.3.1 (`backend/security/bot_challenge.py` + `backend/config.py::Settings.as_enabled` field land) PR 觸發 env knob 落地 + lifespan validate hook；AS.4.1 / AS.1 / AS.2 / AS.6.x PR 各自接 `_as_enabled()` short-circuit；AS.5.2 dashboard PR 觸發 banner + cron skip；AS.7.x frontend PR 觸發 `OMNISIGHT_AS_FRONTEND_ENABLED` env + graceful-degrade UI；AS.0.9 PR 觸發 §7.2.6 5 顆 critical regression test。八 PR 完成 = AS-wide single-knob 全 wire = catastrophic rollback runbook（§13）可實際執行。

---

## 15. Cross-references

- **AS.0.1 inventory**：`docs/security/as_0_1_auth_surface_inventory.md` §4.5 (10-item bypass list — 本 row §3.1 noop 矩陣對齊 source) + §6 (`OMNISIGHT_AUTH_MODE` 與 `OMNISIGHT_AS_ENABLED` 不衝突的 §1.2 出處) + §7 AS.0.8 row（「TBD：env wiring location」是本 row §2.1 設計回應）。
- **AS.0.2 alembic 0056**：`backend/alembic/versions/0056_tenants_auth_features.py` — `auth_features` JSONB column 是 §4.3 雙層 gate precedence 的 schema source；本 row §3.3 schema decoupling invariant 釘住 column 在 knob false 時保留。
- **AS.0.3 users.auth_methods**：`backend/alembic/versions/0058_users_auth_methods.py` — knob false 時不刪 row（per §3.3）；既有 oauth_<provider> tag 保留供 re-enable 後續用。
- **AS.0.4 credential refactor**：`docs/security/as_0_4_credential_refactor_migration_plan.md` §4.2 — 「knob false ≠ alembic downgrade」是本 row §3.3 invariant 的 sibling source；本 row 統一條文。
- **AS.0.5 Turnstile fail-open**：`docs/security/as_0_5_turnstile_fail_open_phased_strategy.md` §7.2 (「knob false 與 phase advance state 解耦」是本 row §3.1 / §6.1 的 sibling source) + §4 (5 層 precedence 是本 row §4.1 的 既有 source)。
- **AS.0.6 automation bypass list**：`docs/security/as_0_6_automation_bypass_list.md` §11 (「knob false 時三 axis 全 noop」是本 row §3.1 的 sibling source) + §9 (「knob false 時三 mechanism 皆 noop」表格 row 是本 row §3.1 對齊 source)。
- **AS.0.7 honeypot field 設計**：`docs/security/as_0_7_honeypot_field_design.md` §9 / §10 (「knob false 時 honeypot module noop、不查 auth_features.honeypot_active」是本 row §3.1 的 sibling source) + §3.5 (audit row schema 是本 row §5 audit 行為矩陣的 既有 source)。
- **AS.0.9 compat regression test suite**：TODO § AS.0.9 5 顆 critical test (本 row §7.1 acceptance criteria + §7.2.6 drift guard 是 AS.0.9 第 5 顆 critical test 的 contract source)。
- **設計 doc § 5 / § 10 (R34) / § AS.0.8**：`docs/design/as-auth-security-shared-library.md` line 154 (`export OMNISIGHT_AS_ENABLED=false` 是本 row §13 runbook 的 既有 source) + R34 risk register。
- **W11-W16 roadmap**：`docs/design/w11-w16-as-fs-sc-roadmap.md` line 125 / line 278 (「每 priority 各自有 env knob 可全套 disable」是本 row 設計初衷的 既有 source；本 row 是 AS priority 該 env knob 的 design freeze)。
- **`backend/config.py::Settings`**：本 row §2.1 / §7.2.1 規範 `as_enabled: bool = True` field 必加；既有 Settings field pattern (`gerrit_enabled` / `release_enabled` / `docker_enabled`) 是 type / default / 命名一致性 source。
- **`backend/auth.py:69-73` + `backend/config.py:770-792`**：既有 `OMNISIGHT_AUTH_MODE` 三值 enum、`ENV=production` + `AUTH_MODE=open` exit 78 hard error — 是本 row §1.2 解釋「why not reuse AUTH_MODE」的 既有 source。
- **`docs/sop/implement_phase_step.md` lines 136-216**：G4 production-readiness gate (AS.3.1 / AS.4.1 / AS.1 / AS.2 / AS.6.x 落地 PR 必過此 gate)。
- **`docs/ops/db_failover.md`**：disaster recovery 既有 runbook — 本 row §13.4 邊界釘住 AS knob 不替代 DB failover；ops runbook (AS.8.3) 必 cross-link 兩者。

---

**End of AS.0.8 plan**. 下一步 → AS.0.9 Compat regression test suite（5 顆 critical：既有 password / 既有 password+MFA / API key / test token bypass / rollback knob）— 把本 row §7.1 acceptance criteria + §7.2.6 drift guard 翻成 `backend/tests/test_as_compat_regression.py` 的具體 test fixture。
