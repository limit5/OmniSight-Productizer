# AS.0.5 — Turnstile Fail-Open Phased Strategy

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Scope**: 釘住 Cloudflare Turnstile（與其備援 reCAPTCHA / hCaptcha，AS.3.5 fallback chain）在 OmniSight 自家 login / signup / password-reset / contact form 上線後 **三個 phase 的 fail-open → alert → tenant opt-in fail-closed 漸進路徑**——每個 phase 的 trigger、行為、observable acceptance criteria、rollback path、與 AS.0.2 `auth_features.turnstile_required` / AS.0.6 automation bypass list / AS.0.8 `OMNISIGHT_AS_ENABLED` single-knob 三條 gating axis 的 precedence。
>
> **目標讀者**：(1) 寫 AS.3.1 `backend/security/bot_challenge.py` + AS.3.4 server-side score verification 的人——本文件規範 fail mode 行為（pass/fail/unverified/jsfail/bypass 五分類）+ audit event canonical names。(2) 寫 AS.6.3 OmniSight self login/signup/password-reset/contact Turnstile backend verify 的人——本文件規範 caller wiring 與 phase advance gate。(3) 寫 AS.5.1 auth event format / AS.5.2 per-tenant dashboard 的人——本文件釘 dashboard 上 phase advance 的觀察條件 + alert threshold。(4) 寫 AS.8.3 `docs/operations/as-rollout-and-rollback.md` Turnstile 三階段切換 SOP 的人——本文件是 runbook 的 design source。
>
> **不在本 row 範圍**：實際 bot challenge module 程式碼（AS.3.1）、server-side score verification 實作（AS.3.4）、provider fallback 切換邏輯（AS.3.5）、frontend Turnstile widget（AS.7.1 / AS.7.2 / AS.7.3）、audit/dashboard schema 落地（AS.5.x）、operator runbook 文字（AS.8.3）。本 row 是 **plan-only design freeze**，下游 PR 必須遵守此處釘的 phase semantics 與 advance gate。

---

## 1. 為什麼必須是 fail-open 漸進、不能直接 fail-closed

### 1.1 既有 production user / automation surface 的 blast radius

`AS.0.1` 盤點顯示既有 production OmniSight 的 auth surface 與自動化 client：

- **既有 password user 主流程** (`/api/v1/auth/login`、`/api/v1/auth/mfa/challenge`、`/api/v1/auth/change-password`) — 完全沒有 CAPTCHA；目前 throttle 走 `backend/auth.py::_login_throttle` 的 in-memory rate-limit + 單機 IP/email 雙鍵 + per-account lockout（AS.0.1 §1.1 / §5）。在 Turnstile 上線當下強制 fail-closed 會把**既有 user 鎖在門外**——他們的 browser 沒先 load Turnstile widget、Cookie 沒 challenge token、API call 立即 401，毫無 grace。
- **Bootstrap wizard** (`/api/v1/bootstrap/admin-password`、`/api/v1/bootstrap/init-tenant`) — 屬於 fresh-install / dr-drill 路徑，AS.0.1 §4.5 第 7 條已釘為 Turnstile-bypass 必經 path；但若 fail-closed semantics 寫錯（例：caller 沒在 bypass list、Turnstile JS 因 Cloudflare 邊緣節點故障未 load），會打到 first-boot scenarios。
- **Automation script bearer caller** (`scripts/prod_smoke_test.py`、`scripts/usage_report.py`、`scripts/check_fallback_freshness.py`、`scripts/bootstrap_prod.sh` 等，AS.0.1 §4.1) — 全走 `Authorization: Bearer` header，**沒有 browser context**；Turnstile widget 是 browser-only DOM widget、自動化 client 根本拿不到 token。bypass list（AS.0.6）必須在 fail-closed phase 之前 100% 確認所有 caller 已歸納，漏一條 = 自動化 break。
- **Cloudflare 在大陸地區邊緣節點不穩**（per design doc §10 Q2）— `challenges.cloudflare.com/turnstile/v0/api.js` 在大陸客戶側可能因 ISP / GFW 不可達；fail-closed phase 對該地區 user 等同**全面服務拒絕**，不是「bot 被擋」是「人被擋」。fallback chain（AS.3.5: Turnstile → reCAPTCHA → hCaptcha）需要**有觀察窗驗過**才能切 fail-closed。

### 1.2 為什麼不採「先試 fail-closed、出事就 hotfix」

- **Hot-fix 路徑要 redeploy backend image**（AS.0.8 single-knob 是 env var、env var 要重啟兩個 worker container），最快 ~5 min；這 5 min 內既有 user 完全登不進去。
- 既有 password user 沒有第二條 fallback path（OAuth 還沒 GA、AS.6.1 才會 wire），login 是唯一入口。
- **Audit gap 問題**：直接 fail-closed 沒有「逐步觀察 challenge pass rate / unverified rate / jsfail rate」三個 metrics 的累積資料，事後無法解釋「為什麼此 tenant 失敗率 30%」是 bot 還是真 user。

### 1.3 漸進的本質

`fail-open + warning log → alert → tenant opt-in fail-closed` 把「Turnstile 上線」從「single-axis hard cutover」拆成 **observability-first staged rollout**：每階段都「不阻擋 user」、每階段都「累積比 prev phase 多一層 evidence」、每次 advance 都「需要 explicit 條件達成 + operator ack」。這跟 AS.0.4 expand-migrate-contract 的精神一致——任一 phase 失敗都可以**安全 rollback 到 prev phase**、不會出現「砍舊回不去」的 contract-phase 點。

---

## 2. 三個 phase 的精確定義

### 2.1 Phase 0 — Pre-rollout（AS.3 / AS.6.3 落地之前）

**狀態**：Turnstile 完全不存在 production code path。AS 尚未 ship 任一 row。

- `tenants.auth_features.turnstile_required` 既有 tenant 預設 `false`（AS.0.2 已 land），新 tenant 預設 `true`。
- 既有 login flow 走 `backend/auth.py::_login_throttle` 純 rate-limit，無 challenge。
- 沒有 audit event `bot_challenge.*`。

**離開 Phase 0 的 trigger**：AS.3.1 `backend/security/bot_challenge.py` + AS.3.4 server-side verify + AS.6.3 OmniSight self login/signup/password-reset/contact 接 Turnstile backend verify 全部 ship 進 main、deployed-inactive。**Phase 0 → Phase 1 由 deploy 行為觸發、不靠 env knob**——code 就位即進 Phase 1（fail-open 預設）。

### 2.2 Phase 1 — Fail-open + warning log（最少 4 週）

**行為矩陣**：

| Caller scenario | Turnstile 結果 | Phase 1 行為 | Audit event |
|---|---|---|---|
| Browser user, widget load OK, score ≥ 0.5 | `pass` | login 繼續、無 friction | `bot_challenge.pass` |
| Browser user, widget load OK, score < 0.5 | `fail` (low-score) | login 繼續（fail-open），標 unverified | `bot_challenge.unverified_lowscore` |
| Browser user, widget load OK, server verify return error | `fail` (server error) | login 繼續（fail-open），標 unverified | `bot_challenge.unverified_servererr` |
| Browser user, widget JS load failed (CDN block / network) | `n/a` (no token) | **AS.3.5 fallback chain**：先試 reCAPTCHA、再試 hCaptcha；全失敗 → fallback 到 AS.4 honeypot + slow rate-limit | `bot_challenge.jsfail_fallback_*` |
| API key bearer caller (AS.0.1 §4.5 #1-#3) | `n/a` (no widget) | **bypass**（不檢查），login 繼續 | `bot_challenge.bypass_apikey` |
| Webhook / chatops signature caller (AS.0.1 §4.5 #4-#5) | `n/a` | **bypass**（path-allowlisted） | `bot_challenge.bypass_webhook` |
| Bootstrap wizard / probe path (AS.0.1 §4.5 #6-#7) | `n/a` | **bypass**（path-allowlisted） | `bot_challenge.bypass_bootstrap` |
| `OMNISIGHT_AUTH_MODE=open` (dev/test) | n/a | bypass + audit nothing（dev mode) | (none) |
| `OMNISIGHT_AS_ENABLED=false` (AS.0.8 single-knob) | n/a | **AS 全套 disabled**，pre-AS rate-limit-only 行為 | (none — AS gating 不掛) |

**Warning log 約束**（給 AS.5.1 auth event format / AS.3.1 emitter 必遵守）：

- 任何 `bot_challenge.unverified_*` audit row 必含 `severity=warn`、`metadata.score`（若有）、`metadata.provider`（turnstile/recaptcha/hcaptcha）、`metadata.fail_mode`（lowscore / servererr / jsfail / network_timeout）。
- 任何 `bot_challenge.bypass_*` audit row 必含 `severity=info`、`metadata.bypass_reason`（apikey / webhook / chatops / bootstrap / probe / authmode_open）。
- 任何 `bot_challenge.pass` audit row 必含 `severity=info`、`metadata.score`、`metadata.provider`。
- 後端 application log 同時 emit 一行 `WARNING bot_challenge unverified user=<user_id_or_email_hash> tenant=<tid> mode=<fail_mode> score=<score>`，給 ops grep 用。

**離開 Phase 1 的 trigger**（advance 到 Phase 2 的 acceptance gate）：

1. **時間下限 ≥ 28 天連續觀察**——抓週期性 traffic（週末 / 假日 / 月結 cron）。
2. **Audit event volume ≥ 一個合理 baseline**（per-tenant ≥ 100 `bot_challenge.*` row 才有統計意義；少於 100 row 的 tenant 留 Phase 1）。
3. **`bot_challenge.unverified_*` rate < 5%**（per-tenant 7-day rolling）——若高於 5% 必須先排查是真 bot（rate-limit 收緊）還是 widget 配置錯（Cloudflare site key / hostname / Pages config）。
4. **`bot_challenge.jsfail_fallback_*` rate < 3%**（per-tenant 7-day rolling）——若高於 3% 必須先驗 fallback chain（AS.3.5）切換邏輯。
5. **No `bot_challenge.bypass_*` audit miss**——bypass list（AS.0.6）必須對齊 production traffic 看到的 caller 種類；任一個自動化 client 落在 `unverified_*` 而非 `bypass_*` = bypass list 漏條目，必須先補 AS.0.6 再 advance。
6. **Operator 顯式 ack**（在 AS.5.2 dashboard 上點「Advance to Phase 2」，audit `phase_advance.p1_to_p2` 落 row）。

任一條件不滿足 → 留 Phase 1，不 advance。

### 2.3 Phase 2 — Fail-open + per-tenant alert（最少 4 週）

**行為矩陣**：與 Phase 1 完全相同（fail-open 行為不變、challenge 結果不阻擋 login）。

**新增的 observability 行為**：

- AS.5.2 per-tenant dashboard 出 widget「Last 7 days unverified login: X 次（占 Y%）」；超過 tenant-configurable threshold（預設 5%）時 dashboard 紅 highlight。
- 每週一 09:00 UTC cron job `bot_challenge_weekly_alert.py`（**新 owner row：AS.5.2 / 或獨立 hot-fix row**），對 tenant admin 發 email 報告：「過去 7 天有 X 次 unverified login（占 Y%），其中 Z 次來自同一 IP；建議切 fail-closed 嗎？」
- Email 報告包含 sample audit row（最多 10 條，匿名化 user_id → email_hash + ip → /24 prefix）給 admin 自行判斷。
- 報告**不**自動把 tenant flip `turnstile_required=true`——advance 到 Phase 3 必走 tenant 主動操作。

**Alert threshold（per-tenant 可調，預設值）**：

| 指標 | 預設 threshold | 行為 |
|---|---|---|
| `unverified_*` rate (7-day) | ≥ 5% | dashboard 紅 highlight + email 週報突顯 |
| `unverified_*` 同 IP 頻次 (24h) | ≥ 50 / IP | email 即時通知 admin（可選 opt-out） |
| `jsfail_fallback_*` rate (7-day) | ≥ 3% | dashboard 黃 highlight（可能是 provider 配置 / 區域可達性問題） |
| `pass` rate sudden drop (day-over-day) | ≥ 30% drop | dashboard 黃 highlight（可能是 site key 失效 / Cloudflare 全球事件） |

**離開 Phase 2 的 trigger**（advance 到 Phase 3 = tenant opt-in fail-closed 才會觸發）：

- **Phase 2 不會「全 tenant」一起 advance**——advance 是 **per-tenant opt-in**，由 tenant admin 在 settings UI 點「Enable strict bot challenge」。
- 每個 tenant 自己的 advance gate：(1) Phase 2 觀察 ≥ 28 天 + (2) tenant admin 顯式同意 + (3) 該 tenant `unverified_*` rate ≤ 1%（避免 admin 在高 false-positive 期切 fail-closed 把自己鎖出去）+ (4) audit `phase_advance.p2_to_p3.tenant_<tid>` 落 row。
- **既有 production tenant 預設永遠 stay Phase 2**（除非 admin 主動 opt-in），與 design doc §3.5 一致。
- **新建 tenant 預設**：AS.0.2 alembic 0056 `auth_features.turnstile_required=true` 是「新 tenant 全開」的設計；新 tenant 一進系統就 Phase 3，但仍走 AS.3.5 fallback chain 與 jsfail honeypot fallback、不直接 401 鎖死 user（Phase 3 fail-closed semantics 見 §2.4）。

### 2.4 Phase 3 — Per-tenant fail-closed（tenant opt-in only）

**行為矩陣**（per tenant `auth_features.turnstile_required=true` 才生效）：

| Caller scenario | Turnstile 結果 | Phase 3 行為 | Audit event |
|---|---|---|---|
| Browser user, widget OK, score ≥ 0.5 | pass | login 繼續 | `bot_challenge.pass` |
| Browser user, widget OK, score < 0.5 | fail | **HTTP 429 `bot_challenge_failed`**，UI 顯示「Try again or contact admin」 | `bot_challenge.blocked_lowscore` |
| Browser user, widget OK, server verify error | fail | **不 fail-closed**——server-side verify error 屬 our-side 故障、與 user 無關，仍 fail-open + audit warn | `bot_challenge.unverified_servererr` |
| Browser user, widget JS load failed | n/a | **AS.3.5 fallback chain → AS.4 honeypot + slow rate-limit + 5-second min response delay**；honeypot 中 → block；honeypot 過 → login 繼續（fail-open for jsfail）| `bot_challenge.jsfail_honeypot_<pass_or_fail>` |
| API key / webhook / probe / bootstrap (bypass list) | n/a | **bypass**（與 Phase 1/2 完全相同） | `bot_challenge.bypass_*` |
| `OMNISIGHT_AUTH_MODE=open` | n/a | bypass | (none) |
| `OMNISIGHT_AS_ENABLED=false` (AS.0.8) | n/a | **AS 全套 disabled，pre-AS 行為** | (none) |

**Phase 3 的「fail-closed」嚴格只對「browser-context user 且 widget 成功 load 且 server verify 成功 return 而 score 低」這唯一情境 401**——server error / jsfail 仍走 fail-open，因為這兩種 mode 屬「our-side 不確定性」，不是 confirmed bot signal。這是 design doc §3.5「JS 載入失敗 → 自動 fallback 到 honeypot + 慢速 rate limit（不 fail-closed 鎖 user）」的精確化。

**Per-tenant rollback Phase 3 → Phase 2**：tenant admin 在 settings UI flip `turnstile_required=false`，立即生效（next request）；audit `phase_revert.p3_to_p2.tenant_<tid>` 落 row。**Operator-side global Phase 3 → Phase 2**：`OMNISIGHT_AS_ENABLED=false`（AS.0.8 single-knob）一鍵全 disable，所有 tenant 行為即時退到「pre-AS」基線。

### 2.5 三 phase 一覽表

| Axis | Phase 0 | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|---|
| Code path | Turnstile 不存在 | AS.3 + AS.6.3 deployed-inactive 或 active | 同 Phase 1 | 同 Phase 1，但 `tenants.auth_features.turnstile_required=true` 走 fail-closed branch |
| Default `turnstile_required` (existing tenant) | n/a | false | false | false（除非 admin opt-in） |
| Default `turnstile_required` (new tenant) | n/a | true（schema default）但行為仍 fail-open | 同 | true → 行為 fail-closed（only for confirmed low-score） |
| Login on `unverified` (low-score / server-error) | n/a | continue + warn | continue + warn + alert | low-score → 401; server-error → continue + warn |
| Login on `jsfail` | n/a | continue + AS.3.5 fallback | 同 | AS.3.5 fallback + AS.4 honeypot + 5s delay |
| Audit event minimum granularity | none | bot_challenge.{pass,unverified_*,bypass_*,jsfail_*} | + phase_advance.* | + phase_advance.*, phase_revert.* |
| Alert path | none | log only | log + dashboard + weekly email | log + dashboard + weekly email + per-tenant config |
| Advance trigger | code deploy | 28d + 5%/3% guards + bypass-coverage + ops ack | per-tenant opt-in（與 §2.3 4 條件） | n/a — terminal phase |
| Rollback path | n/a | redeploy backend without AS code | flip `OMNISIGHT_AS_ENABLED=false`（global）| flip `turnstile_required=false`（per-tenant）OR `OMNISIGHT_AS_ENABLED=false`（global） |

---

## 3. Audit event canonical names（給 AS.5.1 / AS.3.1 釘）

仿 `backend/audit_events.py` 的 `domain.verb` 命名（dot-separated）。**所有名稱在本 plan 釘死、AS.3.1 module 必須用同一 set 的常數、不可自行造名**：

```python
# AS.3.1 backend/security/bot_challenge.py 必須匯出（plan §3 釘）
EVENT_BOT_CHALLENGE_PASS = "bot_challenge.pass"
EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE = "bot_challenge.unverified_lowscore"
EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR = "bot_challenge.unverified_servererr"
EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE = "bot_challenge.blocked_lowscore"  # Phase 3 only
EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA = "bot_challenge.jsfail_fallback_recaptcha"
EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA = "bot_challenge.jsfail_fallback_hcaptcha"
EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS = "bot_challenge.jsfail_honeypot_pass"
EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL = "bot_challenge.jsfail_honeypot_fail"
EVENT_BOT_CHALLENGE_BYPASS_APIKEY = "bot_challenge.bypass_apikey"
EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK = "bot_challenge.bypass_webhook"
EVENT_BOT_CHALLENGE_BYPASS_CHATOPS = "bot_challenge.bypass_chatops"
EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP = "bot_challenge.bypass_bootstrap"
EVENT_BOT_CHALLENGE_BYPASS_PROBE = "bot_challenge.bypass_probe"

# Phase advance / revert（owner: AS.5.2 dashboard）
EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2 = "bot_challenge.phase_advance_p1_to_p2"
EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3 = "bot_challenge.phase_advance_p2_to_p3"
EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2 = "bot_challenge.phase_revert_p3_to_p2"
EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1 = "bot_challenge.phase_revert_p2_to_p1"
```

**Schema 約束**（每個 event row 的 `metadata` JSONB 必含 fields）：

| Event | `metadata` 必含 |
|---|---|
| `pass` / `unverified_lowscore` / `blocked_lowscore` | `provider`, `score` (float), `widget_action`（login / signup / pwreset / contact）|
| `unverified_servererr` | `provider`, `error_kind`（timeout / 5xx / 4xx_invalid_token / dns_fail）, `widget_action` |
| `jsfail_fallback_*` | `original_provider`, `fallback_provider`, `widget_action`, `fallback_score`（若有） |
| `jsfail_honeypot_*` | `widget_action`, `honeypot_field_name`, `delay_ms`（5000 default） |
| `bypass_*` | `bypass_reason`（caller_kind 細分）, `widget_action` |
| `phase_advance_*` / `phase_revert_*` | `actor_user_id`, `tenant_id`, `previous_phase`, `next_phase`, `reason`（free text） |

**Drift guard test pattern**（AS.3.1 PR land 時必同 PR 加）：

```python
def test_bot_challenge_event_names_canonical():
    """AS.0.5 §3 invariant: 所有 bot_challenge.* event name 必走常數匯出，禁 inline 字串."""
    import pathlib, re
    src_files = ["backend/security/bot_challenge.py", "backend/routers/auth.py"]
    inline_pattern = re.compile(r'["\']bot_challenge\.\w+["\']')
    for path in src_files:
        text = pathlib.Path(path).read_text()
        # 只允許在 EVENT_* 常數定義行出現 inline string
        for line in text.splitlines():
            if 'EVENT_BOT_CHALLENGE_' in line and '=' in line:
                continue
            assert not inline_pattern.search(line), (
                f"{path}: bot_challenge.* event name 必須走常數匯出（line: {line.strip()})"
            )
```

---

## 4. Bypass list 與 Phase gating 的 precedence

**三層 gating axis 的 precedence 順序（最高優先到最低）**：

1. **`OMNISIGHT_AUTH_MODE=open`**（dev/test）→ 永遠 bypass，不寫任何 audit、不走任何 phase 邏輯。
2. **`OMNISIGHT_AS_ENABLED=false`**（AS.0.8 single-knob global rollback）→ 整 AS 套件 noop、bot_challenge module 直接 return `pass` 無 verify、不寫 audit。
3. **AS.0.6 bypass list path / caller match**（API key / webhook / chatops / bootstrap / probe path）→ bypass + 寫 `bot_challenge.bypass_*` audit row（與 phase 無關）。
4. **`tenants.auth_features.turnstile_required`**（Phase 3 per-tenant gate）→ true → fail-closed branch（only for low-score）；false → fail-open branch。
5. **Phase 1/2/3 共用 verify 路徑**（widget verify + score check + fallback chain + honeypot fallback）。

**Audit semantic 的差異**：

| Bypass kind | Audit row 寫嗎 | Phase advance 計入嗎 |
|---|---|---|
| `OMNISIGHT_AUTH_MODE=open` | 否 | 否 |
| `OMNISIGHT_AS_ENABLED=false` | 否 | 否（pre-AS baseline，無 phase concept） |
| AS.0.6 caller-kind bypass | 是（`bot_challenge.bypass_*`） | 否（bypass row 不算 unverified_rate denominator） |

**Phase advance metric denominator**（AS.5.2 dashboard 公式）：

```
unverified_rate = COUNT(bot_challenge.unverified_*) / (
    COUNT(bot_challenge.pass) + COUNT(bot_challenge.unverified_*) + COUNT(bot_challenge.blocked_lowscore)
)
# bypass_* 與 jsfail_* 不算分母（bypass 是 by-design、jsfail 是 widget 故障）
jsfail_rate = COUNT(bot_challenge.jsfail_*) / (
    COUNT(bot_challenge.pass) + COUNT(bot_challenge.unverified_*) + COUNT(bot_challenge.blocked_lowscore) + COUNT(bot_challenge.jsfail_*)
)
```

---

## 5. Provider fallback chain（AS.3.5）的 Phase 互動

設計 doc §3.5 已規劃 `Turnstile → reCAPTCHA → hCaptcha`。本 plan 釘住該 chain 在三個 phase 的行為差異：

| Phase | Primary failure → fallback 觸發 trigger | Fallback failure → next behaviour |
|---|---|---|
| Phase 1 | widget JS load fail (timeout 5s / DNS fail / 4xx) → 切 reCAPTCHA | reCAPTCHA 也 fail → 切 hCaptcha；hCaptcha 也 fail → AS.4 honeypot + slow rate-limit；honeypot 過 → continue + audit `unverified_jsfail` |
| Phase 2 | 同 Phase 1 + dashboard 黃 highlight + email weekly 報告突顯「該 tenant fallback rate 高」 | 同 Phase 1 |
| Phase 3 | 同 Phase 1 + dashboard 黃 highlight | hCaptcha 也 fail → AS.4 honeypot + 5s 延遲 + slow rate-limit；honeypot 過 → continue（**not fail-closed** — 因為 widget 故障非 user 過錯）；honeypot 失敗 → block |

**Provider 切換的 audit 軌跡**（給 ops 排查用）：

每次 fallback 必寫一條 `bot_challenge.jsfail_fallback_<next_provider>` 帶 `metadata.original_provider` + `metadata.original_failure_kind`，方便 grep 出「過去 24h 有多少 user 是因為 Cloudflare 邊緣節點故障被 fallback」vs「reCAPTCHA 全球 quota 滿了被 fallback」。

**禁止的反 pattern**：

- ❌ Fallback chain 之間「平行試三個 provider 取最快回應」——會放大流量、且讓 `score` 來自不同 provider 的不同 calibration（Turnstile 0-1 與 reCAPTCHA v3 0-1 與 hCaptcha 0-1 的「0.5」意義不同）。必走 sequential、failure-driven。
- ❌ 同 user 同 session 多次 fallback——一次 session 內 fallback 一次後 sticky 該 provider 直到 session 結束（per-tab `sessionStorage.setItem("bot_challenge_provider", X)`），避免 user 連續看到三個不同 captcha 困惑。
- ❌ Server-side verify endpoint 在 fallback 時切換 site secret——per provider 獨立 site secret env (`OMNISIGHT_TURNSTILE_SECRET` / `OMNISIGHT_RECAPTCHA_SECRET` / `OMNISIGHT_HCAPTCHA_SECRET`)、不共用、不熱切，避免 caching 衝突。

---

## 6. Acceptance criteria per phase（給 deploy gate 用）

### 6.1 Phase 0 → Phase 1 deploy 條件

- [ ] AS.3.1 `backend/security/bot_challenge.py` 過 lint + ≥ 80% line coverage（含 unit test for fail-open default）
- [ ] AS.3.4 server-side verify 對三 provider（Turnstile / reCAPTCHA / hCaptcha）有 contract test（mocked 200 / 400 / 5xx response）
- [ ] AS.6.3 OmniSight self login/signup/password-reset/contact form 全 wire 完成、grep 4 處 caller 確認都呼叫 `bot_challenge.verify()`
- [ ] AS.0.6 bypass list（AS.3.1 內建 `_BYPASS_PATH_PREFIXES` + `_BYPASS_CALLER_KINDS`）對齊 AS.0.1 §4.5 十項
- [ ] Audit event 13 個常數已 export、drift guard test 綠
- [ ] HANDOFF.md AS.3 / AS.6.3 row `Production status: deployed-inactive`、`Next gate: deployed-active when 第一個 tenant 切 auth_features.turnstile_required=true 觸發 widget 載入觀察`

### 6.2 Phase 1 → Phase 2 advance 條件

- [ ] 連續觀察 ≥ 28 天（calendar days，跨至少一個月底/週末/假日）
- [ ] Per-tenant audit event volume ≥ 100 row（少於 100 row 的 tenant 留 Phase 1）
- [ ] `unverified_rate` 7-day rolling < 5%（per tenant，符合條件 tenant 才 advance）
- [ ] `jsfail_rate` 7-day rolling < 3%（per tenant）
- [ ] 所有 production 自動化 caller 落在 `bypass_*`、無一個落 `unverified_*`（grep `bot_challenge.unverified_*` audit 看 caller_kind 分布）
- [ ] AS.5.2 dashboard widget 已 ship、operator 能看到 per-tenant unverified rate
- [ ] Operator 顯式 ack（dashboard「Advance to Phase 2」按鈕，audit `phase_advance.p1_to_p2` 落 row）
- [ ] HANDOFF.md `Production status: deployed-active`、`Next gate: per-tenant Phase 3 opt-in once dashboard alert ship 與 alert 週報 cron 跑滿一週`

### 6.3 Phase 2 → Phase 3 advance 條件（per tenant）

- [ ] Phase 2 觀察該 tenant ≥ 28 天（Phase 1 + Phase 2 累計 ≥ 56 天）
- [ ] 該 tenant `unverified_rate` 7-day rolling ≤ 1%（更嚴）
- [ ] 該 tenant 至少收到 4 次 weekly alert email、admin 已讀
- [ ] Tenant admin 在 settings UI flip `auth_features.turnstile_required=true`、audit `phase_advance.p2_to_p3.tenant_<tid>` 落 row
- [ ] HANDOFF.md 不 per-tenant 寫 entry——AS.5.2 dashboard widget 取代 manual record；本 plan 文件是 per-tenant advance 唯一 SOP source

---

## 7. Rollback 策略

### 7.1 Per-phase rollback path

| Phase 走進去 → 回退 | Trigger | 動作 | 影響範圍 |
|---|---|---|---|
| Phase 1 → Phase 0 | AS.3 / AS.6.3 critical bug | redeploy backend image without AS code，或 git revert AS.3 / AS.6.3 commits | 所有 tenant + 所有 user，但因 fail-open 行為差別僅是「audit row 不寫」 |
| Phase 2 → Phase 1 | Alert 系統故障 / 過量誤報擾民 | 停 weekly cron、移除 dashboard widget、保留 audit emit | dashboard / email 行為退、login 行為不變 |
| Phase 3 → Phase 2（per tenant） | Tenant admin 反應「user 被擋」 | flip `turnstile_required=false`、audit `phase_revert.p3_to_p2.tenant_<tid>` | 該 tenant only，行為退 fail-open |
| Phase 3 → Phase 0（global） | Catastrophic — Turnstile widget 全球故障 | `OMNISIGHT_AS_ENABLED=false` + restart | 所有 tenant 即時退 pre-AS 行為 |

### 7.2 與 AS.0.8 single-knob (`OMNISIGHT_AS_ENABLED`) 的解耦

`OMNISIGHT_AS_ENABLED=false` 與 phase advance state 彼此**獨立**：

- env knob false 期間：bot_challenge module 整個 short-circuit `return BotChallengeResult.passthrough()`、不寫 audit、不查 `auth_features.turnstile_required`；但 DB 的 `auth_features.turnstile_required` 值不變（保留 tenant 的 opt-in 狀態）。
- env knob 切回 true：phase 行為恢復到 knob 切 false **之前**的 state（per-tenant `turnstile_required` 還在），不需要重新 advance。
- env knob false 持續期間 dashboard widget 必顯示 banner「AS globally disabled — phase metrics paused」，避免 admin 誤以為 unverified rate = 0 是好事。

### 7.3 與 `tenants.auth_features.turnstile_required` 的解耦

flip `turnstile_required` 是 **per-tenant runtime gate**、與 phase state 同樣解耦於 env knob：

- Phase 1 期間 admin flip `true` → 仍走 fail-open（Phase 1 的 schema-default 是 schema-level only、runtime check 走 phase state）。
- Phase 3 期間 admin flip `false` → 該 tenant 退 fail-open，其他 tenant 不影響。
- 這套解耦讓「全 tenant 共同的 phase advance gate」與「個別 tenant 的 fail-closed opt-in」可以獨立 evolve。

---

## 8. Drift guards（給未來 PR 必須維護的對齊關係）

### 8.1 Bypass list 對齊 guard

`backend/security/bot_challenge.py` 的 `_BYPASS_PATH_PREFIXES` 與 `_BYPASS_CALLER_KINDS` 必對齊 AS.0.1 §4.5 十項：

```python
def test_as_0_5_bypass_list_aligned_with_inventory():
    """AS.0.5 §4 invariant: bot_challenge bypass list 必對齊 AS.0.1 §4.5 inventory."""
    from backend.security.bot_challenge import _BYPASS_PATH_PREFIXES, _BYPASS_CALLER_KINDS
    expected_paths = {
        "/api/v1/livez", "/api/v1/readyz", "/api/v1/healthz",
        "/api/v1/bootstrap/", "/api/v1/webhooks/", "/api/v1/chatops/webhook/",
        "/api/v1/auth/oidc/", "/api/v1/auth/mfa/challenge",
        "/api/v1/auth/mfa/webauthn/challenge/",
    }
    expected_kinds = {"apikey_omni", "apikey_legacy", "metrics_token"}
    assert expected_paths.issubset(_BYPASS_PATH_PREFIXES), (
        f"AS.0.5 §4 / AS.0.1 §4.5 bypass list drift: missing {expected_paths - _BYPASS_PATH_PREFIXES}"
    )
    assert expected_kinds.issubset(_BYPASS_CALLER_KINDS), (
        f"AS.0.5 §4 / AS.0.6 caller kinds drift: missing {expected_kinds - _BYPASS_CALLER_KINDS}"
    )
```

**注意**：`/api/v1/auth/login` 本身**不在 bypass path**——Turnstile 的主要保護目標是 login。fail-open 在 Phase 1/2 的「不阻擋 login」行為是 verify 後的 *outcome* 決定的（continue + warn），不是 bypass。

### 8.2 Audit event name 常數匯出 guard

見 §3 末尾的 `test_bot_challenge_event_names_canonical()` pattern。

### 8.3 Provider site secret env wiring guard

```python
def test_as_0_5_provider_site_secret_envs_distinct():
    """AS.0.5 §5 invariant: 三 provider site secret 各自獨立 env，禁共用 / 禁熱切."""
    import os
    from backend.security.bot_challenge import _PROVIDER_SECRET_ENVS
    expected = {
        "turnstile": "OMNISIGHT_TURNSTILE_SECRET",
        "recaptcha": "OMNISIGHT_RECAPTCHA_SECRET",
        "hcaptcha": "OMNISIGHT_HCAPTCHA_SECRET",
    }
    assert _PROVIDER_SECRET_ENVS == expected, (
        f"AS.0.5 §5 provider env drift: {_PROVIDER_SECRET_ENVS}"
    )
```

### 8.4 Phase advance audit row 不可手動造假 guard

dashboard 的「Advance to Phase 2」按鈕 handler 必走 `bot_challenge.emit_phase_advance(prev=1, next=2, actor=admin)` helper、handler 自身不可直接 `audit.log(action="phase_advance.p1_to_p2", ...)`。helper 內負責：(1) 驗 actor 有 `require_super_admin` 權限、(2) 驗 prev/next 是合法 transition（不可跳級）、(3) 驗 §6.2 / §6.3 條件當下滿足（拉 7-day rolling metric 確認 <5%）。

```python
def test_as_0_5_phase_advance_must_use_helper():
    """AS.0.5 §3 invariant: phase_advance audit row 禁直接 audit.log()，必走 helper."""
    import pathlib, re
    helper_call = re.compile(r'emit_phase_advance\s*\(')
    direct_call = re.compile(r'audit\.log[_sync]*\([^)]*phase_advance')
    text = pathlib.Path("backend/routers/admin_tenants.py").read_text()
    # 任何 phase_advance 路徑必經 helper
    if "phase_advance" in text:
        assert helper_call.search(text), "phase_advance 必走 emit_phase_advance helper"
        assert not direct_call.search(text), "phase_advance 不可直接 audit.log()"
```

---

## 9. 與 AS.0.x / AS.x 其他 row 的互動 / 邊界

| 互動對象 | 邊界 |
|---|---|
| **AS.0.2** `tenants.auth_features.turnstile_required` | column 已存在；本 plan 規範 runtime caller 何時讀（Phase 3）、何時忽略（Phase 1/2）、新 tenant 預設 true（schema） vs 既有 tenant 預設 false（既有 row 凍結）|
| **AS.0.3** `users.auth_methods` + account-linking | 不交集——本 plan 是 bot challenge / phase strategy；account-linking 是 OAuth login 後的 user merge policy|
| **AS.0.4** credential refactor migration plan | 不交集——AS.0.4 鎖 oauth_tokens / 三 router 內 OMNISIGHT_DECISION_BEARER；本 plan 鎖 bot challenge phase|
| **AS.0.6** automation bypass list | **強耦合**——AS.0.6 是 bypass list 的 SoT inventory；本 plan §4 規範 bypass list 在 phase gating 中的 precedence + audit semantic|
| **AS.0.7** honeypot field 設計 | **強耦合**——本 plan §2.3/§2.4 jsfail fallback 終點是 honeypot；AS.0.7 釘 honeypot field 命名 / DOM hide / aria-hidden 細節|
| **AS.0.8** `OMNISIGHT_AS_ENABLED` single-knob | **強耦合**——本 plan §7.2 規範 knob 與 phase state 的解耦關係|
| **AS.0.9** compat regression test suite | 5 顆 critical 必含「Phase 1 fail-open 既有 password user 仍能 login」+「Phase 3 false-positive low-score 仍能透過 fallback chain 進來」|
| **AS.3.1** bot_challenge module | **本 plan 是 AS.3.1 的 spec source**——event name / bypass list / fail mode 矩陣 / phase gating 必對齊|
| **AS.3.4** server-side score verification | 本 plan §3 metadata.score 必填、§5 fallback chain 必走 sequential|
| **AS.3.5** provider fallback chain | 本 plan §5 釘 sequential / sticky / per-provider env 三條 invariant|
| **AS.4** honeypot helper | jsfail fallback 終點；本 plan 不定義 honeypot 細節（AS.0.7 / AS.4.1 負責）|
| **AS.5.1** auth event format | 本 plan §3 是 bot_challenge subset 的 canonical 命名 source|
| **AS.5.2** per-tenant dashboard | 本 plan §2.3 / §6.2 / §6.3 釘 dashboard widget 上要顯示什麼 metric / threshold / advance trigger|
| **AS.6.3** OmniSight self Turnstile backend verify | caller side wiring；本 plan §6.1 釘 4 處 caller 必經 `bot_challenge.verify()` helper|
| **AS.7.1 / 7.2 / 7.3** UI redesign | frontend widget 載入 / fallback UI / Phase 3 「too many failed attempts」UI 樣式；本 plan 不規範樣式|
| **AS.8.3** ops runbook | 本 plan 是 runbook 的 design source; AS.8.3 把 §6 acceptance criteria 翻成 step-by-step ops SOP|

---

## 10. 非目標 / 刻意不做的事

1. **跨 tenant 的「全 tenant 同步 advance」** — Phase 2 → Phase 3 永遠 per-tenant、never global flip。理由：tenant 規模 / user 地理分布 / 自動化 client 比例不同，single global threshold 無意義。
2. **基於「IP reputation feed」的 Phase 3 自動 advance** — 不引入第三方 IP reputation provider，phase advance 必須走 explicit operator/admin ack。理由：IP reputation 假陽率高、會把企業 NAT 後合法 user 誤判 bot。
3. **Phase 3 的 score < 0.5 fail-closed 之外的 stricter mode** — 不支援「score < 0.7 fail-closed」等更嚴 threshold。理由：score calibration 各 provider 不同、threshold 越高 false-positive 越多、與「fail-closed 只擋 confirmed bot」的精神衝突。
4. **Cross-tenant audit aggregation** — 本 plan 不設計「全 tenant unverified rate」聚合 metric，per-tenant 才是 advance unit。AS.5.2 dashboard 可顯示 cross-tenant 概覽僅作 SRE 觀察、不作 advance gate。
5. **Phase 3 → Phase 4「全 tenant 強制 fail-closed」** — 不規劃。Roadmap 終點是「per-tenant opt-in fail-closed」，沒有「強制全 tenant fail-closed」這個 phase。理由：OmniSight 服務的 enterprise tenant 有 SLA 對 false-positive 容忍極低，永遠保留 opt-out 是契約義務。
6. **Auto-revert（Phase 3 → Phase 2 自動回退）based on `unverified_rate` spike** — 不引入。Phase revert 永遠走 explicit operator/admin action（per-tenant flip OR global single-knob）。理由：自動 revert 會造成「半夜 Cloudflare 全球事件 → 自動退 Phase → 隔天清醒不知為何 tenant 退掉」的 ops surprise。
7. **Per-route phase 差異化**（例：login Phase 3 但 signup Phase 1）— 不規劃。Phase state 是 tenant-level 整體屬性、跨 route 共用。AS.6.3 的 4 處 caller（login / signup / pwreset / contact）統一走同 phase。

---

## 11. Production status

* 文件本身：plan-only design freeze。
* 影響的程式碼：本 row 不改 code；AS.3.x / AS.5.x / AS.6.3 follow-up rows 才動。
* Rollback 影響：plan 無 runtime impact、無 rollback。

**Production status: dev-only**
**Next gate**: 不適用 — 本 row 是 design doc。Schedule 由 AS.3.1 (`backend/security/bot_challenge.py`) + AS.3.4 (server-side verify) + AS.6.3 (OmniSight self Turnstile backend wire) PR 觸發 Phase 0 → Phase 1 deploy gate。Phase 1 → Phase 2 advance 由 28 天觀察 + AS.5.2 dashboard ship + operator ack 觸發；Phase 2 → Phase 3 由 per-tenant admin 主動 opt-in 觸發。

---

## 12. Cross-references

- **AS.0.1 inventory**：`docs/security/as_0_1_auth_surface_inventory.md` §1.1 (login throttle baseline) + §4.5 (10-item bypass list — §4 of this plan 對齊 source) + §6 (no test-token bypass clarification)。
- **AS.0.2 alembic 0056**：`backend/alembic/versions/0056_tenants_auth_features.py` — `auth_features.turnstile_required` column 是 §2.4 Phase 3 per-tenant gate runtime check 的 schema source；既有 tenant 預設 false / 新 tenant INSERT 預設 true 是 §2.5 表「Default」一欄的 source。
- **AS.0.3 account-linking**：`docs/security/as_0_3_account_linking.md` — 不直接交集；本 plan §9 表已標明邊界。
- **AS.0.4 credential refactor**：`docs/security/as_0_4_credential_refactor_migration_plan.md` — 不直接交集；§9 表已標明邊界。
- **設計 doc § 3.5 / § 3.6 / § 3.7 / § 3.8 / § 10 R34 / § 10 Q2**：`docs/design/as-auth-security-shared-library.md` — fail-open 漸進 / automation bypass / single-knob rollback / compat regression test / R34 risk register / Cloudflare 大陸區可達性 open question 五處原始 design source。
- **G4 production-readiness gate**：`docs/sop/implement_phase_step.md` lines 136-216；任何 phase advance PR 必過此 gate（image rebuild + env knob wired + at least one live smoke + 24h observation）。
- **Audit event canonical 命名範本**：`backend/audit_events.py` — `domain.verb` 命名規範與 `EVENT_*` 常數匯出 pattern 是 §3 的 SoT。
- **R34 risk**：design doc §10 — Turnstile lock 既有自動化 client → AS.0.5 fail-open 漸進 + AS.0.6 bypass list（本 plan + AS.0.6 共同 mitigate R34）。

---

**End of AS.0.5 plan**. 下一步 → AS.0.6 Automation bypass list（API key auth / IP allowlist / test token header）— 把本 plan §4 的 bypass list 從「對齊 inventory」升級到「per-caller authentication mechanism 完整盤點 + Settings UI 可加 IP allowlist + audit row + 月結報表 spec」。
