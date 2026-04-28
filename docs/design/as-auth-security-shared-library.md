---
audience: internal
---

# ADR — Priority AS: Auth & Security Shared Library

> Status: Accepted 2026-04-27 · Implemented 2026-04-28（AS.0 → AS.7 ✅、AS.8.1 ✅、AS.8.2 本文 ✅、AS.8.3 runbook 待 land）
> Authors: nanakusa sora + Agent-software-beta
> Related: W11-W16/AS/FS/SC Roadmap (`w11-w16-as-fs-sc-roadmap.md`), R20 (chat-layer security), K-rest (CF Access SSO), Phase 5b (LLM credentials), git_credentials (既有 GitHub OAuth)
> As-built audit 章節：見 §12（2026-04-28 加入）。原本 §2 表格的「Python: backend/auth/...」是規劃時的命名空間預期；實作落在 `backend/security/...`，§12 對齊紀錄。

## 1. Problem statement

OmniSight 既有 auth 是**碎片化的 ad-hoc 實作**：

- `backend/auth.py` — session-based password + cookie + CSRF
- `backend/api_keys.py` — API key auth（獨立 channel）
- `backend/git_credentials.py` — GitHub / GitLab OAuth（**outbound** integration，per-vendor 寫死）
- Phase 5b `llm_credentials.py` — LLM provider API key（per-tenant Fernet 加密儲存，per-vendor 寫死）
- K 系列 — multi-session hardening、MFA、password policy
- K-rest — Cloudflare Access SSO（網路層 IdP）

**問題**：
1. **無共用 OAuth abstraction** — 每個 vendor 整合都重寫 PKCE / state / refresh / token storage
2. **無 OAuth login UI** — OmniSight 自家只能 password 登入，企業客戶要 Google Workspace SSO 是基本要求
3. **無 bot 防禦** — 既有 login / signup / password-reset 沒 CAPTCHA，靠 CF Bot Fight Mode + rate limit 二線
4. **無 auto-gen 密碼 UX** — 既有 K 系列 password policy 嚴格但 UX 差（要 user 自己想 12+ 字密碼）
5. **W/FS/SC template 重複造輪** — 沒 shared lib 的話 generated app 各自實作 OAuth/Turnstile，drift 必然

## 2. Decision

建立 **Priority AS — Auth & Security Shared Library**，提供：

| 元件 | OmniSight 自家用（as-built path） | Generated app（template emit） |
|---|---|---|
| OAuth client core lib（AS.1） | Python: `backend/security/oauth_client.py` + `backend/security/oauth_vendors.py` | TypeScript: `templates/_shared/oauth-client/` |
| Token vault（AS.2） | Python: `backend/security/token_vault.py` + `backend/security/oauth_refresh_hook.py` + `backend/security/oauth_revoke.py` | TypeScript: `templates/_shared/token-vault/` |
| Bot challenge lib（AS.3） | Python: `backend/security/bot_challenge.py` + `backend/security/turnstile_form_verifier.py` | TypeScript: `templates/_shared/bot-challenge/` |
| Honeypot helper（AS.4） | Python: `backend/security/honeypot.py` + `backend/security/honeypot_form_verifier.py` | TypeScript: `templates/_shared/honeypot/` |
| Auto-gen password lib（AS.0.10） | Python: `backend/security/password_generator.py` | TypeScript: `templates/_shared/password-generator/` |
| Audit hooks（AS.5） | Python: `backend/security/oauth_audit.py` + `backend/security/auth_event.py` + `backend/security/auth_audit_bridge.py` + `backend/security/auth_dashboard.py` | TypeScript: `templates/_shared/auth-event/` + `templates/_shared/auth-dashboard/` |
| Credential vault（AS.6.2） | Python: `backend/security/credential_vault.py`（git secrets re-encrypt 路徑） | — |
| Single-knob 收口（AS.0.8） | `settings.as_enabled`（每模組 `is_enabled()` 走 `getattr(settings, "as_enabled", True)`） | — |

**雙 twin pattern**：Python lib（OmniSight backend）+ TypeScript lib（emit 進 generated app workspace），共享 spec 經 drift guard test 確保兩 surface 行為一致。

### 為何不選 Option A（混合進 FS/SC）

- FS / SC 的 phase 邊界會爆炸（OmniSight 自身 auth lifecycle 嚴格度遠高於 template）
- OmniSight 自家想升級獨立節奏會卡

### 為何不選 Option C（完全獨立）

- 雙實作 drift 必然事故
- OmniSight 不 dogfood lib = template 沒 production 驗證
- vulnerability fix 只修一邊很常見

## 3. AS.0 — Compatibility & Migration Discipline（**零妥協**）

production 已上線、有真實 operator + 169 commit ahead of origin。AS 任何動作必須遵守：

### 3.1 既有 auth surface 完整盤點（AS.0.1）

開工前列出以下完整 inventory：
- 所有 login / signup / password-reset / API auth call site
- 所有 git_credentials / LLM credentials call site
- 所有自動化 client（CI / monitor / e2e test / customer scripts）— 進 AS.0.6 bypass list

**沒做盤點不可開工**。

### 3.2 Per-tenant feature flag schema（AS.0.2，alembic 0056）

```sql
ALTER TABLE tenants ADD COLUMN auth_features JSONB
  NOT NULL DEFAULT '{}'::jsonb;
```

預設值：
- 既有 tenant：`{}` (隱含全 false) — **零行為變動**
- 新 tenant：`{"oauth_login": true, "turnstile_required": true, "honeypot_active": true, "auth_layer": "app_oauth"}`

`auth_layer` 三選一：`cf_access | app_oauth | password_only`，明確互斥避免 K-rest CF Access SSO 與 AS OAuth 重疊邏輯衝突。

Tenant admin 可在 Settings → Auth → "Enable OAuth login" 切換、寫 audit row + 通知所有 active session 重新 challenge。

### 3.3 Account-linking 安全規則（AS.0.3）

**OAuth email 匹配既有 password user**：

```
case A — 既有 password user (foo@x.com) + 點 "Sign in with Google" (Google email = foo@x.com):
  → 跳「先用 password 登入確認你是這個帳號」flow
  → password 驗證 + MFA 通過後才 link OAuth 到既有 user
  → 防 takeover（攻擊者用受害者 email 在 Google 註冊不能直接拿到帳號）

case B — 從未 link 過的 OAuth → 跳「驗證你擁有此 email」flow

case C — 純 OAuth-only 帳號（沒 password）：
  → users.auth_methods = {"google"}
  → password reset endpoint 回 400 "OAuth-only account, manage at provider"
```

`users.auth_methods` 改成 set/array，必須在 list 內的方法才能登入。既有 user 預設 `{"password"}`，需主動 add OAuth。

**MFA 一律 enforce**：OAuth → email match → MFA challenge → session（OAuth 只是第一因子，MFA 仍在）。**無 OAuth bypass MFA**。

**email_verified trust 差異**：
- Google / Apple / Microsoft `email_verified = true` 可信
- GitHub `email_verified` 不信，強制 OmniSight 端發 email 二次驗證

### 3.4 Credential refactor — expand-migrate-contract（AS.0.4 + AS.6.2）

```
Phase 1 (AS.2): oauth_tokens 新表並存既有 git_credentials / llm_credentials
                read 仍走舊表，write 雙寫（舊+新）

Phase 2 (AS.6.2 backfill): 後台 idempotent script
                read 舊表 → re-encrypt → write 新表
                舊表保留 read-only 不刪

Phase 3 (AS.6.2): 切讀路徑到新表
                舊表保留作 fallback 30 天

Phase 4 (一個 release cycle 後): drop 舊表
```

**Encryption key 連續性**：保留舊 Fernet master key、新表也用同 key。萬一 rollback 舊 ciphertext 還 decrypt 得回來。`oauth_tokens.key_version` field 區分新舊加密、log 每個 decrypt 走哪版。

**API surface 不改**：`pick_account_for_url(...)` / `resolve_provider_balance(...)` 簽名 frozen，內部換 implementation。13+ 個 caller 不需動。

### 3.5 Turnstile fail-open 漸進策略（AS.0.5）

```
Phase 1（4 週）：fail-open + warning log
                Turnstile 沒過仍允許登入
                寫 audit「unverified」標記

Phase 2（4 週）：fail-open + per-tenant alert
                admin 看到「上週有 X 次未驗證登入」報告

Phase 3：tenant 主動 opt-in 切 fail-closed
        既有 prod tenant 永遠 default 在 Phase 1，不主動 push
```

**JS 載入失敗 → 自動 fallback 到 honeypot + 慢速 rate limit**（不 fail-closed 鎖 user）。Score-based UX：v3 invisible 模式優先，只有 score 低才出視覺挑戰。

### 3.6 Automation bypass list（AS.0.6）

- API key 認證一律 bypass Turnstile（已 authenticated）
- 既有 IP allowlist（CI runner / monitor IP）— Settings UI 可加
- test token header `X-OmniSight-Test-Token` 帶有效 token bypass（CI / e2e 用）
- 全部 bypass 寫 audit row + 月結報表給 admin 審

### 3.7 Single-knob rollback（AS.0.8）

```bash
export OMNISIGHT_AS_ENABLED=false
docker compose restart backend-a backend-b
# AS 全套 disabled
# login 走 100% 既有 password flow
# Turnstile 不檢查
# 不寫新 token_vault
# git_credentials / llm_credentials 走既有 ad-hoc path
```

任何 production 異常 → 設 false + restart → instant fallback 到 pre-AS 行為。AS 任何 code path 都檢查此 flag、保證可全切回。

### 3.8 Compat regression test（AS.0.9 — 5 顆必過）

1. 既有 password user 完整 login flow（不裝任何 AS feature）
2. 既有 password user with MFA login flow（K 系列 MFA 仍工作）
3. API key client login（不帶 Turnstile）
4. test-token header bypass（CI scenario）
5. rollback knob 切 false 後行為跟 pre-AS 一致

## 4. AS.0.10 — Auto-gen 密碼 lib（3 種 style）

### 4.1 Style A — Random（max 安全，預設）

```
範例:    kJ#9mPx$2vRq8nYz!4Bw
長度:    20 字元（slider 8-32 可調）
字元集:  alphanumeric + symbols
避開:    0/O / l/1/I 等視覺易混（可開關）
熵:      ~131 bits
推薦給:  用 password manager 的 user
```

### 4.2 Style B — Memorable（Diceware 詞彙）

```
範例:    correct-horse-battery-staple-42
組成:    4 個 EFF wordlist 詞 + 分隔符 + 隨機數
熵:      ~52 bits
推薦給:  得自己記、不用 password manager 的 user
分隔符:  - / _ / . / 空格 可選
語言:    英文 EFF wordlist + 中文成語版（依 locale）
```

### 4.3 Style C — Pronounceable（子音母音交替）

```
範例:    rifobeko-pumitazo-43
組成:    consonant-vowel pair 重複 → 可念出聲音
熵:      ~75 bits（介於 A 和 B 之間）
推薦給:  要打字輸入到別處的 user（手機 ↔ 電腦）
```

### 4.4 UX 整合三點

**1. 預設行為**：點「Sign up」按鈕、密碼欄**自動填一個 Style A 強密碼**（不用 user 點 generate）。瀏覽器 / iCloud Keychain / 1Password 自動偵測 → 浮 prompt「Save this password?」→ user 一鍵存。**整個流程 zero friction**（Apple macOS Sonoma pattern）。

**2. 強制「我已保存」勾選**：
```
[✓] I have saved this password somewhere safe.
    Without saving, you'll lose access to your account.
```
沒勾 → submit button disabled。擋掉「random 完按 enter 結果忘記寫下」的悲劇。

**3. Real-time strength meter + breach check**（即使 user 自己打的）：
- zxcvbn score 即時顯示
- HaveIBeenPwned k-anonymity API（傳 sha1 prefix 5 字、不洩漏密碼）
- 跟 K 系列 password policy 整合（最後 5 次禁用 / 跟 email 太相似禁用）

## 5. AS.7 — 8 視覺層 spec（"Command Bridge Auth Experience"）

OmniSight 自身 auth 8 頁全面重做的視覺基礎。8 層動畫疊加，每層解一個感官線索：

### 5.1 層 1 — 背景星雲（全螢幕 WebGL fragment shader，~50 行 GLSL）

```glsl
// fragment.glsl 簡化骨架
- 緩慢漂移星雲（OmniSight palette: deep-space + neural-blue + artifact-purple）
- 三層星空 parallax depth（前 / 中 / 遠）
- 游標 gravity well — 滑鼠位置扭曲星雲（重力透鏡效果）
- 隨時間呼吸（10s 週期、極慢、潛意識感知）
```

**不上 Three.js / WebGL framework** — 純 vanilla GLSL，避免 600 KB Three.js dep。

### 5.2 層 2 — 浮動 Auth Card（玻璃艦橋窗框感）

```
重 glass morphism: backdrop-filter: blur(24px) saturate(180%)
+ 邊框 corner-brackets-full（既有 class）
+ 邊緣 neon glow flicker（隨機 50ms 微閃、像舊霓虹燈）
+ 容積陰影（card 浮在虛空中 50px 高）
+ idle drift（reuse BS.3 motion library，幅度加大到 ±6px）
+ 游標磁吸 3D tilt（reuse BS.3，rotateX/Y 範圍 5° → 12°）
+ scroll parallax（card 比背景慢 0.85x）
```

### 5.3 層 3 — Brand wordmark 動畫

```
"OmniSight" 文字效果:
- 漸層字（neural-blue → artifact-purple → validation-emerald）
- 字母內有「光弧 traveling」一道亮光每 4s 從左滑到右
- 整段字 breathing-pulse（吸 / 吐 2s 週期）
- 滑鼠靠近時 scale 1.02 + bloom（鏡頭眩光效果）
- 點任何 input field 時 wordmark 微微「回應」（亮一下）
```

### 5.4 層 4 — Input field 能量化

```
未 focus: 普通邊框 + 內陰影
focus 進入瞬間:
  ⚡ 4 個 corner brackets 從外飛進來 snap 到位（150ms）
  ⚡ 邊框轉為 gradient 動畫（neural-blue → cyan → 回 neural-blue 循環 2s）
  ⚡ field 內部底色加微 scan line
  ⚡ 輸入時每個字符出現帶微閃光
驗證成功: 邊框轉 emerald + 短暫 inner glow pulse
驗證失敗: 邊框 critical-red + 0.4s 抖動 + glitch 效果
```

### 5.5 層 5 — OAuth provider energy spheres

```
每個 OAuth button:
- Provider brand 色當主色
- 圓形 shape（不是矩形 — 像艦橋按鈕陣列）
- 中心放 provider logo + 外圍 halo glow（idle 30% opacity）
- hover: halo 漸亮到 80% + scale 1.08 + 內部 ring-spin
- click 瞬間: 中心發出 beam-shoot 朝畫面中央射出 → warp transition
- 6 個 button 不完美對齊（各 ±2deg 旋轉 + 微 idle drift），看起來像懸浮儀錶盤
```

### 5.6 層 6 — Auto-gen 密碼 Slot Machine（**signup 主視覺**）

```
時間軸 0ms:    密碼欄清空、20 個垂直 column 出現
時間軸 50ms:   每個 column 開始瘋狂 cycle 字元（所有 ASCII 隨機跳）
              字元變色（cyan → purple → emerald 循環）
              column 之間用微 stagger（左到右 5ms 差）
時間軸 200ms:  從左到右 column 開始「定格 stop」
              定格瞬間字元發出閃光 pulse + scale flash
時間軸 600ms:  全部 20 column 落定 = final 密碼出現
時間軸 700ms:  整個密碼欄一道光弧從左掃到右（confirm 完成）
時間軸 750ms:  Strength meter 從 0 用「液態 glow」填充到 strong
時間軸 1000ms: ✓ Not in breach DB 檢查打勾、emerald pulse
```

整個過程**1 秒**，視覺像「量子計算機在生密碼」。

### 5.7 層 7 — Page transition: Warp Drive

```
Frame 1 (0ms):    card 開始 zoom in
Frame 2 (200ms):  背景星雲拉長條紋（hyperspace effect）
                  card scale 到 2x + blur
Frame 3 (400ms):  card 消失於畫面中心
                  只剩星雲拉伸線
Frame 4 (500ms):  反向、新 card 從深處浮出
Frame 5 (700ms):  星雲回正、新 card 完整顯示
總 700ms，感官像「跨越光年到下一頁」
```

### 5.8 層 8 — 場景化 dramatic state

| 狀態 | 視覺 |
|---|---|
| 登入成功 | Card 中心發 emerald 能量波 → 圓形擴散填滿全螢幕 → fade → warp |
| 登入失敗 | Card spring-shake + 紅 lightning flicker × 2 + 邊框紅 |
| MFA 6 digit 輸入 | 每填一個 digit、那個 box pulse + cyan 漣漪；填滿 6 個整排同步 pulse + spinning verification ring |
| MFA 通過 | 6 個 box 同步 emerald glow + 上升組成「✓」形狀 |
| 帳號鎖定 | Card 整個變藍 tint + 速度感慢下來（所有動畫降 0.5x speed）+ frosted overlay |
| 歡迎新用戶 | 1 秒慶祝 burst — 30 個小粒子從 card 中心爆發 + trail + 升起組「Welcome aboard」 |
| email verification 等待 | Card 中央放 envelope icon、idle motion（左右搖擺） |
| 密碼 reset 中 | Card 暫時模糊化（blur 4px）+ 中央 ring-spin |

### 5.9 Reduce-motion + 電池感知

`@media (prefers-reduced-motion: reduce)` 全 8 層退化：
- 星雲 → 靜態 gradient
- Card → 無 idle drift / 無 magnetic tilt
- Wordmark → 靜態漸層
- Field → 靜態邊框 + 純色 focus
- OAuth → 靜態 button + simple hover scale
- Slot machine → 直接顯示最終密碼（無 cycle 動畫）
- Warp → 200ms cross-fade
- 狀態 → 純文字 + 顏色（無 shake / burst）

電池感知 4 級降級（reuse BS.3 規則）：
- 充電中 → 全套 (dramatic level)
- 電量 30-50% 沒充電 → 降一級（normal）— 關背景 shader
- 電量 15-30% 沒充電 → subtle — 只保留 hover lift + click feedback
- 電量 < 15% → off — 跟 reduce-motion 同

## 6. K-rest CF Access SSO 與 AS OAuth 邊界

> **2026-04-27 audit clarification (P2.1)**：本節實作邊界釐清。原本 TODO row 寫「K-rest 既有 CF Access SSO」是 ops-level 描述（CF console 配置完成），不是 backend code-level feature（backend 沒讀 CF headers）。本節說明 AS 落地時要做哪些 backend code 動作才能讓 `auth_layer="cf_access"` 真正運作。

兩者**功能重疊但設計上互斥使用**：

| 層 | 職責 | 觸發場景 | 既有狀態 |
|---|---|---|---|
| K-rest CF Access | 網路層 SSO（IdP-driven，Cloudflare 邊界閘） | 企業 SSO 配置、whole-site 都在 Access 後面 | ✅ ops-level shipped (Cloudflare console 配置 + email-OTP rule) <br>❌ backend code 未讀 `Cf-Access-Authenticated-User-Email` / `CF-Ray` 等 headers |
| AS OAuth | 應用層 OAuth（user-driven，OmniSight backend 處理 OIDC flow） | 個人 / SaaS user 自選 provider | ❌ 整套未做（AS 主要交付） |

### 6.1 「shipped」claim 的 audit clarification

CF Access 邊界控制透過 **Cloudflare console 設定**（rule 5.1 + email-OTP），這部分 **ops-level 已落地**（詳見 `docs/ops/cloudflare_settings.md`）。

但 **backend code 完全沒對應 logic**：
- 沒讀 `Cf-Access-Authenticated-User-Email` header（CF Access 會在 verified 流量加這個 header）
- 沒驗 `Cf-Access-Jwt-Assertion`（CF 簽的 JWT、可驗來源是真 CF）
- 沒在 backend 用 user email 自動 link / create OmniSight user

**意思是**：目前 CF Access 是「**邊界控制 + ops-level trust 邊界**」— 過了 CF Access 的流量被當「已認證」直接送 backend session 流程。**這對單 tenant 是 OK 的**（whole-site 後面、cookie session 仍照常驗），但對 multi-tenant + auth_layer="cf_access" 沒法分辨「哪個 tenant 的 user 進來」。

### 6.2 AS 落地時要做的 CF Access 整合（如果走 cf_access path）

如果要支援 `auth_layer="cf_access"` 的 tenant、AS.6 範圍要加：

1. **新 middleware `backend/auth/cf_access_middleware.py`**
   - 讀 `Cf-Access-Authenticated-User-Email` + `Cf-Access-Jwt-Assertion`
   - 驗 JWT signature 對 CF public key（防偽造 — 攻擊者直接打 backend 不經 CF 邊界）
   - 從 email 找 / 自動 create OmniSight user（per-tenant scope）
   - 寫 audit row「CF Access SSO login」
2. **AS.0.2 `tenants.auth_features.auth_layer="cf_access"` 落地時要做的開關**
   - 啟 cf_access middleware
   - **不啟** AS OAuth login UI（AS.7.1 login page 自動隱藏 OAuth buttons）
   - **啟** password 登入仍可用作 fallback（避免 CF Access 中斷時 user 鎖死）
3. **K-rest CF Access doc 同步更新**
   - 補一段「backend 對 CF headers 的 trust 規則」
   - 列「whole-site CF Access vs per-route CF Access」差異
   - 寫 deploy SOP — CF Access 必須跟 cf_access auth_layer flag 一起切

### 6.3 三 path 行為摘要

`auth_features.auth_layer` 三選一：

| auth_layer | login UI 顯示什麼 | backend 認證流程 | 適用 tenant |
|---|---|---|---|
| `cf_access` | 沒 OAuth buttons、沒 password form（CF Access 已經認證了） | CF middleware 從 header 取 email → find/create user → session | 企業 tenant、有 SSO IdP（Okta / Azure AD / Google Workspace） |
| `app_oauth` | OAuth buttons (Google/Apple/GitHub/MS) + password form 並列 | AS OAuth flow OR password flow（AS 完整） | SaaS tenant、user 自選 provider |
| `password_only` | 只 password form | 既有 K 系列 password 流程不變 | 既有 prod tenant default、legacy 兼容 |

UI 自動隱藏不適用的選項，避免 user 困惑。

### 6.4 P2.1 audit follow-up status

- ❌ **未做**：backend CF Access middleware（要做才能真正支援 `auth_layer="cf_access"`）
- ✅ **已 documented**：本 ADR §6 說清楚 ops vs code-level 邊界
- ❌ **未做**：K-rest doc 同步更新 backend trust 規則（AS.6 落地時補）
- ⏳ **AS 落地時會做**：AS.6 要實作 cf_access_middleware 才能真正 ship `auth_layer="cf_access"` 三選一

**結論**：CF Access 在 OmniSight 目前是「**邊界控制 + 非分 tenant 的 trust gate**」。要真正讓「企業 tenant 走 CF Access SSO 一路認證到 backend user」，**AS.6 的工作範圍要加 cf_access_middleware**。本 audit clarification 把這個責任放到 AS 落地的 spec 內、避免之後繼續以為「K-rest 既有 CF Access SSO 已 shipped」就跳過 backend code 工作。

## 7. K 系列 password 政策對 OAuth-only user 的處理

K 系列既有：90 天到期 / 強度檢查（zxcvbn ≥ 3）/ 歷史 5 次禁用 / breached password DB（HaveIBeenPwned）。

**OAuth-only user 處理規則**：
- `users.auth_methods` 不含 `"password"` → 跳過所有 password policy check
- Password reset endpoint → 400 「OAuth-only account, reset password at provider」
- UI 顯示「You signed in with Google. To change credentials, manage at your Google account.」

## 8. 範圍 + 排程

```
AS.0  Compatibility & Migration Discipline (含 auto-gen lib)  ~1.5d
AS.1  OAuth Client Core Lib                                   ~2d
AS.2  Token Vault (alembic 0057 oauth_tokens)                 ~1d
AS.3  Bot Challenge Lib                                       ~1d
AS.4  Honeypot Helper                                         ~0.25d
AS.5  Observability + Audit                                   ~0.5d
AS.6  OmniSight Backend Self-Integration (含 credential refactor)  ~1.5d
AS.7  OmniSight Auth UI/UX Overhaul (8 頁 + 8 視覺層)          ~14d
AS.8  Tests + Migration Runbook + ADR                         ~0.5d

總計: ~21.75 day（11 phase）
```

### 平行分工

- **2 track 平行**：
  - Track A（後端）：AS.0 → AS.1 → AS.2 → AS.3-5 → AS.6 → AS.8（~8d）
  - Track B（前端）：AS.7.0 → AS.7.1-7.8（~14d）
  - 同步點：AS.6 ship 後 → Track B 才能在 AS.7 接 backend 真實 endpoint
  - Wall time: 14d（Track B 是關鍵路徑）

## 9. Migration 編號

| Migration | 用途 | Phase |
|---|---|---|
| 0056 | `tenants.auth_features` JSONB 欄位 | AS.0.2 |
| 0057 | `oauth_tokens` table | AS.2.2 |
| 0058 | `users.auth_methods` JSONB 欄位 + account-linking takeover-prevention 政策 | AS.0.3 |

開工前驗證指令：
```bash
ls backend/alembic/versions/ | tail -3
# 預期：W11-W16 開工後最後一號可能是 0060；AS 從那 +1 開始（如 0056 仍可用看 BS 實際用到哪）
```

## 10. Risks + Open questions

風險登記：
- **R31**: OAuth account takeover via email collision → AS.0.3 強制 password 驗證 BEFORE link
- **R32**: OAuth path bypass MFA → AS.0.3 OAuth 後仍要求 MFA 第二因子
- **R33**: Credential refactor data loss → AS.0.4 expand-migrate-contract + key 連續性
- **R34**: Turnstile lock 既有自動化 client → AS.0.5 fail-open 漸進 + AS.0.6 bypass list

Open questions:
1. **WebGL shader 在低階 device 性能** — AS.7.0 背景星雲在 ~3-5% GPU 持續成本，需驗 mid-tier laptop / mobile FPS ≥ 50。電池感知策略應自動降級。
2. **Turnstile 在大陸地區 access** — Cloudflare 在大陸 CDN 不穩，部分 user 可能 fail。Fallback chain（Turnstile → reCAPTCHA → hCaptcha）必要。
3. **`navigator.getBattery()` Firefox / Safari 不支援** — 假設充電中 fallback、用 user pref，AS.7.0 reduce 規則對這兩 browser 不主動降級。
4. **自動 generated app template 的 OAuth client 取得** — Google / Apple / Microsoft / GitHub 沒有讓 generated app 自動建 OAuth client 的 API（怕 abuse），FS.2.3 走 step-by-step instructions + 偵測 callback URL 變動。

## 11. References

- 母 ADR：`docs/design/w11-w16-as-fs-sc-roadmap.md`
- BS bootstrap pattern：`docs/design/bs-bootstrap-vertical-aware.md`（BS.0.1 寫入）
- AS.0.1 既有 auth surface inventory：`docs/security/as_0_1_auth_surface_inventory.md`
- AS.0.3 account-linking 安全規則：`docs/security/as_0_3_account_linking.md`
- AS.0.4 credential refactor expand-migrate-contract：`docs/security/as_0_4_credential_refactor_migration_plan.md`
- AS.0.5 Turnstile fail-open 三階段：`docs/security/as_0_5_turnstile_fail_open_phased_strategy.md`
- AS.0.6 automation bypass list：`docs/security/as_0_6_automation_bypass_list.md`
- AS.0.7 honeypot field 設計：`docs/security/as_0_7_honeypot_field_design.md`
- AS.0.8 single-knob rollback：`docs/security/as_0_8_single_knob_rollback.md`
- AS rollout runbook：`docs/operations/as-rollout-and-rollback.md`（AS.8.3 寫入 — 仍待 land）
- 既有 K 系列：TODO.md Priority K / K-rest / K-early
- 既有 git_credentials.py + Phase 5b llm_credentials.py：refactor 對象（AS.6.2 走 `backend/security/credential_vault.py`）

## 12. As-built status（2026-04-28）

> 本節為 AS.8.2 ADR 收尾時加入的「實作落地對齊」紀錄。AS.0 → AS.7 + AS.8.1 全部 landed 之後，把規劃 §2 的元件預期、§9 的 migration 編號、§5 的 8 視覺層描述，跟實際 git tree 對上，避免讀者把 §2 的「`backend/auth/...`」當成尋路指引（實作落在 `backend/security/...`）。

### 12.1 落地的 backend 11 模組（位置以 git tree 為準）

```
backend/security/
├── __init__.py
├── oauth_client.py            # AS.1.1 PKCE + state + parse_token_response + is_enabled() 收 settings.as_enabled
├── oauth_vendors.py           # AS.1.2 vendor catalog（Google / GitHub / Microsoft / Apple / 其他）
├── oauth_audit.py             # AS.1.4 forensic audit emitters（oauth.login_init / login_callback / refresh / unlink ...）
├── token_vault.py             # AS.2.1 (user_id, provider) binding 信封 + version + key_version
├── oauth_refresh_hook.py      # AS.2.4 due-record refresh + rotation 偵測 + version+1 + audit chain
├── oauth_revoke.py            # AS.2.5 DSAR / right-to-erasure（unlink 永遠 emit、不論 IdP 結果）
├── oauth_login_handler.py     # AS.6 backend 自家 OAuth login orchestrator
├── bot_challenge.py           # AS.3.1 Turnstile 三階段 + AS.0.6 bypass evaluator
├── turnstile_form_verifier.py # AS.3.2 form 端驗 helper
├── honeypot.py                # AS.4.1 invisible field 名稱算 + AS.0.6 bypass 對齊
├── honeypot_form_verifier.py  # AS.4.2 form 端驗 helper
├── auth_event.py              # AS.5.1 dashboard rollup emitters（auth.login_success / oauth_connect / token_rotated ...）
├── auth_audit_bridge.py       # AS.5.2 forensic ↔ rollup 雙寫橋
├── auth_dashboard.py          # AS.5.3 dashboard 讀路徑（per-tenant rollup 聚合）
├── credential_vault.py        # AS.6.2 git secrets re-encrypt 入口（tenant_id × secret_kind binding）
├── password_generator.py      # AS.0.10 三種 style（random / diceware / pronounceable）
├── prompt_hardening.py        # 既有 R20 chat-layer security（非 AS scope，但 backend/security/ 同居）
└── secret_filter.py           # 既有 secret 過濾（非 AS scope）
```

**Knob 收口**：每個 AS 模組 export 自己的 `is_enabled()`，全部讀 `settings.as_enabled`（`getattr(settings, "as_enabled", True)`），缺 setting 時 default true（保留可運作）。AS.0.8 single-knob rollback 路徑：set `settings.as_enabled=False`（或 env `OMNISIGHT_AS_ENABLED=false` + restart）→ 所有 async emitter `return None`、pure helper 仍可被呼叫但 emitter 都 silent-skip。AS.8.1 cross-feature integration test #5 (`test_knob_off_silences_audit_emitters_but_pure_helpers_still_run`) 鎖定此契約。

### 12.2 落地的 templates/_shared 7 個 TypeScript twin

```
templates/_shared/
├── oauth-client/      # AS.1 TS twin（README + index.ts + vendors.ts + audit.ts）
├── token-vault/       # AS.2 TS twin
├── bot-challenge/     # AS.3 TS twin
├── honeypot/          # AS.4 TS twin
├── password-generator/# AS.0.10 TS twin
├── auth-event/        # AS.5.1 TS twin
└── auth-dashboard/    # AS.5.3 TS twin
```

雙 twin pattern：generated app 拿到的是 TypeScript 版，行為跟 Python 版同 spec，drift 由 AS.8.1 contract test + spec table 鎖定。

### 12.3 Migration（§9 表格 as-built 確認）

| Migration | 用途 | as-built 狀態 |
|---|---|---|
| `0056_tenants_auth_features.py` | `tenants.auth_features` JSONB 欄位（既有 tenant 預設 `{}` 隱含全 false） | ✅ landed |
| `0057_oauth_tokens.py` | `oauth_tokens` table（AS.2.1 token vault） | ✅ landed |
| `0058_users_auth_methods.py` | `users.auth_methods` JSONB 欄位 + account-linking takeover-prevention 政策 | ✅ landed（檔名 0058 跟 0057 同 phase；AS.8.1 §52 已記錄 `test_alembic_pg_compat.py` 既有 naming-convention 假設因此 fail，pre-existing、out-of-scope） |

### 12.4 AS.7 8 視覺層 → 8 dedicated pages 對應

| AS.7 page | 路徑 | 視覺層重點 |
|---|---|---|
| AS.7.1 login | `app/login/page.tsx` | 8 視覺層全套 + AS.7.6 423 redirect + AS.7.4 mfaPending push |
| AS.7.2 signup | `app/signup/page.tsx` | 層 6 slot-machine（核心主視覺） |
| AS.7.3 password reset | `app/forgot-password/page.tsx` + `app/reset-password/page.tsx` | 層 8 ring-spin during reset |
| AS.7.4 MFA | `app/mfa-challenge/page.tsx` | 層 8 6-digit pulse + ✓ 形狀組成 |
| AS.7.5 email verification | `app/email-verify/page.tsx` | 層 8 envelope idle motion |
| AS.7.6 account locked | `app/account-locked/page.tsx` | 層 8 chill blue-tint + 動畫 0.5x speed + frosted overlay |
| AS.7.7 profile / settings | `app/settings/account/page.tsx` | OAuth orbital satellites（層 5 升級，雙圈 counter-rotation） |
| AS.7.8 first-login onboarding | `app/onboarding/page.tsx` | 30-particle celebration burst + "Welcome aboard, X!" rising wordmark |

視覺基礎在 `lib/auth-visual/` 4 helper module + `components/omnisight/auth/` 11+ React primitive + `styles/auth-visual.css`。Reduce-motion + 4-level battery-aware degradation（§5.9）由 root level resolver `useEffectiveMotionLevel()` thread 給所有 leaf — frame mismatch 不存在。

### 12.5 落地的 test 收尾（AS.8.1）

```
backend/tests/
├── test_as_compat_regression.py        # AS.0.9 baseline — 7 test (5 critical + §7.2.6 oracle drift guard + settings field placeholder)
└── test_as_cross_feature_integration.py # AS.8.1 — 8 cross-feature integration test
```

8 顆 cross-feature test 把 11 個 backend 模組兩兩 / 三三 / 四四 wire 在一起測 boundary 行為，單模組重構不會通過、必須兩個一起改才綠。詳細 test name + cross-feature 對應參見 HANDOFF.md 2026-04-28 AS.8.1 entry §22-§29。

Pre-commit fingerprint grep（`_conn() / await conn.commit() / datetime('now') / VALUES (?,...)`）對 AS 全模組 0-hit。

### 12.6 與 §5 / §6 spec 對照的 deferred 項

落地時刻意保留為 follow-up（不 in-scope to AS.8）：

- **CF Access middleware**（§6.2 第 1 項）— `backend/security/cf_access_middleware.py` 仍未實作。意思是 `auth_features.auth_layer="cf_access"` 三選一裡的 `cf_access` path 目前沒 backend code 支撐；既有 `cf_access` ops-level（CF console rule 5.1 + email-OTP）仍在 trust-edge mode。AS.6 落地時的 K-rest doc 同步更新 + middleware 實作整體放到 AS.8 之後的 follow-up row（仍由 audit clarification §6.4 紀錄）。
- **K-rest doc 同步更新**（§6.2 第 3 項）— 跟上面 follow-up 一起 land。
- **AS.7.0 deployed-active level perf-budget e2e**（Playwright）— vitest contract test 已涵蓋 component path × motion level matrix；e2e 是 AS.7.0 deployed-active 階段獨立 task。
- **`settings.as_enabled` field 正式 land 在 `backend/config.py`** — 目前每模組走 `getattr` fallback default true；當 settings field 正式宣告後 AS.8.1 cross-feature test #5 的 monkeypatch 路徑可以改用直接翻 setting 的更 robust 寫法（HANDOFF.md AS.8.1 §52 同樣紀錄）。

### 12.7 Production status

```
Production status: deployed-active（AS.0–AS.7 + AS.8.1 全部 landed；as_enabled default true，每 module is_enabled() 都讀 settings；
                                    既有 prod tenant 因 §3.2 預設 auth_features={} 故所有 per-tenant gate 仍隱含 off，
                                    AS shared lib 全套對既有 tenant 行為實質仍 inert — 等於 deployed-but-tenant-gated）
Next gate:        deployed-observed（24h observation window after first tenant flips auth_features.{oauth_login|turnstile_required|honeypot_active}=true；
                                     觀察 metric：oauth_audit chain integrity、token_vault decrypt outcome 分佈、turnstile fail-open 上線 unverified-rate）
```

### 12.8 給未來自己的 sanity-check checklist

照 SOP §136-227 Production Readiness Gate 的 5 個問題，AS shared lib as-built 狀態：

1. **這條 code path 現在 production image 真的跑得起來嗎？** — ✅ AS 11 modules 全在 `backend/security/`，跟既有 `backend/audit.py` / `backend/auth.py` 同 image，無新 OS / Python wheel。
2. **這條 code path 依賴的「靜態列表 / catalog / schema」跟 live 狀態對齊嗎？** — ✅ AS.5.1 dashboard rollup 用的 `entity_id` 模式、AS.1.4 forensic action 字串、AS.2.4 refresh-rotate 物件 shape 都被 AS.8.1 cross-feature test 8 顆 case-by-case 鎖定；單向更動會立刻 CI red。
3. **Env knob / feature flag 實際有 set 嗎？** — ⚠️ `settings.as_enabled` 走 `getattr` fallback default true；`auth_features` per-tenant 仍 `{}` for legacy tenant。沒 explicit set 任何 prod tenant 進 active。
4. **Docker network / external volume / compose dependency 真存在 / 真 mount / 真連通嗎？** — ✅ AS 沒新增 docker resource。Turnstile / OAuth provider 都走 outbound HTTPS，現有 egress 已涵蓋。
5. **有沒有 drift guard test 可以主動抓到 #2？** — ✅ AS.8.1 8 顆 cross-feature test + AS.0.9 5 顆 critical + `test_as_compat_regression.py::§7.2.6` oracle drift guard。
