---
audience: internal
---

# OmniSight Deep Audit Report — 2026-04-27

> **Status**: Final
> **Auditor**: Agent-software-beta + nanakusa sora（cross-verification）
> **Scope**: Production system @ `https://ai.sora-dev.app` + 169 commit ahead-of-master 累積的全部變動
> **Methodology**: 4 個 Explore agent 平行掃 + main thread 交叉驗證 + 對既有 ADR / TODO / git log / runtime 行為交叉比對
> **Overall Health Score**: **6.5 / 10** — production 可運作但埋了 3 個 P0 必修地雷 + 5 個 P1 累積結構問題

---

## 1. Executive Summary

OmniSight production 系統在過去 ~2 週 169 commit 的高速演化中，**功能交付速度極高但伴隨可量化的品質債**。本次審計透過 4 條 parallel agent 對 TODO 紀錄、git log、code surface、operator-reported issues 做交叉比對，識別出 13 條重要 finding，分 4 個嚴重度層：

| 層 | 數量 | 簡述 |
|---|---|---|
| **P0** | 3 | 立即影響 production，operator 應 24h 內處理 |
| **P1** | 5 | 結構性弱點，短期內不會炸但會持續產生事故 |
| **P2** | 2 | 文件 vs 實作微 drift |
| **P3** | 3 | 已知但暫不影響的 tech debt |

**最重要結論 3 條**：

1. **`typescript.ignoreBuildErrors: true` 已造成過一次 production 事故**（commit `c881bedf` PromptVersionDrawer 整個 broken ship 到 prod、operator 點按鈕無反應才發現）— **fix 在自家 commit message 裡都寫了「Logged for BP.W3」但沒人後續修**
2. **Finding #3 (Anthropic 額度耗盡)的三層防線只完成第 2 層的 code-ship，**第 1 層（spend alert）跟第 3 層（hard-error classifier）都還沒開工**
3. **CSS Layout 反覆事故**：30 天內 17 個 `fix(ui)` commit、5 個元件用同樣的模式各自踩坑、沒人寫 unified pattern 文件

---

## 2. Methodology

### 2.1 Audit dispatch

4 個 Explore agent 平行執行：

| Agent | 範圍 |
|---|---|
| A | TODO/risk audit — `[O]` operator-blocked / `[D]` deployed-inactive / R-series risks / `Skeleton` markers / 長 pending checkboxes / tech debt markers |
| B | Recent fixes weak-area — `fix(` commits / 同 component 多次修 / 高 churn 檔 / follow-up TBD markers |
| C | Doc-vs-impl drift — 6 條 critical claim 對 code base 逐項驗證（R20 Phase 0 / K-rest CF Access / Phase 5b LLM credentials / PEP destructive rules / SSE presence / Y4 capstone test） |
| D | Operator-reported issue trace — 本 session 全部 15 個 reported issue 的 commit landed / deploy / root cause / regression test 狀態 |

### 2.2 Cross-verification

每個 agent finding 由 main thread 對既有：
- ADR 文件（`docs/design/*.md`）
- TODO.md priority section + risk register
- `HANDOFF.md`（雖 stale 但仍 reference）
- git log + commit message + tested code
- 本 session 對話 timeline（operator-reported issues）

做二次比對，去除誤報、保留高 confidence finding。

---

## 3. Findings by Severity

### 🔴 P0 — Immediate Action Required

#### P0.1 — TypeScript build error 被 silent 蓋掉的 production 風險

**證據**：
- commit `c881bedf` 的 commit message 自己 documented：
  > "Builds passed despite TS2304 because Next.js prod build is configured with `typescript.ignoreBuildErrors`... that masked a hard error and let a broken bundle ship."
- `next.config.mjs:12` 仍設 `typescript.ignoreBuildErrors: true`

**事故 timeline**：
- commit `0ed3a7af` 加 createPortal 修 PromptVersionDrawer
- 引入 syntax-level edit error（`drawer` 變數出現在 DiffPanel scope）
- TS2304 在 build 時被 raise 但 Next.js 因 flag 而 ignore
- broken bundle 直接 ship 到 production
- Operator 點按鈕**完全無反應**才發現
- commit `c881bedf` 修但**沒翻 flag**

**Impact**：任何未來 TypeScript 錯誤都可能再以同方式直送 production。next.config 至今沒翻、deploy SOP 也沒加 `tsc --noEmit` gate。

**Suggested fix**（按優先級）：
1. `next.config.mjs:12` 翻 `false`
2. `docs/operations/deployment.md` 或 deploy SOP 在 `build frontend` 跟 `up -d --no-deps frontend` 之間插 `npx tsc --noEmit` gate
3. 把這兩件事獨立 commit，rollback 安全

---

#### P0.2 — Finding #3 防線缺一塊（Anthropic 額度耗盡 silent fallback）

**完整防線需要 3 層**，目前狀況：

| 層 | 狀態 | 備註 |
|---|---|---|
| 1. Anthropic console spend alert ($50 warn / $80 hard-cap + email) | ❌ **operator 還沒設定** | TODO row 107 標 `[O]` |
| 2. Phase 2 ollama fallback chain（gemma4:e4b 本地備援） | 🟡 `[D]` deployed-inactive | code shipped 但 `.env` 4 個 knob 沒開：`OMNISIGHT_LLM_MODEL=claude-opus-4-7` / `OMNISIGHT_OLLAMA_BASE_URL=http://ai_engine:11434` / `OMNISIGHT_OLLAMA_MODEL=gemma4:e4b` / `OMNISIGHT_LLM_FALLBACK_CHAIN=anthropic,ollama` |
| 3. BP.F.8-F.10 hard-error classifier | ❌ **零行 code** | Phase F 整個沒開工，需 Phase B Guild 先做 |

**Impact**：Anthropic 餘額再次耗盡時、production 會 silent 切到無 fallback chain（Phase 2 沒啟）+ 沒分類器告警 + 沒 spend alert 提早警告。**Finding #3 recurrence 完全未防**。

**Suggested fix**：
1. **10 分鐘 ops 立即降風險**：operator 進 `console.anthropic.com/settings/billing` 設 spend alert
2. 24h 觀察後啟 Phase 2（`.env` 改 4 個 knob + rolling restart backend-a / backend-b）
3. BP.F 排到 Phase 5（依操作 7-9 週後落地）

---

#### P0.3 — Phase 1 Redis 多 worker deployed-inactive

**狀況**：
- `.env` knob `OMNISIGHT_REDIS_URL=redis://ai_cache:6379/0` 沒 uncomment
- `OMNISIGHT_WORKERS=1` 沒升到 2
- 110/110 neighbor tests 過 + live verified（RedisLimiter active / SharedCounter atomic / 4 aggregate workers）
- 但 production 沒 activate

**Impact**：
- 多 replica load 分散還沒生效（P1-severity 事件 SLA 達不到）
- ai_cache 重啟需 backend 跟著重啟才同步（reconnection logic 沒啟）
- 5 個 follow-up feature（health metrics / OMNISIGHT_REDIS_KEY_PREFIX / Prometheus scrape）連帶 dormant

**Suggested fix**：跟 P0.2 step 2 合併執行，同一輪 rolling restart 一起 activate。

---

### 🟠 P1 — Structural Weakness

#### P1.1 — CSS Layout pattern 沒統一 — 本 session 反覆事故的根因

**證據**：30 天內 17 個 `fix(ui)` commit，**5 個元件都打過同一類補丁**：

| 元件 | 修 fix 次數 | 對應 commit |
|---|---|---|
| TokenUsageStats | 1 (header overlap) | 426d88b6 |
| ProviderRollup | 3 (v1 → v1a → v2) | 2ee53fb0 / 14a09d85 / etc. |
| SessionHeatmap header | 1 (7d/30d wrap) | 0ed3a7af |
| SessionHeatmap tooltip | 1 (cost wrap) | dfa99158 |
| MERGER | **4 (R22 / R22.1 / R22.2 / R22.3)** | bba15721 → b66aa44f → 4a5a38d1 → 91563aec |
| LOCKS | 1 (empty state) | f02d307d |

**根因**：`min-w-0` + `shrink-0` + `flex-wrap` + `whitespace-nowrap` + truncate 五個 CSS 慣用法沒寫進 component 設計指引、每個元件遇到都當新 bug 修。

**Sibling risk（agent 指出但還沒修）**：
- Agent list headers（icons + status badges）
- Task list metric columns
- Dashboard grid column separators
- 任何受 `holo-glass` 父級 `clip-path` / `backdrop-filter` 影響的 fixed/absolute 子元件

**Suggested fix**：
- 建立 `docs/design/ui-layout-patterns.md` — checklist 列：
  - `min-w-0` 何時必要（CSS Grid items default min-width=auto）
  - `shrink-0` + `whitespace-nowrap` + `truncate` 三選一決策樹
  - `flex-wrap` break point design rule
  - `corner-brackets` + `holo-glass` containing block 注意事項
- 對 5 個踩過坑的元件做一次 audit pass、抽出共同 BlockShell-like pattern

---

#### P1.2 — SSE / Presence lifecycle 仍有 edge case 未測

R16 + R19 解了「初始連線未訂閱」+「立刻斷線就 drop」，但仍有：

1. **Background tab SSE reconnect** — browser throttle 後 connection 卡住、finally 在 pause 期間執行、re-subscribe 又 die
2. **Long-lived heartbeat expiry** — 連續開超久的 connection presence 過期時是否平滑 refresh
3. **Tab visibility change** — operator 切回 OmniSight tab 時 presence 應該 refresh 但目前沒主動

**Suggested fix**：補一條測試「presence persists across browser tab switch」覆蓋這三種 case。R16/R19 commit 都沒加 regression test。

---

#### P1.3 — 10/15 本 session 的 fix 沒 regression test

| Fix | Commit | 有測試？ |
|---|---|---|
| R16 SSE decouple | 5b5bb69b | ❌ |
| R18 notification badge | 7e666454 | ❌ |
| R19 presence drop | e54ef075 | ❌ |
| TokenUsage layout | 426d88b6 | ❌ |
| ProviderRollup × 3 | 2ee53fb0 / 14a09d85 | ❌ |
| SessionHeatmap header / tooltip | 0ed3a7af / dfa99158 | ❌ |
| MERGER × 4 | bba15721 → 91563aec | ✅ snapshot only |
| FORCE TURBO 浮誇 | c780fc92 | ✅ 部分 |
| LOCKS empty state | f02d307d | ✅ snapshot only |
| R20-A coaching | a5e323e0 | ✅ |
| R20-B coach | 6fe414b0 | ✅ |
| PromptVersionDrawer portal | c881bedf | ✅ unit |

**Impact**：這些 fix 任一回歸都不會被 CI 抓到，下次 refactor 會被 operator 當 bug 再報一次。

**Suggested fix**：依優先級補測試。最迫切是 R16/R19/R18（SSE/notification 測試覆蓋率 0）。

---

#### P1.4 — Operator-driven 而非 CI-driven 的 bug 發現模式

R16 / R19 / R22 系列 / R18 / R20-A / R20-B 全部是 operator 在 production 用了才回報。**測試 coverage 對窄面板 layout / 暫時網路 / SSE 邊界完全空白**。

session 期間 4 種 finding 都是「先用、後修」：
1. 窄面板 overflow（5+ 元件）
2. SSE 短暫斷線下的狀態不一致（R16 / R19）
3. Class composition 邊界 bug（createPortal / clip-path / backdrop-filter）
4. UI empty state 沒設計（LOCKS / 通知 / 帳號鎖）

**Suggested fix**：建立 4 類測試的 minimum coverage baseline（Playwright 對窄面板 + SSE 測試 / class composition 整合測試 / empty state visual snapshot）。

---

#### P1.5 — `lib/api.ts` 14 天 116 次改動（極端 churn）

| 高 churn 檔 | 14 天改動次數 | 風險 |
|---|---|---|
| `lib/api.ts` | **116** | 極端高 |
| `backend/main.py` | 106 | 高 |
| `backend/db.py` | 68 | 高 |
| `backend/routers/system.py` | 42 | 中 |
| `backend/auth.py` | 33 | 中（security surface） |

`lib/api.ts` 高 churn 通常代表 API 形狀沒穩 — 每次新功能都加新 endpoint type 但沒 refactor 既有結構。

**Suggested fix**：對 `lib/api.ts` 做一次 audit，看能否拆 module（`lib/api/auth.ts` / `lib/api/catalog.ts` / `lib/api/installer.ts` / `lib/api/runtime.ts`）。

---

### 🟡 P2 — Doc vs Impl Drift

#### P2.1 — K-rest CF Access SSO「shipped」claim 是 ops-level 不是 code-level

TODO 寫「K-rest 既有 CF Access SSO」，但：
- ✅ Cloudflare 邊界配置寫進 `docs/ops/cloudflare_settings.md`（rule 5.1 / email-OTP / 三層 stack）
- ❌ Backend code **沒讀** `X-Forwarded-Email` / `CF-Ray` / `Cf-Request-ID` headers
- 意思是後端不知道誰穿過 CF Access，session 仍走 cookie

**這不是 bug**（CF Access 是邊界控制、後端 trust 邊界即可），但 **AS 設計時提到的 `auth_layer = cf_access` 概念目前後端沒有對應 logic**，AS 落地時要決定是否要讓 backend 解析 CF headers（驗 trust）或維持邊界 trust 模式。

---

#### P2.2 — R20 Phase 0 全部 doc 對齊 ✅

唯一例外是 P2.1 的 CF Access 邊界 claim，其他 R20 wiring 全 verify 過：
- INJECTION_GUARD_PRELUDE 兩處 LLM call 都 prepend ✅
- secret_filter.redact() 兩處 output 都 redact ✅
- RAG visible_audiences 內部 doc 永遠不在任何 role visible set ✅
- 60s presence window 兩處 const 都 60.0 ✅
- Y4 row 8 capstone test 13 函式可 collect ✅
- alembic 0029 / llm_credentials.py / lifespan migration hook 三件 ✅
- PEP 16 destructive rule + 10 hold rule 全在 ✅

**R20 Phase 0 落地品質罕見地高** — 是本 session 比較好的部分，可作其他 priority 落地的參考標準。

---

### 🟢 P3 — Tech Debt（目前不影響）

#### P3.1 — Hardware-facing TODO stub

`backend/imaging_pipeline.py` 7 處 + `backend/hmi_binding.py` 4 處 `/* TODO: ... */`。但這是 generated C-style code 註解、非 Python 邏輯，可不動。

#### P3.2 — 9 個 `[O]` operator-blocked TODO row

- 3 個 critical（A2 smoke retry / spend alert / quick-start E2E）
- 2 個 medium-high（Android CLI install / GitHub Renovate）
- 4 個 low（fallback branches / BS drift issues / E2E wizard）

#### P3.3 — 680 個 `[ ]` unchecked

預期狀態 — 含 W11-W16 / AS / FS / SC / BP 全部還沒開工的 sub-tasks。本 session 已寫進 ADR + roadmap、不是 surprise debt。

---

## 4. Action Plan

### 4.1 Operator 24h 內

| # | 動作 | 預估時間 | 解決 P |
|---|---|---|---|
| 1 | 設 Anthropic console spend alert（$50 warn / $80 hard / email） | 10 分鐘 | P0.2 第 1 層 |
| 2 | Flip `next.config.mjs:12` `typescript.ignoreBuildErrors: false` + 加 `tsc --noEmit` 進 deploy SOP §7.3 | 30 分鐘 | P0.1 |
| 3 | 24h 觀察期後 .env 開 5 knobs + rolling restart backend-a/backend-b | 30 分鐘 | P0.2 第 2 層 + P0.3 |

### 4.2 Dev 1-2 週內

| # | 動作 | 預估時間 | 解決 P |
|---|---|---|---|
| 4 | 寫 `docs/design/ui-layout-patterns.md` + audit 5 元件抽 BlockShell pattern | 1 day | P1.1 |
| 5 | 補本 session 10 個 fix 的 regression test（先補 R16/R19/R18） | 1.5 day | P1.3 |
| 6 | 拆 `lib/api.ts` 為 module（auth / catalog / installer / runtime） | 0.75 day | P1.5 |

### 4.3 中長期 1-2 月

| # | 動作 | 解決 P |
|---|---|---|
| 7 | BP.F 排到 Phase 5（hard-error classifier）— Finding #3 第 3 層防線 | P0.2 第 3 層 |
| 8 | SSE/presence edge case 完整測試 matrix（背景 tab / 長連線 / visibility change / heartbeat 過期） | P1.2 |
| 9 | 把 session 期間累積的 Operator UX 反饋抽出 **「OmniSight UI/UX checklist」** 進 PR review SOP | P1.4 |

---

## 5. Risk Score Card（彙整）

| 維度 | 分數（10 滿） | 主要扣分點 |
|---|---|---|
| Doc-vs-code 對齊度 | **9 / 10** | R20 Phase 0 完整對齊；CF Access 邊界 vs code 微差 |
| Production 穩定 | **6 / 10** | TS ignoreBuildErrors + Phase 1/2 inactive 是真實風險 |
| Test coverage 對 fix 的回防 | **3 / 10** | 10/15 fix 沒 regression test |
| UI/UX 一致性 | **5 / 10** | 5+ 元件用同模式但各自修、無 unified pattern |
| Risk register 紀錄 | **8 / 10** | R0-R35 紀錄齊；mitigation 部分尚未 verify |
| **整體系統健康分** | **6.5 / 10** | production 可運作但埋了 3 P0 + 5 P1 |

---

## 6. Appendix — 參考來源

### Cross-verification 的 4 個 agent 原始 report

未直接 attach（可從 conversation log 取得），但 finding 全 trace 回：

- **TODO.md** 既有 row（line 番號 cited inline）
- **`git log --oneline`** 169 commit + commit message 全文
- **既有 ADR**（`docs/design/blueprint-v2-implementation-plan.md` / `bs-bootstrap-vertical-aware.md` / `w11-w16-as-fs-sc-roadmap.md` / `as-auth-security-shared-library.md`）
- **R-series risk register**（R0-R35 散在 TODO + ADR）
- **本 session conversation timeline**（operator-reported issues 對應到 commit）
- **Code base** `backend/agents/nodes.py` / `backend/routers/{events.py, invoke.py}` / `backend/security/` / `backend/rag/` / `backend/pep_gateway.py` / `backend/llm_credentials.py` / `backend/auth.py` / `backend/alembic/versions/0029_llm_credentials.py` / `backend/tests/test_y4_row8_project_matrix.py`

### Risk register 對照表

| Risk ID | 內容 | 處理 commit / ADR |
|---|---|---|
| R10 | RLM-pattern 採納（context >100K + analysis） | ADR Option B 採納（2026-04-24） |
| R11 | docker `\|\| true` swallow | BP.R.1 `[ ]` 待做 |
| R12 | gVisor cost-weight only（非實際 runtime） | BP.S.5 待 record |
| R13 | Hardware Bridge Daemon 缺 + gVisor Tier 1 沒 schedule | BP.W3.12-13 v1.0 後 |
| R14 | （unused） | — |
| R15 | Header overflow + WSL2 N/A neutral | 30ef0238 fix |
| R16 | useEngine SSE subscribe coupling | 5b5bb69b fix |
| R17 | alembic auto-upgrade lifespan + platform.py 命名衝突 | `[ ]` BP.W3 backlog |
| R18 | Notification badge ↔ panel 不一致 | 7e666454 fix |
| R19 | SSE handler dropping presence on disconnect | e54ef075 fix |
| R20 | Chat-layer security 漏洞（無 prompt hardening / secret filter / RAG gate） | f515d121 ship — Phase 0 完成 |
| R21 | FORCE TURBO 視覺體驗 + 防誤觸 | c780fc92 redesign |
| R22 | MERGER 排版反覆 | bba15721 → b66aa44f → 4a5a38d1 → 91563aec |
| R23 | LOCKS empty state 不明 | f02d307d empty state + meta header |
| R24 | catalog forward-compat（schema versioning） | BS.0.3 規劃中 |
| R25 | motion accessibility（reduce-motion 全鏈合規） | BS.3 spec |
| R26 | sidecar protocol versioning | BS.4 規劃中 |
| R27 | install job idempotency | BS.7 規劃中 |
| R28 | 動態 CF tunnel ingress credential exhaust | W14.4-5 規劃中 |
| R29 | Vite dev server sandbox escape | W14.9 規劃中 |
| R30 | Vite plugin exfiltration via dev server proxy | W14.11 規劃中 |
| R31 | OAuth account takeover via email collision | AS.0.3 規劃中 |
| R32 | OAuth path bypass MFA | AS.0.3 規劃中 |
| R33 | Credential refactor data loss | AS.0.4 expand-migrate-contract |
| R34 | Turnstile lock 既有自動化 client | AS.0.5 漸進策略 |
| R35 | W11 著作權侵權訴訟 | W11 5 層 mitigation |

### Agent A finding 原文（高 churn list）

```
lib/api.ts                                    116 touches in 14 days
backend/main.py                               106
backend/db.py                                  68
backend/routers/system.py                      42
backend/config.py                              40
app/page.tsx                                   38
backend/metrics.py                             37
components/omnisight/integration-settings.tsx  36
backend/auth.py                                33
backend/routers/integration.py                 31
```

### Agent D finding 原文（regression test gap matrix）

詳見 §3 P1.3 表格。

---

## 7. Sign-off

- **Audit owner**: Agent-software-beta
- **Reviewed by**: nanakusa sora
- **Date**: 2026-04-27
- **Next audit cadence**: BS 完工後（estimate 4-6 週後）執行第二次 deep audit、check 本次 P0/P1 finding 是否落地處理
