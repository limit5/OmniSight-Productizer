---
audience: internal
---

# ADR — Web Vertical 擴充 + Auth/Security Shared Lib + Full-Stack Generation + Security Compliance Roadmap (W11-W16 / AS / FS / SC)

> Status: Accepted 2026-04-27
> Authors: nanakusa sora + Agent-software-beta
> Supersedes: 部分 W 系列原 scope
> Related: BS (Bootstrap & Platform Catalog), R20 (chat-layer security), Priority L (既有 bootstrap wizard), K-rest (CF Access SSO)

## 1. 戰略 context

OmniSight Priority W 系列（W0-W10）已 ship，覆蓋多框架 scaffold（Next/Nuxt/Astro/Vue/Svelte）+ 4 deploy adapter + WCAG/GDPR/SPDX compliance + Lighthouse CI + 觀測性。但**生成的網站離 production-ready SaaS 仍有距離**，缺三類能力：

1. **Operator UX**：「**對話到 preview 的 loop 是斷的**」 — 蓋完專案 operator 還要自己 git clone + npm install + dev server 才看得到。
2. **Backend / DB / Auth 自動化**：W 只蓋前端 + 靜態部署，DB provisioning / 認證 setup / object storage / email / background jobs / search 全靠 operator 手動補 setup。
3. **資安自動化**：W5 只覆蓋 WCAG/GDPR/SPDX license scan，OWASP Top 10 / SAST / DAST / SCA / 安全 headers / per-jurisdiction 法規等資安自動化覆蓋率僅 ~35%。

本 ADR 規劃四條 priority section 把這三類能力補齊：
- **W11-W16**（11d）— 借鑑 firecrawl/open-lovable (MIT) 的網站克隆 + live preview + 對話 UX 整合
- **AS**（22d）— OAuth / Turnstile / Token Vault / Auto-gen 密碼 shared library，OmniSight 自身 + generated app 雙用
- **FS**（12d）— Full-stack generation（DB / auth provisioning / object storage / email / background jobs / search / billing）
- **SC**（13.5d）— Security compliance（SAST / DAST / SCA / OWASP mitigation / per-jurisdiction privacy / DSAR）

**總範圍**：~58.5 day（不計 Y / BS / BP）。落地後 generated app 自動覆蓋率 ~88%（從現況 ~50%），HIPAA / PCI DSS / SOC2 仍需 human auditor 簽但所有技術控制 90%+ 自動。

## 2. 各 Priority 的角色

### W11-W16 — Operator-facing UX 補完（解決對話 loop 斷）

| Phase | 解決什麼 | 借鑑 |
|---|---|---|
| W11 | URL → CloneSpec → 任一框架重建 | open-lovable scrape-website + scrape-url-enhanced |
| W12 | URL → BrandSpec（5 維 palette/fonts/heading/spacing/radius） | open-lovable extract-brand-styles |
| W13 | URL → multi-breakpoint screenshot | open-lovable scrape-screenshot |
| W14 | Workspace → live preview iframe（Vite hot-reload） | open-lovable create-ai-sandbox-v2 + restart-vite |
| W15 | Build error → agent 自動修 | open-lovable monitor-vite-logs + report-vite-error |
| W16 | 自然語言 chat 自動 trigger W11-W15 整套 | OmniSight 上層創新（open-lovable 是 single-page，不存在 chat 整合） |

**5 層 defense-in-depth 取代黑名單**（W11.4-W11.8）— 這是本 ADR 最重要的安全設計決策：詳見 §5。

**W14 = 既有元件加強版**：T2/T3 sandbox tier + BS.4 sidecar pattern + Y6 workspace + B12 CF tunnel + K-rest CF Access SSO 五個既有元件的 glue + Vite-specific 邏輯（~80% reuse、~20% 新 code）。**不是平行新建**。

### AS — Auth & Security shared library（OmniSight 自身 + generated app 雙用）

OAuth + Turnstile + Token Vault + Auto-gen 密碼必須 **OmniSight 自家 + W/FS/SC template 共用同一份 lib**，否則：
- 雙實作 drift 是必然事故（哪天 OAuth spec 升級可能只升一邊）
- OmniSight 自家不 dogfood 等於 generated app template 沒 production 驗證

設計：**Python lib + TS twin** 的雙 surface 模式（同 BS catalog pattern）。

**AS.6 dogfood**：OmniSight 自家 login 加「Sign in with Google / GitHub / Microsoft / Apple」+ Turnstile + auto-gen 密碼 + token vault refactor 既有 git_credentials / Phase 5b LLM credentials。

**AS.7 浮誇 UI 重做**：8 頁全面重做，套 BS.3 motion library + 額外「Command Bridge Auth Experience」8 視覺層（背景星雲 WebGL / floating glass card / brand wordmark 光弧 / field 通電 / OAuth provider energy spheres / 密碼 slot machine / warp drive transition / 場景化 dramatic）。詳見 §6。

### FS — Full-stack generation（reuse AS lib，不重複實作）

| Phase | 補的能力 | reuse 哪個 |
|---|---|---|
| FS.1 | DB provisioning（Supabase / Neon / PlanetScale） | — |
| FS.2 | Inbound auth（Clerk / Auth0 / WorkOS / NextAuth.js / Lucia） | AS.1 OAuth lib |
| FS.2b | Outbound OAuth（GitHub / Slack / Notion / Salesforce / etc.） | AS.1 + AS.2 token vault |
| FS.3-FS.6 | Object storage / email / cron / search | — |
| FS.7 | Full-stack scaffold templates（bundles） | AS + FS.1-FS.6 |
| FS.8 | Stripe / billing | AS.2 token vault for Stripe key |
| FS.9 | E2E full-stack scenario tests | — |

reuse AS 後 FS.2 + FS.2b 從原 3.5d 縮成 1.75d。

### SC — Security compliance（reuse AS bot challenge lib）

13 phase 涵蓋 SAST + DAST + SCA + Container scan + Secret scan + Security headers + OWASP Top 10 mitigation + MFA + per-jurisdiction privacy notice + DSAR + Compliance evidence bundle + PII detection + Bot defense。

**SC.13 reuse AS.3 bot_challenge lib + AS.4 honeypot**，從原 1.5d 縮成 0.5d。

## 3. 依賴拓撲

```
                    Y 完工（進行中）
                      ↓
                    BS（Bootstrap & Catalog）
                      ↓
        ┌─────────────┼─────────────┐
        ↓                           ↓
   W11-W16（11d）          AS（22d）         ← 兩條可平行
        │                           │
        └─────────────┬─────────────┘
                      ↓
                   FS（12d, reuse AS）
                      ↓
                   SC（13.5d, reuse AS）
                      ↓
                   BP（~5.5w architecture）
```

**關鍵 invariant**：
- W11-W16 / AS 平行 ✓（merge conflict 0 風險、不同 surface）
- FS / SC 必須 AS 之後（reuse AS lib）
- W11-W16 / FS 必須 BS.4 sidecar 之後（reuse pattern）

## 4. Migration / 編號 / 兼容性

### Alembic 編號完整 layout

```
Y1 (已 ship):         0032-0038
Y2-Y10 (預留):        0039-0050
BS:                   0051-0055
AS:                   0056-0058
W11-W16:              0059-0060
FS:                   0061-0063
SC:                   0064-0065
BP:                   0066+
```

每段預留有 1-2 號 buffer。BS / AS / W / FS / SC 開工前都要先 `ls backend/alembic/versions/ | tail -3` 確認上段實際用到哪號，從那 +1 開始。

### 既有 production user 兼容性（**最關鍵設計約束**）

production 已上線、有真實 operator + 169 commit ahead。所有新 priority 必須遵守：

1. **Strict additive** — 不刪不改既有 schema column / API surface / behavior，只 add
2. **Per-tenant feature flag** — 既有 tenant 預設關閉新行為（AS.0.2 `tenants.auth_features` JSONB）；新 tenant 預設開啟
3. **Single-knob rollback** — 每 priority 都有一個 env knob 可全套 disable（如 `OMNISIGHT_AS_ENABLED=false`）
4. **既有 4 step `REQUIRED_STEPS` bootstrap 永遠不變** — Vertical setup / OAuth / Turnstile 全是 optional intermediate
5. **Compat regression test suite** — 每 priority ship 前必過 N 顆 critical regression test（AS.0.9 列了 5 顆）

### AS.0 — credential refactor 的 expand-migrate-contract 策略

既有 `git_credentials` / `llm_credentials` 表有 production 加密 row，AS.6.2 重構必須走：

- **Phase 1**: AS.2 `oauth_tokens` 表並存既有兩表（雙寫）
- **Phase 2**: backfill — 後台 idempotent 把舊資料 re-encrypt 寫新表（讀舊、不刪舊）
- **Phase 3**: 切讀路徑到新表（保留舊表 read-only fallback）
- **Phase 4**: 一個 release cycle 後 drop 舊表

**Encryption key 連續性**：保留舊 Fernet master key、新表也用同 key（不換 key derivation）— 萬一 rollback 舊 ciphertext 還 decrypt 得回來。

## 5. W11 5 層 defense-in-depth — 取代黑名單的安全設計

黑名單方法（domain whitelist 不允許克隆）有三大缺陷：
1. 維護不過來（新網站每天出生）
2. 擋不到「個人 blog 抄襲」
3. 沒解決法律核心問題（實質性近似）

改用 **5 層 defense-in-depth**：

### L1 — 機器可讀的拒絕信號（W11.4）
抓取前讀：
- `robots.txt` (`User-agent: *` disallow)
- `<meta name="robots" content="noai">` (Anthropic / OpenAI 已 honor)
- `ai.txt` convention
- Cloudflare `ai-bot` rule

任一拒絕 → 直接停。

### L2 — LLM 內容分類器（W11.5）
抓取前用 small LLM (Haiku / Gemini Flash) 分類 target，輸出：
```json
{
  "risk_level": "low | medium | high",
  "reason": "...",
  "recommended_action": "allow | warn | block"
}
```

判斷依據：`© All rights reserved` / 法律 / 醫療 / 金融 / 付費內容 / 含 PII。

### L3 — Output transformation（**最關鍵**，W11.6）

> **永遠不直接複製 bytes**

- 文字內容**全 LLM rewrite** 成 operator 自家 brand voice
- 圖片**不下載**、改用同尺寸 placeholder + alt 文字說明用途
- 只抽 layout primitives（grid / typography / spacing）+ 風格 spec
- 生成物是「**inspired by** layout pattern + your own content」

這層是法律邊界的核心 — 即使 L1+L2 沒擋，L3 保證生成物**不是複製品**。

### L4 — 強制可追溯性（W11.7）
- 生成 workspace `<head>` 自動加 HTML comment `<!-- Layout inspired by https://X. Original at workspace .omnisight/clone-manifest.json -->`
- `.omnisight/clone-manifest.json` 含完整 sha256 of CloneSpec + operator identity + target URL + timestamp
- audit_log 寫對應 row

法律糾紛時可證明誰、何時、從哪裡借鑑、生成了什麼。

### L5 — Per-target rate limit + operator PEP HOLD（W11.8）
- 同 target URL 24h 同 tenant 最多 clone 3 次（防 bulk reproduction）
- 每次 PEP HOLD 顯示 L1+L2 結果 + 強制 operator 確認「我已確認此用途符合 fair use / 我有授權」
- 拒絕：金融 / 醫療 / 法律專屬內容

### 整體效果

5 層擋的是「**著作權實質性近似**」這個真正的法律問題，比黑名單擋大廠網站強很多。即使 target 沒在任何黑名單，L3 的 output transformation 保證生成物是**新創作**。

## 6. AS.7 — 8 視覺層 spec（"Command Bridge Auth Experience"）

OmniSight 自身 auth 8 頁全面重做的視覺基礎。詳細實作在 `docs/design/as-auth-security-shared-library.md`，本節摘要設計意圖。

| 層 | 內容 | 技術 |
|---|---|---|
| 1 | 背景星雲 WebGL fragment shader | ~50 行 GLSL，緩慢漂移 + 三層星空 parallax + 游標 gravity well |
| 2 | Floating glass card | heavy backdrop-blur + corner-brackets + neon glow flicker + idle drift + 3D tilt |
| 3 | Brand wordmark 光弧動畫 | gradient 字 + traveling light 4s 滑過 + breathing pulse |
| 4 | Field 通電 | focus 4 corner brackets snap + 邊框 gradient + scan line |
| 5 | OAuth energy spheres | 圓形 button + provider brand 色 halo + ring-spin charge + click 發 beam |
| 6 | 密碼 Slot Machine | 點 🎲 → 20 column 200ms 瘋狂 cycle → 從左到右 lock + scale flash → 1s 落定 |
| 7 | Warp drive transition | card zoom + 星雲 hyperspace stretch + 新 card 從深處浮出，700ms |
| 8 | 場景化 dramatic | 登入成功 emerald 能量波 / 登入失敗 spring-shake + 紅 lightning / MFA 通過 ✓ 形狀 / 帳號鎖 chill blue tint / 歡迎新用戶 30 粒子 burst |

**Reduce-motion** 全套退化 fallback、**電池感知** 4 級降級（reuse BS.3 motion library 規則）、**a11y** WCAG 2.3.3 合規。

## 7. 抉擇對比

### W11 為何選 5-layer mitigation 而非 vendor whitelist

- whitelist 維護不過來、法律邊界錯位
- 5-layer 是內容層 + 元數據層 + 行為層三維 mitigation，cover 法律核心問題

### AS 為何選 shared lib 而非各自實作

- 雙實作 drift 是必然事故
- OmniSight 自家 dogfood 是品質保證
- 修一處 vulnerability 全 surface 受惠
- 對齊 OmniSight 既有 pattern（audit_log / secret_store / decision_engine 都是 self+template 雙用）

### W14 為何選擴 T2/T3 而非平行新 sandbox

- T2/T3 已 ship 且生產驗過
- BS.4 sidecar pattern 通用
- 不重複發明 ingress / SSO / cgroup 機制
- ~80% reuse / ~20% 新 Vite-specific 邏輯

### AS.7 為何 8 頁全做不縮減

- production 已上線、UI 完整一致是企業客戶 first-impression
- 不重做會留「OAuth/Turnstile backend 上了、UI 沒跟上」的詭異中間態
- 8 頁分工清楚、每頁獨立可 ship 可 rollback

### 平行 vs 串行 — 為何選平行 W11-W16 + AS

- 兩條 track 完全獨立 surface（W = 視覺 / sandbox / clone vs AS = lib / auth flow）
- 0 merge conflict 風險
- Wall time max(11d, 22d) = 22d 比串行 33d 省 11d
- AS 跟 BS.3 motion library 重疊度高（reuse），可一起做 visual foundation 落地驗證

## 8. R 系列風險登記

W11-W16 + AS 引入新風險，登記在 BP 風險表：

| ID | 風險 | 緩解 | Phase |
|---|---|---|---|
| R28 | 動態 CF tunnel ingress credential exhaust（W14 每 preview 一個 subdomain） | 30min idle kill + per-tenant rate limit + CF Access SSO | W14.4-W14.5 |
| R29 | Vite dev server sandbox escape（malicious plugin RCE） | T2 cgroup + 唯讀 docker-socket-proxy + W14.11 規格 | W14.9 |
| R30 | Vite plugin agent 注入 exfiltration via dev server proxy | sandbox network 隔離 + workspace 唯讀 mount | W14.11 |
| R31 | OAuth account takeover via email collision | AS.0.3 強制 password 驗證 BEFORE link | AS.0.3 |
| R32 | OAuth path bypass MFA | AS.0.3 OAuth 後仍要求 MFA 第二因子 | AS.0.3 |
| R33 | Credential refactor data loss | AS.0.4 expand-migrate-contract + key 連續性 | AS.0.4 |
| R34 | Turnstile lock 既有自動化 client | AS.0.5 fail-open 漸進策略 + AS.0.6 bypass list | AS.0.5-6 |
| R35 | W11 著作權侵權訴訟 | W11 5-layer mitigation + L3 output transformation | W11.4-W11.8 |

## 9. Rollout 順序 + Rollback 計畫

### 建議 Sprint 順序

| Sprint | 內容 | Wall time | 累計 | Milestone |
|---|---|---|---|---|
| 1 | BS（前置） | 10d | 10d | Catalog + sidecar 上線 |
| 2 | W11-W16 + AS 平行（兩 track） | 22d | 32d | 對話 UX + auth 重做 |
| 3 | FS | 12d | 44d | Full-stack 生成 |
| 4 | SC | 13.5d | 57.5d | 資安 production-ready |
| 5 | BP | ~5.5w | ~16w | Architecture 深耕 |

### 各 priority rollback 策略

- **W11-W16 rollback**：env knob `OMNISIGHT_W_LOVABLE_ENABLED=false` → frontend 不展示新 chat trigger / 不開 W14 preview / W11-13 endpoint 禁用
- **AS rollback**：env knob `OMNISIGHT_AS_ENABLED=false` → login 走 100% 既有 password flow，全 AS 行為 disabled
- **FS rollback**：撤回 sidecar image，scaffold 退回現有 W6/W7/W8 不附 FS bundle
- **SC rollback**：scanner 全 disabled，不 block 任何 deploy
- 任何 alembic migration 都可 `alembic downgrade <prev>` 回退 — schema 永遠是 add-only

## 10. Open questions

1. **Firecrawl ToS 商用授權** — W11 用 Firecrawl SaaS 大量商用要看條款，可能要 pricing tier。Air-gap host 走 self-host Playwright pipeline。
2. **Vendor OAuth app 自動 provisioning** — Google / Apple / Microsoft / GitHub console **沒有 API 讓你建 OAuth client**（怕被 abuse）。FS.2.3 最多生 step-by-step instructions + 偵測 callback URL 變動自動更新。產品上要誠實「跟 wizard 走、5-10 分鐘 setup」不要承諾「全自動」。
3. **WebGL shader 在低階 device 性能** — AS.7.0 背景星雲在 ~3-5% GPU 持續成本，需驗 mid-tier laptop / mobile FPS ≥ 50。電池感知策略應自動降級。
4. **Turnstile 在大陸地區 access** — Cloudflare 在大陸 CDN 不穩，部分 user 可能 fail。SC.13 fallback chain（Turnstile → reCAPTCHA → hCaptcha）必要。

## 11. References

- BS umbrella ADR：`docs/design/blueprint-v2-implementation-plan.md`（Priority BP 主 ADR）
- BS specific：`docs/design/bs-bootstrap-vertical-aware.md`（待 BS.0.1 寫）
- AS specific：`docs/design/as-auth-security-shared-library.md`（本 batch 並行寫）
- W11 mitigation：`docs/security/w11-clone-mitigation.md`（W11.4-W11.8 落地時寫）
- AS migration discipline：`docs/security/as-migration-discipline.md`（AS.0 落地時寫）
- AS rollout runbook：`docs/operations/as-rollout-and-rollback.md`（AS.8.3 寫入）
- 借鑑來源：`https://github.com/firecrawl/open-lovable`（MIT, 2026-04-25 surveyed）
