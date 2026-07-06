# Sora → 自動 Supervisor Roadmap（含模型自動路由）

> 狀態：草案 v1 · 2026-07-06 · 關聯 OP-2530（Sora 角色線）
> 定位：把「對話主控 Sora」逐步進化成「自動 supervisor」的落地藍圖。
> 讀者：實作者 + operator。每個 Phase 皆可獨立交付、獨立驗收。

---

## 0. 背景與願景

Sora 是 OmniSight 的 orchestrator（會長 / 主控），目前走 **Anthropic API**
（`claude-sonnet-4-5`），能力是：對話問答 + 依權限 RAG 文件 + `create_task`
（開 **GATED** runner Story）。

系統的北極星是把「手動介入層」自動化——也就是今天由 **Claude-in-tmux（人工）**
扮演的 **SUPERVISOR**：部署、救卡住的 runner、除錯、剝 stale label、開 release-train
……這些 Sora 現在都不會做。本文件描述如何在**不打壞安全邊界**的前提下，讓 Sora
逐步接手這一層。

**一句話架構解**：
> **Sora ＝ 小粒度推理（規劃/拆解）＋ 可驗證的維運操作 ＋ 記憶驅動學習；
> 大粒度執行一律委派給訂閱制 worker。**

---

## 1. 核心設計原則（所有 Phase 共用）

1. **小粒度 + 委派**：Sora 自己的工具只做「小而可驗證」的事；任何大粒度編碼
   一律 `create_task` 丟給訂閱 worker（worker 的強項）。不要逼 API 路徑去追平
   CLI 的大粒度執行——那是跟架構對打。
2. **工具合約**：每把工具都要
   - **原子 + 冪等**（重試安全，沿用 runner idempotency key 基礎）；
   - **結構化回傳**（`{success, data, next_hint}`，非 raw stdout）；
   - **動作↔驗證成對**（每個動作工具配一個驗證查詢，graph 強制
     `act → verify → (retry|escalate)`）——這是降低失敗率**最大的槓桿**。
3. **非破壞優先、破壞性 GATED**：自動可做的僅限**非破壞性**動作（做不出災難）；
   破壞性動作（deploy / force-push / 改 prod 設定）Sora 只能**提案 + diff**，
   由 operator 一鍵放行才執行——與 `create_task` 同哲學。
4. **記憶驅動學習**：每個驗證通過的介入 → 存進 L3 episodic memory；下次類似問題
   自動召回。驗證迴圈負責「當步止血」、記憶負責「跨次不重犯」，兩者合起來讓
   可靠度**複利成長**。
5. **兩次同錯即升級**（CLAUDE.md 既有規則）：重試有界，第 2 次同錯就交還人類。

---

## 2. 現況盤點（已有的積木，不用重造）

| 能力 | 現況 | 位置 |
|---|---|---|
| 對話 RAG（文件，依權限分級）| ✅ 已在 Sora 路徑 | `nodes.py` `_rag.retrieve(role=user_role)` |
| `create_task`（GATED 開票）| ✅ 已綁 Sora | `ORCHESTRATION_TOOLS=[create_task]` |
| per-request 模型覆寫 | ✅ 已 thread | `_get_llm(model_name=...)`，`provider:model` 語法 |
| 複雜度自動路由器 | ✅ 存在（未接 Sora）| `edit_complexity_router.py`：小=Haiku/中=Sonnet/大=Opus |
| L2 向量記憶（蒸餾知識）| ✅ 存在，scope=`architect_guild` | `skill_memory.py` + `embedding_chunks` |
| L3 episodic memory | ✅ 工具存在（未綁 Sora）| `EPISODIC_TOOLS=[search_past_solutions, save_solution]` |
| Gated 拆解 pipeline | ✅ SOP 存在 | `docs/sop/epic-decomposition-and-ticket-filing-sop.md` |
| provider 選擇器（UI）| ✅ 骨架在（`onSwitchProvider(provider, model?)`）| `orchestrator-ai.tsx` |
| runner 遙測（成功/事故）| ✅ 有資料 | `runner_metrics` / `runner_incidents` |

> 重點：**大多是「綁定 + scope + 包裝」的工作，不是「能力缺口」。**

---

## 3. 分階段 Roadmap（總表）

| Phase | 名稱 | 風險 | 交付重點 |
|---|---|---|---|
| **P1** | 視力 + 記憶 | 零（唯讀）| 3–5 把唯讀觀察工具 + 綁 L3 episodic memory 到 Sora |
| **P2** | **模型自動路由 + 手動覆寫** | 低 | 登錄模型 + `classify_prompt` 接進 Sora + 面板 model 下拉 |
| **P3** | 安全動作 + 驗證迴圈 | 低（可逆）| 可逆動作工具，每把配驗證；`act→verify→(retry\|escalate)` |
| **P4** | 規劃（走 SOP）| 中 | 接 epic-decomposition SOP；階層式規劃；重規劃可再委派 |
| **P5** | GATED 危險動作 | 高（GATED）| deploy/force-promote/abandon/restart：提案+diff，operator 放行 |

順序理由：先讓 Sora **看得見**（P1，零風險）→ 讓她**用對腦**（P2，低風險、馬上提升
品質/降成本）→ 才給她**動手**（P3 可逆 → P5 GATED）。

---

## 4. Phase 詳述

### P1 — 視力 + 記憶（唯讀，零風險）
- **觀察工具（唯讀，結構化回傳）**：
  - `fleet_status`：6 個 runner 的 active/idle、當前 ticket、耗時。
  - `stuck_ticket_scan`：偵測卡票（assignee 未清、stale `claim:*`、stoploss、
    無 Gerrit change 的 In-Progress 殭屍）。
  - `quota_circuit_status`：`provider_quota_state` 四條線的斷路器狀態。
  - `change_status`：某 Gerrit change / CI pipeline 的當前狀態。
  - `blocked_why`：某票為何 block（blockedBy 圖 + label 診斷）。
- **記憶**：把 `EPISODIC_TOOLS`（`search_past_solutions` / `save_solution`）綁給 Sora；
  新增 **supervisor scope**，讓她的維運心得與 worker 編碼技能隔離。
- 驗收：Sora 能在對話中「看見車隊」並召回過去類似狀況，**不做任何寫入**。

### P2 — 模型自動路由 + 手動覆寫（本文件重點，見 §5–§7）
- 把 `classify_prompt` 接進 Sora 對話前置，按**意圖/複雜度**選模型。
- 面板加 model 下拉：`auto`（預設）或釘選特定模型（覆蓋自動）。
- 登錄 Fable 5 / Opus 4.8 / Haiku 4.6 到 pricing / context-limit / mapping。
- 驗收：純聊天走 Haiku、預設 Sonnet、規劃/重構自動升 Opus/Fable；使用者可釘選。

### P3 — 安全動作 + 驗證迴圈（可逆，低風險）
- **可逆動作工具**（每把冪等 + 配驗證）：
  - `requeue_ticket`（清 assignee）→ verify：票回到 pickable。
  - `strip_stale_labels`（剝 `claim:*` / `stoploss`）→ verify：label 已清。
  - `convert_bug_to_story` → verify：issuetype=Story。
  - `comment_ticket` → verify：留言存在。
- **graph 強制** `act → verify → (retry≤1 | escalate)`；驗證通過即 `save_solution`。
- 驗收：Sora 能自動救「非破壞性」的卡票，且每次動作都有驗證軌跡。

### P4 — 規劃（走既有 gated SOP）
- Sora 接到大/模糊任務 → 跑 **epic-decomposition SOP**（拆解→稽核→blind-test→
  開 GATED 票），而非自由發揮。
- **階層式規劃**：高層規劃 → 每分支各自再規劃（每次呼叫 context 有界）；
  RAG 只取相關 context。
- **重規劃可再委派**：若拆解本身很重，Sora 可把「規劃任務」丟給 architect guild
  worker（訂閱）——規劃也沒有天花板。
- 驗收：一個 epic 能被 Sora 拆成 runner-safe 的小票、不 goal-drift、operator 審後派工。

### P5 — GATED 危險動作
- `deploy` / `force_promote` / `abandon_change` / `restart_service`：Sora 產出
  **提案 + diff/計畫**，經 operator 一鍵放行才執行。沿用 `create_task` 的 GATED 模式。
- 驗收：Sora 能「提議」一次部署或救援，但**永遠**要人放行。

---

## 5. 模型登錄清單（Registration Checklist）

每個模型要成為「一等公民」（可路由、可計費、context 不爆），需在三處登錄：
`config/llm_pricing.yaml`（input/output 價）、`configs/context_window_limits.yaml`
（context 上限）、`configs/model_mapping.yaml`（provider:model 映射）。

| 顯示名 | model id | 定位 | Input/Output（/1M）| 登錄狀態 |
|---|---|---|---|---|
| Haiku 4.6 | `claude-haiku-4-6` | 純聊天/輕量 | ⚠️ **待確認**（現有 Haiku 4.5＝$1/$5）| ❌ 需登錄（含確認 id 是否存在，否則退回 4.5）|
| Sonnet 5 | `claude-sonnet-4-6`（最新 Sonnet）| **預設** | $3 / $15 | ✅ 已在 pricing（sonnet tier）|
| Opus 4.8 | `claude-opus-4-8` | 重型規劃/重構 | ⚠️ **待確認**（現有 Opus 4.7＝$5/$25，暫代）| ❌ 需登錄 |
| Fable 5 | `claude-fable-5` | 深度推理（旗艦）| ⚠️ **無權威定價** | ❌ 需登錄（先向官方定價確認，勿臆造）|

> 誠實註記：Fable 5 / Opus 4.8 / Haiku 4.6 的**定價與 context 上限我沒有權威來源**，
> 登錄前**必須**以官方數字為準；上表暫代值僅供估算，勿當帳單依據。
> Sonnet「5」對應目前最新的 `claude-sonnet-4-6`（命名以實際可用 id 為準）。

---

## 6. 路由規則表（意圖 → 模型）

原則：**按意圖分級，不只按長度**。用 `edit_complexity_router.classify_prompt`
（純啟發式、零額外 LLM 呼叫）產生 signals，映射如下：

| 意圖 / signal | 複雜度 | 路由到 | 理由 |
|---|---|---|---|
| 純問答、寒暄、狀態查詢、**無工具需求** | small | **Haiku 4.6** | 便宜 + 快（互動要快）|
| 一般對話、開單意圖（`create_task`）、單步維運 | medium | **Sonnet 5** | 品質/成本平衡；工具呼叫穩 |
| 深度規劃、epic 拆解、重構、跨檔設計、多步推理 | large | **Opus 4.8 / Fable 5** | 最強推理留給**拆解品質** |

規則細節：
- **有工具/開票意圖 → 至少 medium**（小模型工具呼叫可靠度較低，勿降到 Haiku）。
- **planning/refactor/decompose 關鍵詞或 signals → large**。
- **升不降优先**：不確定時偏保守（往上一級），品質重於省成本。
- **手動覆寫優先於自動**：使用者釘選的模型永遠贏過路由結果。

---

## 7. UI 下拉規格（面板 model 選擇器）

- 位置：orchestrator 面板現有 provider 選擇器旁（`onSwitchProvider(provider, model?)`
  回呼已支援 model 參數，`providers[].models[]` 已帶清單）。
- 選項：
  - **`Auto`（預設）** — 顯示自動路由結果（例：`Auto · Sonnet 5`），hover 顯示
    路由理由（`classify_prompt` 的 reasons）。
  - **釘選** — `Haiku 4.6` / `Sonnet 5` / `Opus 4.8` / `Fable 5`（僅列**已登錄且
    configured** 的模型；未登錄者不出現）。
- 行為：
  - 釘選 → 該 session 覆寫自動路由，覆寫狀態持久化（localStorage，同 UI dial）。
  - 顯示**每則回覆實際用了哪個模型**（小標籤），透明化成本/品質。
  - 若釘選了一個「未 configured」的模型 → 降級提示 + 回退 Auto（fail-open）。

---

## 8. 安全模型（貫穿全程）

- **自動只做非破壞性**：P1 觀察（唯讀）、P2 選模型、P3 可逆動作、P4 開 GATED 票。
- **破壞性一律 GATED**：P5 的 deploy/force-push/restart，提案 + diff，operator 放行。
- **每個動作都有驗證軌跡**：`act → verify`，失敗兩次升級人類。
- **記憶隔離**：supervisor 記憶與 worker 技能記憶分 scope，且 tenant-scoped。
- **模型覆寫不繞過 GATED**：換哪個模型都不改變「破壞性動作要人放行」。

---

## 9. 開放問題 / 待決

1. Fable 5 / Opus 4.8 / Haiku 4.6 的**官方定價與 context 上限**（登錄前必須確認）。
2. Haiku「4.6」的 model id 是否存在；若無，純聊天檔退回 Haiku 4.5。
3. 路由 signals 的門檻校準（哪些關鍵詞/長度算 large）——上線後用實際對話回饋校準。
4. supervisor L3 記憶的**寫入權**：哪些介入結果值得存、由誰判定「驗證通過」。
5. P5 的 operator 放行 UX（一鍵放行 + diff 預覽）要不要沿用 `create_task` 的 GATED UI。

---

## 附錄：與既有系統的關聯
- 對話路徑：`backend/agents/nodes.py`（persona、RAG、tool 綁定）。
- 工具清單：`backend/agents/tools.py`（`ORCHESTRATION_TOOLS` / `EPISODIC_TOOLS` /
  `MEMORY_TOOLS` / `DEPLOY_TOOLS` / `PLATFORM_TOOLS`）。
- 模型路由：`backend/edit_complexity_router.py`、`backend/agents/llm.py`。
- 定價 / 上限 / 映射：`config/llm_pricing.yaml`、`configs/context_window_limits.yaml`、
  `configs/model_mapping.yaml`。
- 拆解 SOP：`docs/sop/epic-decomposition-and-ticket-filing-sop.md`。
- 記憶：`backend/agents/skill_memory.py`（L2）、`EPISODIC_TOOLS`（L3）、`embedding_chunks`。
