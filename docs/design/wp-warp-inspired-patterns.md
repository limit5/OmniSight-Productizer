---
audience: architect
status: accepted
date: 2026-04-30
priority: WP — Warp-inspired Patterns（13 phase、distributed across timeline）
related:
  - docs/design/blueprint-v2-implementation-plan.md (BP)
  - docs/design/hd-hardware-design-verification.md (HD)
  - docs/security/ks-multi-tenant-secret-management.md (KS)
  - docs/legal/oss-boundaries.md (license boundary, including inspiration-only tier)
  - TODO.md (Priority WP / Priority Q / Priority Y / HD.19 / BP / W11-W16)
---

# ADR — Priority WP: Warp-inspired Patterns（Block Model / Skills / Diff-Validation / Onboarding / 等 13 模式）

> **One-liner**：把 Warp（modern terminal、Rust、~63 crate、AGPLv3 為主）深度審計後找到的 13 個可借鑑 pattern 落成 OmniSight 的演進路線。**借 pattern 不借 code**（我方棧 TS/React/Python、Warp Rust/Metal、無法移植；採 inspiration-only license boundary）。**Wave-1 五個 phase 必須在 BP 之前 ship**（避免 BP / HD ship 後 retrofit）。

---

## 1. 背景

2026-04-30 Warp repo (`github.com/warpdotdev/warp`) 深度審計、找到 13 個可借鑑 pattern。Warp 是現代 terminal、Rust 寫、AGPLv3 為主（部分 UI crate MIT）、cloud 後端 proprietary（OSS 內只有 client + stub）。

審計核心發現：**Warp 的 design pattern 與 OmniSight 高度同構**，因為兩者都是「結構化人機協作 + AI agent 介入 + 多執行單元組裝」的命令中心類產品。Warp 在 terminal 場景驗證的 pattern（Block model / Skills / Diff-validation / Onboarding intention picker）直接對應 OmniSight 的 multi-agent 場景。

### 1.1 借鑑 vs 不借鑑（License 邊界）

| 借 | 不借 |
|----|------|
| **Design pattern + UX flow + 資料模型形狀** | 任何 Rust source / Metal GPU 渲染 / proprietary cloud |
| **設定的維度組合**（如 sync-scope = Globally/PerPlatform/Never） | literal port |
| **架構分層概念**（如 skills 的 3 scope）| Warp source code 任何片段 vendor |

**License 邊界紀律**：採 `docs/legal/oss-boundaries.md` 新增的**第四級 inspiration-only tier** — 我方實作必須是**獨立寫出的 inspired-by 版本**、嚴禁 vendor 任何 Warp source、嚴禁逐字翻譯 Rust 結構、所有實作 commit 必須能獨立 audit 為「OmniSight 自寫」。

---

## 2. 決策（Decision）

採用 **3-tier 借鑑模型 + 分散式落地**：

### 2.1 三層 Tier

| Tier | phase | 性質 | ROI |
|------|-------|------|-----|
| **Tier 1**（必借）| WP.1 / 2 / 3 / 4 / 5 | 觸碰多 surface、是下游 BP / HD 的 enabler infra | 最高 |
| **Tier 2**（強推）| WP.6 / 7 / 8 / 9 / 10 | 對應既有 priority gap 補強 | 高 |
| **Tier 3**（加分）| WP.11 / 12 / 13 | 既有 surface polish / niche feature | 中 |

### 2.2 分散式落地（不集中、按依賴關係 fold）

| WP phase | 落地位置 | 為何 |
|----------|---------|------|
| **WP.1 Block model**（Tier 1）| **WP-Wave-1 main**（SC↔BP） | BP / HD / W14 / Z 全用、infra primitive |
| **WP.2 Skills loader**（Tier 1）| **WP-Wave-1 main** | BP.B Guild SKILL_HD_* 基礎 |
| **WP.3 Diff-validation cascade**（Tier 1）| **WP-Wave-1 main** | BP / HD agent 改檔通用 |
| **WP.4 Onboarding intention picker**（Tier 1）| **fold 進 W11-W16 Y6 dashboard sub-task** | Y6 已 ship、+1 day 立即受惠 |
| **WP.5 Project-context walker**（Tier 1）| **WP-Wave-1 main** | HD RAG corpus 多檔合併、HD 開工前 ready |
| **WP.6 Settings sync-scope**（Tier 2）| **fold 進 Priority Q（line 954）sub-task** | Q 已存 priority、WP.6 是 Q 核心 schema |
| **WP.7 Feature flag tiered registry**（Tier 2）| **WP-Wave-1 main** | BP / HD / KS rollout 分層 ship 必備 |
| **WP.8 Runbook primitive**（Tier 2）| **fold 進 HD.19 Bring-up Workbench sub-task** | HD bring-up 模板化 |
| **WP.9 shareable_objects table**（Tier 2）| **WP-Wave-1 main**（與 WP.1 配對）| Block model 的 sharing schema |
| **WP.10 BP fleet UI lanes**（Tier 2）| **fold 進 BP dispatch board sub-task** | BP-native UI |
| **WP.11 Command palette polish**（Tier 3）| **opportunistic、任何 idle window** | 不阻塞 |
| **WP.12 CodeMirror ghost-text**（Tier 3）| **opportunistic、與 W 系列 polish 同期** | 不阻塞 |
| **WP.13 Computer-use Actor**（Tier 3）| **fold 進 HD.19 sub-task**（與 WP.8 同 phase）| HD VNC bring-up live UI |

### 2.3 排程定位

```
AS (done) → W11-W16 (進行中、+WP.4 1d) → FS → SC →
              ╔════════════════════════════════════════════════╗
              ║ WP-Wave-1（~2 週 sequential、SC↔BP）           ║
              ║   WP.1 Block model + WP.9 shareable_objects   ║
              ║   WP.2 Skills loader                           ║
              ║   WP.3 Diff-validation cascade                 ║
              ║   WP.5 Project-context walker                  ║
              ║   WP.7 Feature flag tiered registry            ║
              ╚════════════════════════════════════════════════╝
                          ↓
                        BP (含 WP.10 fleet UI lanes 為 sub-task)
                          ↓
                        KS.1 → HD (HD.19 含 WP.8 Runbook + WP.13 Computer-use)
                          ↓
                        [KS.2 / KS.3 commercial-driven]

獨立散落（不影響主路線）：
  • WP.4 Onboarding picker — W11-W16 期間插 1 day
  • WP.6 Settings sync-scope — fold 進 Priority Q
  • WP.11 / WP.12 — opportunistic polish
```

### 2.4 為何 Wave-1 必過 BP 前（最關鍵 insight）

**Tier 1 的 5 個 phase 全是 BP / HD 的 infrastructure dependencies**：

- 沒 WP.1 Block model → BP 多 agent UI 用 ad-hoc message 卡片、HD bring-up 各自 render → 之後加 Block = **改全部 surface**
- 沒 WP.2 Skills loader → BP.B Guild SKILL registry 自寫 hard-code → 之後改 = **重做 registry**
- 沒 WP.3 Diff-validation → BP agent 改檔失敗常 silent corrupt → 之後加 = **既有 BP agent 全 retrofit**
- 沒 WP.5 Project-context walker → R20 RAG 只認單檔 CLAUDE.md → HD 多檔 spec 散落 → retrofit 痛
- 沒 WP.7 Feature flag → BP / HD / KS 各自 ad-hoc ENV knob → 統一 = 改 N priority

省下的 retrofit cost ≥ Wave-1 投入時間 → **schedule-neutral 或略省**。

---

## 3. WP.1 - WP.13 Phase 詳述

### WP.1 Block Data Model + Share/Permalink + Redaction Masks（Tier 1、Wave-1）

**Pattern 來源**：Warp `crates/warp_terminal/src/model/block_id.rs` + `block_index.rs` + persistence schema `blocks` table。

**設計**：每個 agent turn / 命令 / 輸出 / sandbox snapshot / HD finding 都是一個 **addressable Block**：
- `block_id`：stable string、`{session_id}-{seq}` 或 `manual-{uuid}` 格式
- `parent_id`：可選父 Block（樹狀組裝 multi-step agent task）
- `kind`：`agent_turn / command / output / snapshot / finding / runbook_step / chat_message`
- `status`：`pending / running / success / error / cancelled`
- `metadata`：JSONB（model / tokens / cost / tenant / project / session / user）
- `started_at` / `completed_at`：timing
- `redaction_mask`：JSONB（哪些 sub-region 在 share 時要遮）

**Schema**（alembic 0116）：
```sql
CREATE TABLE blocks (
    block_id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES blocks(block_id),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project_id TEXT,
    session_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    payload JSONB,
    metadata JSONB,
    redaction_mask JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_blocks_tenant_session ON blocks(tenant_id, session_id, started_at DESC);
CREATE INDEX idx_blocks_parent ON blocks(parent_id);
```

**Share modal**：右鍵 Block → Share → 勾選 sub-region（command / output / metadata / screenshots）→ 走 WP.9 `shareable_objects` 開 permalink。

**React primitive**：`<Block />` 統一取代既有 message / output 卡片散落實作。

### WP.1.6 Migration Strategy（surface-by-surface）

WP.1 的 rollout 不一次替換所有卡片，而是採 **surface 漸進 + 雙寫期 feature flag + 30 天舊 surface 退回路徑**。每一波只允許一組 owner surface 進入 Block data model；下一波開始前，前一波必須完成 UI snapshot、Block projection、share/redaction smoke 與 rollback 演練。

| Wave | Surface | 切換 flag | 雙寫期行為 | 進下一波 gate |
|------|---------|-----------|------------|---------------|
| 1 | **ORCHESTRATOR / TokenUsageStats** | `wp.block_model.orchestrator_token_usage` | 新 path 寫 `blocks` projection；舊 ad-hoc message / token card state 保留讀取與 render fallback | 24h dogfood，Block wrapper snapshot zero-regression，TokenUsageStats totals 與舊 selector 一致 |
| 2 | **BP** dispatch board / batch progress / fleet lanes | `wp.block_model.bp` | BP run / command / output 生成 Block；舊 BP card props 保留並從同一 payload fan-out | Wave 1 flag 穩定 enabled ≥ 7 天，BP dispatch board snapshot zero-regression |
| 3 | **HD** bring-up workbench / finding rows / runbook steps | `wp.block_model.hd` | HD finding / runbook output 生成 Block；舊 HD finding row renderer 保留 fallback | BP flag 穩定 enabled ≥ 7 天，HD workbench smoke + finding share/redaction round-trip |

**Dual-write invariant**：在任一 wave 的 30 天相容窗內，producer 必須同時維持 Block projection 與舊 surface payload。Block path 失敗時不可吞錯；UI 應立即回到舊 ad-hoc surface，並用 N10 / debug finding 記錄 `block_model_fallback`，讓 reviewer 能看到 dual-write 不一致。

**Feature flag switch**：切換順序固定為 `disabled → dogfood enabled → preview enabled → release enabled`，採 WP.7 registry hot-path read；registry row 缺席時保持舊 surface。WP.1.7 會補 single-knob `OMNISIGHT_WP_BLOCK_MODEL_ENABLED=false` 作全域 kill switch，本節只定義 per-surface strategy，不提前接 env knob。

**30 天退回路徑**：每個 wave 的舊 renderer / payload adapter 從該 wave release-enabled 當天起保留 30 天。30 天內 rollback 只需關閉該 wave flag；30 天後才允許刪除舊 surface fallback，且刪除前必須有 drift guard 證明該 surface 已無 active fallback event。

### WP.2 Skills Loader（`.claude/skills` + `.warp/skills` 共用慣例）

**Pattern 來源**：Warp `crates/ai/src/skills/skill_provider.rs`。

**設計**：Skills = markdown 檔（YAML frontmatter `name` / `description` + 內文）。**3 scope** 分層 + **provider rank precedence**：

| Scope | 路徑 | Precedence |
|-------|------|-----------|
| **Bundled** | `omnisight/agents/skills/` （ship 進 image） | 最低 |
| **Home** | `~/.claude/skills/` 或 `~/.omnisight/skills/` | 中 |
| **Project** | `./<repo>/.claude/skills/` 或 `./<repo>/.omnisight/skills/` | 最高 |

Loader 行為：
- 啟動時掃 3 scope、merge 成 SKILL registry
- 高 precedence 覆蓋低（同 name skill）
- FS-watch project scope、改檔即時 reload
- Skills 在 command palette + chat `@skill-name` 兩處可叫用
- BP.B Guild 既有 SKILL_HD_* 改走新 loader、不 hard-code

**與 Claude Code 共用慣例**：直接用 `.claude/skills/` 路徑（OmniSight 與 Claude Code 共用相同 convention 進一步降低 onboarding 摩擦）。

### WP.3 Diff-Validation Cascade（4-tier fuzzy ladder）

**Pattern 來源**：Warp `crates/ai/src/diff_validation/mod.rs`（41 KB pipeline）。

**設計**：當 agent 提議改檔（patch / replace / insert）、走 4 層 fallback 確保改檔成功而非 silent corrupt：

```
1. Exact match           — 字面比對 old_string、找到 → 改
2. Indent-agnostic       — 容忍 tab/space 差異
3. Prefix-tail rescue    — 前 N 字 + 後 N 字皆 match → 中段差異視為可接受
4. Jaro-Winkler ≥ 0.9    — 模糊相似度、最後一道
```

每層帶 confidence score、進 N10 ledger。失敗時不 silent fail、走 explicit error + agent self-correction loop。

**整合**：
- BP agent file edit tool → 走 cascade
- HD bring-up agent edit DTS / Yocto recipe → 走 cascade
- W14 sandbox edit → 走 cascade
- 既有 Edit tool wrapper 統一升級

### WP.4 Onboarding Intention Picker（Y6 first-run）

**Pattern 來源**：Warp `crates/onboarding/src/agent_onboarding_view.rs`、7-slide flow 中的 "Intention slide"。

**設計**：First-run modal（Y6 dashboard 進入前）問：

> 「你來這做什麼？」
> □ HD verification（嵌入式硬體 / schematic / sensor swap）
> □ Multi-agent dispatch（BP / Guild / agent fleet）
> □ Web app generation（W11-W16 / FS / SC）
> □ Sandbox dev（W14 live preview）
> □ Just exploring

依答案：
- 預設 Y6 tile 排列
- 預載 sample data（HD 範例 schematic / BP 範例 agent task / W14 範例 sandbox）
- 引導到對應 priority 的 quickstart
- skip 可走、不強制

**Time-to-first-value**：從現況「進系統不知道從哪開始」 → 「3 click 內看到屬於我的 starter content」。

### WP.5 Project-Context Multi-Rule Walker

**Pattern 來源**：Warp `crates/ai/src/project_context/model.rs`。

**設計**：升級 OmniSight 既有 CLAUDE.md 處理：
- 從 single-file 升 **multi-file**：`CLAUDE.md` + `AGENTS.md` + `OMNISIGHT.md` + `WARP.md`
- 從 current-dir 升 **parent-walk**（最多 3 層父目錄）
- **FS-watched**：檔變動即時重新 merge
- **Merge precedence**：current dir > 父目錄、project-specific > generic
- 上傳到 R20 Phase 0 RAG 走相同 walker、HD datasheet / sensor spec / errata 全自動 ingest

### WP.6 Settings Sync-Scope（Globally / PerPlatform / Never）

**Pattern 來源**：Warp `crates/settings/src/lib.rs` + `define_settings_group!` macro。

**設計**：每個 setting 標：
- `scope: tenant | user | device`
- `sync: globally | per_platform | never`
- `supported_platforms: [...]`

範例：
- `theme = globally` （任一 device 改、所有 device 同步）
- `hardware_bench_target = never` （HD bring-up workbench 連的 device，per-machine）
- `notification_sound = per_platform` （MacOS / Windows / Linux 各自）

**對應 priority**：直接 fold 進 **Priority Q（Multi-Device Parity, line 954）**作為 schema 核心。

### WP.7 Feature Flag Tiered Registry

**Pattern 來源**：Warp `crates/warp_features/src/lib.rs`（41 KB、~250 flag）。

**設計**：5 tier flag：

| Tier | 對象 | 範例 |
|------|------|------|
| `DEBUG` | dev-only | 開發中 feature 測試 |
| `DOGFOOD` | 內部 + early-access | 我方 dogfood、~50 flag |
| `PREVIEW` | 外部 tester | beta program 客戶 |
| `RELEASE` | GA | 全客戶 |
| `RUNTIME` | server-pushed | 不重 deploy 改 |

Resolution：test-override → user preference → global state → default。Atomic 讀（hot path 友好）。

**對 OmniSight 影響**：BP / HD / KS 各 phase 走 `OMNISIGHT_BP_*` / `OMNISIGHT_HD_*` / `OMNISIGHT_KS_*` ENV knob 散落。WP.7 提供統一 registry + tier、漸進 ship 安全。

### WP.8 Runbook Primitive + "Save Block as Runbook"

**Pattern 來源**：Warp Workflows（`workflow_panes` schema + 獨立 `warpdotdev/workflows` repo YAML）。

**設計**：
- **Runbook = YAML**（name / description / params with type+default+description / steps[] / tags / source_url）
- 三 scope（同 WP.2 Skills）：bundled / home / project
- 任何 Block 都有「Save as Runbook」按鈕、自動推導 params
- Runbook 執行時 prompt user 填 params、走 WP.1 Block 鏈執行

**對應 priority**：fold 進 **HD.19 Bring-up Workbench**：bring-up checklist 自動產 runbook、客戶可 save / re-run / share。

### WP.9 `shareable_objects` Generic Table

**Pattern 來源**：Warp persistence `shareable_objects` table。

**設計**：一張 table 管所有可 share 物件的 ACL / permalink / expiry：

```sql
CREATE TABLE shareable_objects (
    share_id TEXT PRIMARY KEY,           -- public-facing permalink slug
    object_kind TEXT NOT NULL,           -- 'block' / 'runbook' / 'notebook' / 'agent_transcript'
    object_id TEXT NOT NULL,             -- foreign key to blocks/runbooks/etc
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    visibility TEXT NOT NULL,            -- 'private' / 'team' / 'tenant' / 'public'
    expires_at TIMESTAMPTZ,
    redaction_applied JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**配對 WP.1 Block model**：Block 的 share button → 進 `shareable_objects`，走統一 ACL 模型。

### WP.10 BP Fleet UI Lanes（Active / Scheduled / Ambient / History）

**Pattern 來源**：Warp `AgentManagementView` + `AmbientAgentRTC` 系列 flag。

**設計**：BP dispatch board 4 lane：
- **Active**：跑中 agent
- **Scheduled**：排程未發
- **Ambient**：long-running background agent
- **History**：完工 / 失敗 archive

每 agent click 進詳情 panel：執行 timeline / token usage / tool calls / output Blocks（WP.1）/ revoke 按鈕。

**對應 priority**：fold 進 **BP dispatch board** sub-task。

### WP.11 Command Palette Smart-Case + Wildcard + Matched-Indices

**Pattern 來源**：Warp `crates/fuzzy_match/src/lib.rs`。

**設計**：升級既有 `components/omnisight/command-palette.tsx`：
- **Smart-case**：query 全小寫 → case-insensitive、有大寫 → case-sensitive
- **Wildcard fast-path**：`*.tsx` 走 substring shortcut、`agent*config` 走 glob
- **Matched-indices highlight**：result row 內 match 字 highlight（用 `cmdk` 或 `fuse.js` 加 indices output）

### WP.12 CodeMirror Ghost-Text Suggestion

**Pattern 來源**：Warp custom editor `crates/editor/src/multiline.rs` + AI suggestion flag。

**設計**：OmniSight chat 輸入升級成 CodeMirror 6 + ghost-text suggestion：
- AI 推薦下一段（行內 ghost-text）
- Tab 接受全部 / Ctrl-→ 接受一字 / Esc 取消 / Alt-] 切換 alternative
- 不寫 custom editor、用 CodeMirror 6 的 ghost-text plugin

### WP.13 Computer-Use Actor for HD Bring-up VNC

**Pattern 來源**：Warp `crates/computer_use/src/lib.rs`（Anthropic-style Actor trait）。

**設計**：HD bring-up 場景客戶可能要 live UI 互動（client device 跑 GUI 或 boot loader live view）。Actor 提供：
- 截圖（RGBA、region constraint）
- 滑鼠點擊（pixel-budget 限制）
- 鍵盤輸入
- 跨平台（X11 / Wayland / macOS / Windows）

**對應 priority**：fold 進 **HD.19 Bring-up Workbench**、與 W14 Live Sandbox Preview 整合。

---

## 4. License Boundary — Inspiration-Only Tier（第四級新增）

`docs/legal/oss-boundaries.md` 既有三層（直 link / LGPL 動態 link / GPL subprocess / AGPL Docker sidecar）之外、新增**第四級 inspiration-only**：

| Tier | 整合方式 | 邊界執行 | 範例 |
|------|---------|---------|------|
| **Inspiration-only**（新）| 借 design pattern + UX flow + 資料模型形狀、嚴禁 vendor / port any source | 我方實作 commit 必須能獨立 audit 為「自寫」、PR description 註明「inspired by Warp X、無 source 接觸」、CI license scanner 不會誤報（無 Warp source 進 tree）| Warp（AGPLv3） / 任何 viral copyleft 純 idea 借鑑 |

**強制紀律**：
- WP-* 所有 commit message 必須註明「inspired by Warp `crates/<...>`、independently implemented」
- code review 時 reviewer 對照 Warp 上游 source 確認**結構不雷同字面、概念對齊即可**
- 嚴禁 LLM 以 Warp source 為輸入 generate 我方 code（防 inadvertent transcription）
- WP-* PR 進 main 前 legal spot-check（季度抽 5 PR）

---

## 5. 排程影響分析

| 項目 | 增加時程 | 抵銷 / 節省 |
|------|---------|-----------|
| WP-Wave-1 sequential 插 SC↔BP（WP.1/2/3/5/7/9）| **+2 週** | BP 不需 retrofit Block / Skills / Diff-validation = **省 ~3-4 週 BP retrofit** |
| WP.4 inside W11-W16 | +1 day（吸收）| 即時 onboarding ROI |
| WP.6 inside Priority Q | 0（Q 自身 schema 改造）| Q 直接受惠 |
| WP.8 inside HD.19 | +1 週（HD 內擴）| HD bring-up 模板化 |
| WP.10 inside BP dispatch | +1 週（BP 內擴）| BP UI native ship |
| WP.13 inside HD.19 | +1 週（HD 內擴）| HD VNC live UI |
| WP.11 / WP.12 opportunistic | 0-2 day | polish |
| **淨影響** | **+2 週插 + 3 週分散** | **省 ~3-4 週 BP retrofit** |

**結論**：schedule-neutral 或略省。Tier 1+2 早做的真實 ROI 來自避免 retrofit。

---

## 6. Migration / Schema

| Migration | 內容 | 對應 phase |
|-----------|------|-----------|
| 0116 | `blocks` 表 + indexes | WP.1 |
| 0117 | `shareable_objects` 表 | WP.9 |
| 0118 | `feature_flags` 表 + audit log | WP.7 |
| 0119 | `runbooks` 表 + `runbook_steps` 子表 | WP.8 |
| 0120 | `skills` registry 持久化（optional、loader 直接讀檔即可） | WP.2 |
| 0121-0125 | 預留 — 各 phase schema evolution | 未來 |

合計 KS 之後 **WP 0116-0125（10 slots 預留）**。

### 6.1 Single Knob

Three independent knobs：
- `OMNISIGHT_WP_BLOCK_MODEL_ENABLED=false` → WP.1 / WP.9 退回 ad-hoc message 卡片
- `OMNISIGHT_WP_SKILLS_LOADER_ENABLED=false` → WP.2 退回 hard-code SKILL registry
- `OMNISIGHT_WP_DIFF_VALIDATION_ENABLED=false` → WP.3 退回 exact-match only

WP.4 / 5 / 6 / 7 / 8 / 10 / 11 / 12 / 13 各自既有 priority knob 控制（fold 進去之後繼承）。

---

## 7. R-series 風險（R58-R62）

- **R58 Block model migration risk**：既有 message / output / finding 卡片散落各 surface，遷移到統一 Block primitive 過程中 UI 可能斷裂。**Mitigation**：分 surface 漸進遷移（先 ORCHESTRATOR → 後 BP → 後 HD）、雙寫期 feature flag 切換、舊 surface 退回路徑保留 30 天。
- **R59 Skills loader scope creep**：3 scope（bundled / home / project）+ 多檔合併容易讓 skill 衝突排序不可預測。**Mitigation**：強制 `name` 唯一、衝突時高 precedence wins + WARN log；UI 顯示哪 scope 載入；CLI `omnisight skills resolve <name>` 印 effective skill source。
- **R60 Diff-validation false-positive**：Jaro-Winkler 0.9 fallback 太寬鬆 → agent 改錯位置 silent pass。**Mitigation**：Jaro-Winkler 觸發時必 log + N10 ledger、人類 confidence threshold 可調、HD bring-up agent 改 DTS / Yocto recipe 預設 strict mode（不走 0.9 fallback、改 0.95）。
- **R61 Project-context walker 噪音**：parent walk 拉到上層共用 CLAUDE.md / AGENTS.md 可能載入無關內容污染 prompt。**Mitigation**：每檔 max 5 KB cap、總和 max 50 KB cap、UI 顯示載入哪些檔 + 各檔大小、operator 可 ignore 特定檔。
- **R62 Feature flag 爆炸**：250+ flag 在 Warp 已是事實、OmniSight 需控制不重蹈。**Mitigation**：flag 強制過期（`expires_at` field）、過期未清理 → CI fail；季度 flag review 進 N10。

---

## 8. Test Strategy

- **Block model**：每 surface（ORCHESTRATOR / BP / HD / W14）migration 前後 UI snapshot test、確保視覺零回歸
- **Skills loader**：3 scope + 衝突 + FS watch + reload 各自 unit test
- **Diff-validation**：4-tier ladder 各層 unit test + 50+ scenario regression（正改 / 誤改 / 邊界）
- **Onboarding picker**：5 intention × 各自 seed data 完整 → 切換驗
- **Project-context walker**：parent walk + multi-file merge + size cap 各自 test
- **Settings sync-scope**：fold 進 Q 統一 test 套件
- **Feature flag**：5 tier resolution + atomic 讀 + push reload + expire enforcement
- **Runbook**：parameter resolution + fold 進 HD.19 test
- **Shareable_objects**：ACL + permalink + expiry + redaction
- **Fleet UI**：4 lane + agent detail + revoke
- **Compat regression**：三 knob disable → 既有 surface 0 回歸

---

## 9. Open Questions

1. **`.claude/skills/` 路徑慣例 vs `.omnisight/skills/`**：與 Claude Code 共用 `.claude/` 目錄好處是 onboarding 摩擦低、壞處是 namespace 衝突。傾向**雙路徑都掃**、優先級 `.omnisight/` > `.claude/`、衝突 WARN。決策推遲到 WP.2 開工。
2. **Block model 是否取代既有 `event_log`**：兩者形狀類似（時序 + metadata），但 event_log 偏低層 audit、Block 偏高層 user-facing。**傾向並存**、Block 引用 event_log id。決策推遲到 WP.1 設計時。
3. **Runbook 與既有 `workflow_runs` table 的關係**：workflow_runs 是 BP runtime 紀錄、runbook 是 user-defined template。**傾向兩 table**、runbook → workflow_run 一對多。決策推遲到 WP.8。
4. **Computer-use Actor 是否真的需要**：HD bring-up VNC 是 niche、需求是否大到值得投入。**先觀察 HD GA 後客戶反饋、有需求才做**。WP.13 標 optional。
5. **Feature flag UI**：operator 在 settings 看 flag 是否有 toggle？或只 read-only inspect？**傾向 read-only inspect + admin role 才能改**。決策推遲到 WP.7。

---

## 10. 參考文件

- `docs/design/blueprint-v2-implementation-plan.md`（BP 主 ADR、WP.10 fleet UI lanes 落地點）
- `docs/design/hd-hardware-design-verification.md`（HD ADR、WP.8 + WP.13 落地點）
- `docs/security/ks-multi-tenant-secret-management.md`（KS、WP 與 KS.1 都在 BP 後執行）
- `docs/legal/oss-boundaries.md`（OSS license 邊界、WP 新增 inspiration-only tier）
- `TODO.md` Priority WP / Priority Q / Priority Y / HD.19 / BP dispatch board / W11-W16

**Warp 上游 reference paths**（only for inspiration、do not vendor）：
- `crates/warp_terminal/src/model/block_id.rs` / `block_index.rs` / `indexing.rs`
- `crates/persistence/src/{schema.rs, model.rs}`
- `crates/ai/src/skills/{skill_provider.rs, parse_skill.rs}`
- `crates/ai/src/diff_validation/mod.rs`
- `crates/ai/src/project_context/model.rs`
- `crates/onboarding/src/{lib.rs, agent_onboarding_view.rs}`
- `crates/settings/src/{lib.rs, macros.rs}`
- `crates/warp_features/src/lib.rs`
- `crates/fuzzy_match/src/lib.rs`
- `crates/computer_use/src/lib.rs`

---

## 11. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-30
- **Status**: Accepted（Wave-1 排程在 SC 完工後、BP 開工前；Tier 2/3 分散 fold 進對應 priority）
- **Next review**: SC 完工前、WP-Wave-1 開工前
