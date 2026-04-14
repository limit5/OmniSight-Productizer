# HANDOFF.md — OmniSight Productizer 開發交接文件

> 撰寫時間：2026-04-14（Phase 42-46 深度審計後修復進行中）
> 最後 commit：`8d46e8b` (master) → audit-fix batches in progress
> 工作目錄狀態：audit-fix batches 1-N 進行中

## Audit-Fix 進度（Phase 42-46 深度審計後續）
- 第二輪審計總計 ~85 個問題（13 真 CRITICAL + 4 新 CRITICAL + 21 HIGH + ...）
- **Batch 1（完成）**：Security & path-traversal — C3/C4/C5/C6/C8/C9/C10/C12/M14/N2
  - Jenkins/GitLab token 改走 `curl -K -` stdin，不再經 argv（`ps` 不可見）
  - Gerrit webhook 簽名驗證提前到 payload parse 之前，並做 1MB body 上限
  - `git_credentials.yaml` 路徑限制在 `configs/` 與 `~/.config/omnisight/`
  - Auto-fix `_resolve_under_workspace()` + symlink 拒絕 + git-lock 60s stale-guard
  - SSH key chmod 限制在 `~/.ssh` 或 configured key dir，並拒絕 symlink
  - SDK install_script 強制 relative + resolve under sdk_path + 拒絕 symlink
  - SDK scan 拒絕 symlink，避免惡意 repo 注入外部路徑
  - `_validate_platform_name` 統一守門 platform 名稱（拒絕 path traversal）
  - DISK_FULL 清理改為 whitelist + 1h in-flight 保護 + symlink TOCTOU 重檢
- **Batch 2（完成）**：Resource leaks & exception swallowing — N3/H19/H20/L4/M11/N5/N7
  - `EventBus._subscribers` 改 `set`（O(1) discard）+ backpressure 計數器 + warning log
  - `_persist_event` 失敗改 `logger.debug` 而非 silent swallow
  - `invoke.py` watchdog 三處 bare except 改為 narrow + log
  - `sdk_provisioner` clone/pull/install_script 全部 timeout 後強制 `proc.kill()` + 部分 clone 自動清理
  - `permission_errors.check_environment` docker/git subprocess `try/finally proc.kill()`
  - `_provider_failures` dict 上限 256，>24h 條目自動修剪（防 OOM）
  - `error_history` cap=50（防 LangGraph state 膨脹）
- **Batch 3（完成）**：Concurrency & locking — C1/C13/H4/H11/H14/L15
  - `pipeline._pipeline_lock`：run/advance/force_advance 三入口共用 asyncio.Lock，杜絕 task 重複建立
  - `git_credentials._CACHE_LOCK`：double-check pattern，避免 first-call race
  - `sdk_provisioner._get_provision_lock(platform)`：per-platform lock，避免同 platform 並發 clone/YAML write 撞車
  - `agents/llm._provider_failures_lock` + `_record_provider_failure()` 統一接口（節點回呼也走它）
  - `events._log_fn_lock`：lazy import 競爭防護
  - `workspace.cleanup_stale_locks` + 預清理：>=60s 才視為 stale，杜絕誤刪 active git 鎖
- **Batch 4（完成）**：Pipeline deadlock & error-handling resilience — C2/C14/H2/H3/H8/H16/H17/H18/M17/M21
  - `_handle_llm_error` 改 `async`，retry 用 `asyncio.sleep` + token-freeze 中途中止
  - `_specialist_node_factory.node` + `conversation_node` 升級為 async（LangGraph 原生支援）
  - `_check_phase_complete` 過濾 cancelled/deleted；偵測 blocked/error/failed 時發 `pipeline_blocked` SSE 並 return False（C14 不再無限等待）
  - `force_advance` 於跳過 stuck task 時 log + emit `pipeline_force_override` 留審計軌跡
  - `_create_tasks_for_step` 每個 task 獨立 try/except，不會因單筆 fail 整步崩潰，emit `pipeline_task_create_failed`
  - `_active_pipeline` 完成後移到 `_last_completed_pipeline` 釋放 in-flight slot
  - `/invoke/halt` 同步將 pipeline 狀態標 `halted`，避免 race 中 advance
  - permission auto-fix 加 loop guard：同 category 已嘗試 2 次後 escalate（不再無限 fix→fail→fix）
  - permanent_disable 加 `pipeline_phase` SSE，前端 pipeline 面板可見
- **Batch 5（完成）**：SDK provisioner hardening — C11/H13/H15/L10/M15/N9
  - Clone 失敗 / timeout / size-cap 超限 → 強制 `shutil.rmtree(sdk_path)`，避免損壞目錄殘留
  - `OMNISIGHT_SDK_CLONE_MAX_MB`（預設 8GB）clone 後 size 檢查 + http.postBuffer 限制
  - `_atomic_write_yaml`：tempfile + `os.replace()`，併發 / crash 不會留半寫 YAML
  - install script 失敗改回 `provisioned_with_warnings`（M15）+ `install_failed=True`，呼叫端可判斷
  - `_redact_url`：clone 錯誤訊息洩漏 SDK URL/host 改為 `<sdk-url>` / `<sdk-host>`
- **Batch 6（完成）**：Tests, schema guards & misc — N6/H5/H6/H9/H12/N11 + 修復 3 個 pre-existing test_release UNIQUE failures
  - `db.py` 加 schema verify：`tasks.npi_phase_id` / `agents.sub_type` migration 失敗時 fail-fast，不再 silent warn
  - `git_credentials.get_webhook_secret_for_host`：改為精確等於比對，杜絕 `github.com` 誤匹配 `github.company.com`
  - YAML credential schema validation：型別 + 必要欄位（id/url/ssh_host + token/ssh_key/webhook_secret）
  - `workspace.py` git config 改用 `safe_agent`（防 quote command injection）
  - `permission_errors` PORT_IN_USE regex 用 word boundary，杜絕誤判
  - 新增 3 個 Gerrit handler 子函數測試（`_on_comment_added`, `_find_task_by_external_issue_id`）
  - 修復 3 個 pre-existing test_release UNIQUE failures：artifact id 改為 per-test uuid 後綴

## Audit-Fix 總結
- 6 個 batch、~50+ 個問題修復，commit 範圍 `67506d2..756ac93`
- 對應的安全 / 並發 / 資源 / pipeline / SDK / schema 領域全數獲得加固

## Phase 47 進度（Autonomous Decision Engine）
- **47A（完成）**：OperationMode (manual/supervised/full_auto/turbo) + DecisionEngine (`backend/decision_engine.py`) + GET/PUT `/operation-mode` + GET `/decisions` + 5 個 SSE events (mode_changed, decision_pending/auto_executed/resolved/undone) + invoke.py 由 `_invoke_lock` 改為 mode-aware semaphore (parallel cap 1/2/4/8)
- **47B（完成）**：Stuck detection + strategy switch — `backend/stuck_detector.py`（StuckReason × Strategy 策略矩陣）+ `analyze_agent / analyze_blocked_task / propose_remediation` 橋接 DecisionEngine（severity 映射：switch_model→risky、escalate→destructive）+ watchdog 整合（60s 掃描、de-dupe by (agent_id,reason)）
- **47C（完成）**：Ambiguity handling + Budget strategy — `backend/ambiguity.propose_options()`（safe_default_id + id 去重驗證 + severity 化 DecisionEngine 提案）+ `backend/budget_strategy.py`（quality/balanced/cost_saver/sprint 4 策略 × model_tier/max_retries/downgrade_at/freeze_at/prefer_parallel 5 knob）+ GET/PUT `/budget-strategy` + `budget_strategy_changed` SSE
- **47D（完成）**：Decision API + 30s sweep loop — `POST /decisions/{id}/approve|reject|undo` + `POST /decisions/sweep` 手動觸發 + `de.sweep_timeouts()` + `run_sweep_loop()` 於 lifespan 啟動（30s cadence, 過期 pending → timeout_default + resolver=timeout + chosen=default_option_id）
- **Phase 47 總計**：8 API 端點、6 SSE events、4 新模組（decision_engine/stuck_detector/ambiguity/budget_strategy）、~100 新測試全綠、2 background tasks（watchdog+sweep）

## Phase 47-Fix（深度審計後補修）
- **Batch A**（`b20bc2d`）：N4 parallel_slot 改 _ModeSlot（cap 每次 acquire 重讀，mode 切換立即生效）／N5 sweep + resolve 原子化（pop+mutate+archive 同鎖）／③ watchdog 讀 agent error ring buffer (`record_agent_error`)、repeat_error 路徑復活
- **Batch B**（`4471ec5`）：① `model_router` + `_handle_llm_error` 真正消費 `budget_strategy.get_tuning()`（tier/max_retries/downgrade 生效）／② `_apply_stuck_remediation` 執行 switch_model / spawn_alternate / escalate / retry_same（包含 backlog 掃 approved 的 decisions）／N9 halt 時 watchdog 跳過
- **Batch C**（`de2c365`）：N7 pending cap（env `OMNISIGHT_DECISION_PENDING_MAX`，default 256）／N8 reject 用 `__rejected__` sentinel／N10 `OMNISIGHT_DECISION_BEARER` 選配 bearer token／N11 structured-only log／N12 SSEDecision 加 `source`／N13 sweep interval env（default 10s）／N14 GET mode 回傳 `in_flight`
- **Batch D**（本 commit）：8 個 SSE round-trip 測試覆蓋 approve/reject/undo/mode/budget/sweep + schema 契約驗證

## Phase 48 進度（Autonomous Decision 前端）
- **48A**（`7ba21e3`）：lib/api.ts 新增 Phase 47 types + CRUD + SSEEvent 擴展（mode_changed/decision_*/budget_strategy_changed）
- **48B**（`3ddf608`）：`mode-selector.tsx` — 4-pill segmented control，global header 內掛載（mobile + desktop 兩版），SSE 同步 + 5s 輪詢 in_flight
- **48C**（`598127f`）：`decision-dashboard.tsx` — pending/history 雙分頁、approve/reject/undo 按鈕、倒數計時（<10s 變紅）、SSE 自動 refetch、手動 SWEEP
- **48D**（本 commit）：`budget-strategy-panel.tsx` — 4 策略卡片 + 5 knob 讀數（tier/retries/downgrade/freeze/parallel）；全部三個元件已掛在 app/page.tsx 右側 aside 頂端
- **E2E 驗證**：`curl PUT /operation-mode` 與 `PUT /budget-strategy` 成功 round-trip，回傳 payload 與前端 type 完全匹配

## Phase 48-Fix（前端深度審計後補修）
- **Batch A**（`244095d`）：P0 — 共享 SSE manager（lib/api.ts 單 EventSource 跨 caller）、Dashboard local-merge（SSE → upsert/remove 而非 150 項全拉）、AbortController + mountedRef、DecisionRow 去 useMemo、ModeSelector interval 分離（refreshRef 模式）、decision events timestamp 必填、SWEEP loading + RETRY 按鈕
- **Batch B**（`6cbd9b4`）：P1/P2 — Mobile nav 加 decisions/budget、DecisionSource 型別細化、compact 3-字母標籤 MAN/SUP/AUT/TRB、radiogroup aria-labelledby、BudgetPanel RETRY

## Phase 49（前端測試框架）
- **49A**（`2666c34`）：Vitest + jsdom + @testing-library/react + jest-dom + happy-dom 安裝，`vitest.config.ts` / `test/setup.ts` / `package.json` scripts（test / test:watch / test:ui），MockEventSource 伺服器端渲染 polyfill，4 個 smoke tests 綠
- **49B**（`0cc95c1`）：ModeSelector 6 + BudgetStrategyPanel 4 = 10 個 component tests — 覆蓋初始載入 / PUT / peer SSE / 錯誤路徑 / compact 3-letter guard / unmount cleanup
- **49C**（`639d113`）：DecisionDashboard 9 tests — list merge（_pending → 加入、_resolved → 移到 history）、approve/reject/undo、SWEEP loading、countdown（fake timers 驗證 < 10s 變紅）、RETRY 路徑

## Phase 49-Fix（測試框架深度審計補修）
- **Batch A**（`f0194d3`）：**N2** shared-SSE 整合測試（7 real-api cases）、**N1** MockEventSource close 清 listeners、**N5** emitError、**N3+N4** fake-timer isolation（try/finally + pin date）、**N6** compact label 斷言強化（radio.textContent 精確比對）、**N10** history 排序測試
- **Batch B**（`80335b2`）：**N7** `@vitest/coverage-v8` + thresholds（scoped to 3 Phase 48 components）、**N9** 拔 happy-dom、**N11** alias sync 檢查、**N8** 文件化非契約

## Phase 49E（Playwright E2E browser-level）
- 安裝 `@playwright/test` + Chromium（系統缺 `libnspr4`/`libnss3`/`libasound2`，用 `apt download` 抓 deb 解到 `~/.local/lib/playwright-deps`，透過 `OMNISIGHT_PW_LIB_DIR` 注入 LD_LIBRARY_PATH）
- `playwright.config.ts`：自動啟動 backend（uvicorn :18830）+ Next.js dev（:3100）兩個 webServer
- `e2e/decision-happy-path.spec.ts` — 5 tests 全綠：頁面掛載 3 個 panel、mode 切換 round-trip、budget 切換 round-trip、SWEEP button、SSE 決策表面穩定
- 實務心得：Turbopack 開發模式 React hydration 後 re-render 不穩，E2E 斷言在「browser fetch 透過 Next rewrite → backend」這層最可靠；UI aria-checked sync 後斷言會間歇性 flake，改為驗證 round-trip + 重新載入後再 fetch 確認

## 全家桶總計（commit 範圍 `67506d2..HEAD`）
- 後端 pytests：230+ 個，coverage 未量（Python 側不在本 phase 範圍）
- 前端 vitest：32 個（smoke 4 + components 19 + integration 7 + alias 1 + smoke 1），Phase 48 component coverage: lines 97.4% / statements 90.5% / functions 93.3% / branches 75%
- 前端 Playwright：5 個 E2E，涵蓋 3 panel 呈現 + 2 個 round-trip + SWEEP + SSE 基線
- 合計 ~267 個自動化 test 全綠

## Phase 50（排程中，尚未開工）— Timeline / Velocity / Decision Rules / Toast

延續 Phase 47 原 plan 中 Autonomous Decision Engine 仍未落地的 UI 能力。拆 4 個 sub-phase，每個自成 commit：

### 50A — Timeline View with deadline awareness + velocity tracking
- 後端：`GET /pipeline/timeline` 回傳每個 phase 的 `planned_at / started_at / completed_at / deadline_at`；若缺 schedule 資料先從 NPI state 推算
- 前端：`components/omnisight/pipeline-timeline.tsx`，水平 timeline + 當前進度標記 + 逾期 phase 高亮
- Velocity：近 7 天已完成 task 數 / 每 phase 平均完成時長，推算 ETA
- 測試：3 component test + 1 Playwright happy-path

### 50B — Decision Rules Editor
- 後端：`GET /decision-rules` / `PUT /decision-rules` — 規則 shape `{kind_pattern, severity, auto_in_modes[], default_option_id}`
- `decision_engine.propose()` 接 rule engine：優先命中 rule 決定 severity/default，否則落回目前 hardcoded policy
- 前端：`components/omnisight/decision-rules-editor.tsx`（Settings panel 內新 tab），CRUD + 拖拉排序 + "Test against last 20 decisions" 預覽
- 測試：5 backend unit（rule match precedence）+ 4 component test

### 50C — Notification Toast（approve / reject / undo 路徑）
- 前端：`components/omnisight/toast-center.tsx` — SSE `decision_pending` 高 severity 時跳 toast；toast 內含 approve/reject 按鈕 + 倒數 bar
- 與既有 NotificationCenter 不衝突（toast 是即時 overlay，notification 是持久中心）
- 可鍵盤操作（`A` approve default / `R` reject / `Esc` dismiss）
- 測試：3 component test（SSE→toast 出現 / approve / auto-dismiss on timeout）

### 50D — Mobile bridge + deep-link
- Mobile nav 目前有 decisions/budget（48-Fix B 加入）但缺 timeline
- Timeline view 加 mobile 佈局（垂直）
- URL deep-link：`/?decision=<id>` 打開指定 decision、`/?panel=timeline` 直達
- 測試：1 Playwright 路由 test

**預估**：每 sub-phase 1-2 h。整體 ~5-8 h。依照慣例，每 sub-phase 後做深度審計 → 補修 batch。

## Phase 50-Fix — 三輪深度審計後補修（2026-04-14，110 項 → 18 cluster）

三輪審計接連產出：第一輪 15 個 Critical / 第二輪 ~54 個 bug+設計 / 第三輪 56 個設計副作用+UX+測試文件落差。合計 **~110 項**，以 cluster 批次制收斂——每 cluster 修復 → targeted 測試 → uvicorn 啟動檢查 → 清理 → commit。

### 🔴 Critical 波（commit `7d0cf31` .. `e6995b7`，5 cluster）

- **Cluster 1**：SSE 穩定性三項（`connectSSE` stale closure / `_log_fn` race / `_sharedES.onerror` sync）經 Read 驗證**全為審計代理幻覺**——code 已使用正確雙重檢查鎖、EventSource 內建重連、listener iteration 已快照。Wontfix with rationale（無 commit）。
- **Cluster 2** `7d0cf31`：backend safety — `_reset_for_tests()` 參考已刪除全域 `_parallel_sema` 修為實際的 `_parallel_in_flight/_parallel_async_cond`；`decision_rules.apply` 例外改 warning + `source.rule_engine_error` 外露。#5/#8 誤報。
- **Cluster 3** `20a4ac8`：`streamInvoke()` 加 try/finally + `stream_truncated` error frame + reader lock 釋放。#4/#9 誤報。
- **Cluster 4** `9cdad18`：UX Critical — mobile-nav undefined 崩潰保護、toast `deadline_at` 單位驗證（支援秒/毫秒自動偵測）、倒數字體 12px + 紅脈動 + `prefers-reduced-motion`、決策儀表板 empty state with icon/CTA、全站 `aria-live="assertive"` + `aria-atomic`、Page-Visibility tick 暫停。
- **Cluster 5** `e6995b7`：A1 決策規則 SQLite 持久化 — 新增 `decision_rules` 表 + `load_from_db()` lifespan 載入 + `replace_rules()` 寫透；新增 3 個持久化測試全綠。

### 🟠 High 波（commit `e2c11cb` .. `31e81a1`，7 cluster）

- **H1** `e2c11cb`：`_agent_error_history` 加 `threading.Lock` + `_snapshot_agent_errors()` 供 watchdog。#11/#12/#15/#22 誤報。
- **H2** `7177ef0`：API 安全三項 — decision mutator sliding-window rate limit（30 req/10s per IP，`OMNISIGHT_DECISION_RL_{WINDOW_S,MAX}` 可調）、`streamChat` 加 stream_truncated 守護、SSE schema 內聯型別強化（`SSEBudgetTuning`/`SSEDecisionOption`）。#14/#25 誤報。
- **H3** `211486f`：SSR/CSR hydration mismatch 修復 — `activePanel` 統一初始為 `orchestrator`，URL 深鏈在 mount effect 套用。#16/#24 誤報。
- **H4** `832d6f4`：UX accessibility 五項 — toast overflow chip（"+N MORE PENDING"）、mobile dots 44×44 觸控目標、skeleton loading、destructive confirm dialog、HTTP 錯誤分類（AUTH / RATE LIMITED / BACKEND DOWN / NETWORK）。
- **H5** `1bbac3b`：明示 dark-only 設計決定 — `color-scheme: dark` + README Theme 章節解釋。
- **H6** `2f5c327`：新增 `/api/v1/system/sse-schema` 端點、補 `.env.example` 七個遺漏項。同步修復 Phase 47 新增事件後未更新的 `test_schema.py`。A2/A3/A6/A8 標記為設計決定。
- **H7** `31e81a1`：測試/文件 scaffold — 3 個元件 smoke test（EmergencyStop/NeuralGrid/LanguageToggle）、3 個 E2E deep-link spec、README Quick Start `.env` 前置步驟 + `/docs` Swagger 指引、conftest globals-reset pattern 文件化。

### 🟡 Medium 波（commit `f196085` .. `bba663c`，5 cluster）

- **M1** `f196085`：`propose()` options 驗證（非空 id / 不重複 / default 存在）、db `_migrate` PRAGMA 失敗改 raise RuntimeError。#32/#36/#38/#40/#42 誤報。
- **M2** `fd969ec`：budget-panel error 10s 自動清除、decision-dashboard tablist + 方向鍵切換。既有測試 query 由 `role="button"` 改 `role="tab"`。
- **M3** `222ba33`：focus ring 改白色 + offset（WCAG AA 通過）、budget knob cells 加 title + sr-only valid-range。B15/B16 誤報。
- **M4** `8e8265e`：新增 `CHANGELOG.md`（Unreleased 段匯整本次所有修復）、`.github/CONTRIBUTING.md`、`.github/PULL_REQUEST_TEMPLATE.md`、decision-rules-editor 加 `clientValidate()` 行內預檢。
- **M5** `bba663c`：移除 dead `_invoke_lock`、`lib/api.ts` 加 `_resolveApiBase()` URL 驗證、`mode_changed` publish 例外改 warning。#28/#39/#46/#47 wontfix。

### 🟢 Low 波（commit `52a89ab`，1 cluster）

- **L** `52a89ab`：validation 錯誤改 HTTP 422（REST/Pydantic 慣例）、`AgentWorkspace.status` 改 `Literal["none","active","finalized","cleaned"]`、`.scroll-fade` mask 提示可捲動、`playwright.config.ts` env 覆寫文件化。#46/#49/#51/#53 誤報。

### 統計

| 類別 | 總項 | 實修 | 誤報 / 刻意設計 |
|---|---|---|---|
| 🔴 Critical | 15 | 8 + 3 順手 | 7 |
| 🟠 High | 44 | 17 + 5 文件 | 12 |
| 🟡 Medium | 32 | 12 + 3 新檔 | 14 |
| 🟢 Low | 19 | 5 | 10+ |
| **合計** | **~110** | **~48 實修 + 11 新檔/文件** | **~43 wontfix with rationale** |

### 產出
- **新增 SQLite 表**：`decision_rules`（operator 規則持久化）
- **新增 API 端點**：`GET /api/v1/system/sse-schema`
- **新增 env 變數**：`OMNISIGHT_DECISION_RL_WINDOW_S / DECISION_RL_MAX`（速率限制調整）
- **新增檔案**：`CHANGELOG.md`、`.github/CONTRIBUTING.md`、`.github/PULL_REQUEST_TEMPLATE.md`、`backend/tests/test_decision_rules_persistence.py`、`test/components/smoke-untested.test.tsx`、`e2e/deep-link.spec.ts`
- **每 cluster 啟動驗證**：uvicorn `/api/v1/health` → 200
- **測試**：backend 95+ 決策/schema/ambiguity tests 綠；frontend 52/52 綠（46 原 + 6 新 smoke）

### 關鍵工程經驗
- **審計代理幻覺**：三輪審計合計 ~43 項誤報（39%），多為行號幻覺、已有防護視而不見、或 LangGraph/Pydantic 慣例誤判。**修復前務必 Read 驗證**；每項 commit 訊息都標註 wontfix 的具體 rationale。
- **Cluster 批次制**：per-item full test 不可行（備忘錄已記 60–180min + 超時）；改為 cluster 內修多項、cluster 末跑 targeted + 啟動檢查。18 個 cluster、每個 5–15 min，整體 ~4h 完成 110 項。
- **persist → load from DB 模式**：A1 確立的寫透 + lifespan 載入樣式，後續 Phase 53 audit_log 可沿用。

## Phase 65 — Data Flywheel / Auto-Fine-Tuning 完成（2026-04-15）

L4 自我進化最後一塊：合格 workflow_runs 每晚 export 成 JSONL → 微調
backend 提交 → poll 完成 → 對 hold-out 評估 → Decision Engine admin
gate 決定 promote 或 reject。完整的「資料 → 訓練 → 評估 → 部署」閉
環，全程 audit-logged。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `finetune_export.py`：double-gate（completed × hvt_passed × clean resolver × scrub-safe）+ shortest-path filter（drop failed retries by `_key_root`）+ ChatML JSONL；CLI `python -m backend.finetune_export`；1 metric；17 test | `840a862` |
| S2 | `configs/iq_benchmark/holdout-finetune.yaml` 10 題手工策展 + `finetune_eval.py::compare_models` baseline vs candidate；regression > 5pp（env clamp [0,50]）→ reject；4 種 decision；1 Gauge；16 test | `987700b` |
| S3 | `finetune_backend.py` `FinetuneBackend` Protocol + Noop（synthetic 立即 succeeded）/ OpenAI（lazy SDK + key gate）/ Unsloth（subprocess injectable runner，prod 走 T2 sandbox）；`select_backend` factory unknown fallback noop + warn；19 test | `518f42d` |
| S4 | `finetune_nightly.py` 串接 export → submit → poll bounded → eval → DE proposal；10 status 涵蓋全分支；reject 走 destructive default=reject、promote 走 routine default=accept；min_rows=50 防小樣本；audit 全程；opt-in L4；lifespan wire；20 test | `8be01e1` |
| S5 | `docs/operations/finetune.md` 操作員 runbook（status 表 / audit / metrics / backend / hold-out 策展守則 / pitfalls）+ HANDOFF | _本 commit_ |

### 設計姿態

- **雙閘 + shortest-path 防 feedback poisoning**：auto-only resolver
  + hvt_passed=false + scrub_unsafe 都 reject；retry 失敗的中間步驟
  剔除，只訓練「真正成功的最短路徑」。
- **Backend 抽象 Protocol**：3 後端介面一致；prod 用 OpenAI 或 Unsloth，
  dev/staging 用 noop（synthetic 立即 succeeded 仍跑完整 gate logic）。
- **Unsloth 必走 T2 sandbox**：injectable runner 是契約，prod caller
  把 `container.exec_in_container` 包進 runner，本地 subprocess 只
  是 dev fallback。
- **Hold-out 手工策展、禁 auto-gen**：避免「model 評自己功課」自評偏誤。
- **Eval 雙跑同 ask_fn**：baseline 與 candidate 共用同一 ask_fn，任何
  共用基礎設施問題（rate limit / 暫時錯誤）影響相同，delta 仍有意義。
- **Reject 走 destructive default=reject**：admin 必須明確 override 才
  能上一個已知 regress 的模型；DE timeout 24h 後 default 自動 apply
  → 候選自動丟棄。
- **Promote 走 routine default=accept**：通過 hold-out 的候選在
  BALANCED+ profile 自動接受，operator 可手動 reject 退出。
- **min_rows_to_submit=50**：小訓練集帶來 regression 多於改進，預設
  即跳過。
- **每步 audit_log**：10 個 audit action 涵蓋全分支，hash chain 不變。
- **全部 opt-in L4**：`OMNISIGHT_SELF_IMPROVE_LEVEL` 含 `l4` 才啟動。

### 新環境變數

```
OMNISIGHT_FINETUNE_BACKEND=noop           # noop|openai|unsloth
OMNISIGHT_FINETUNE_REGRESSION_PP=5        # [0,50] clamp
# 既有 OMNISIGHT_SELF_IMPROVE_LEVEL 需含 'l4' 或 'all'
```

### 新 metrics

- `omnisight_training_set_rows_total{result}` — Counter；`result=
  written` 或 `skip:<rule>`，funnel 視覺化
- `omnisight_finetune_eval_score{model}` — Gauge；baseline 與
  candidate 同時發 sample 便於 Grafana 對照

### 新 Decision Engine kinds

- `finetune/regression` — destructive，default=reject，options
  {reject, accept_anyway}，24h timeout
- `finetune/promote` — routine，default=accept，options {accept,
  reject}，24h timeout

### 新 audit actions（10 個）

`finetune_exported` / `finetune_submit_unavailable` /
`finetune_submit_error` / `finetune_submitted` /
`finetune_poll_timeout` / `finetune_failed` / `finetune_eval_skipped` /
`finetune_evaluated` / `finetune_promoted` / `finetune_rejected`

### 驗收

`pytest test_finetune_export + test_finetune_eval +
test_finetune_backend + test_finetune_nightly` → **72 passed**
（17 + 16 + 19 + 20）。

### Phase 65 完成 → 64-B/65 連動全鏈打通

64-B Tier 2 sandbox（egress 控制）就位 → 65 Unsloth backend 可在
T2 內 run；OpenAI fine-tune API 也可（egress 經 T2 限流 / 監控）。
完整鏈：

```
workflow_runs → JSONL → T2 sandbox → fine-tune backend → 候選模型
                                                         │
                                                         ▼
                                              hold-out eval (T0)
                                                         │
                                                         ▼
                                      DE finetune/regression or promote
                                                         │
                                                         ▼
                                         operator approve → live model
```

### 後續

剩 **Phase 64-C T3 Hardware Daemon**（10–14h，等實機，獨立 track）。

---

## Phase 63-E — Episodic Memory Quality Decay 完成（2026-04-14）

Locked design rule：**只降權，不刪除**。過時答案可能仍是罕見邊角
case 的正解，刪掉不可逆；decay 讓 `decayed_score` 滑向 0、FTS5
排序往下沉，但 row 留著，admin 可 restore。

### 改動

- `backend/db.py`：`episodic_memory` 加 `decayed_score REAL NOT NULL DEFAULT 0.0`
  + `last_used_at TEXT`（runtime migration）；`insert_episodic_memory`
  初始化 `decayed_score=quality_score`（新 row 以自身品質競爭）。
- `backend/memory_decay.py`（新）：
  - `touch(memory_id)` — RAG pre-fetch / 手動查詢 hook，重置 decay clock
  - `decay_unused(ttl_s, factor, now)` — nightly worker；`last_used_at`
    早於 cutoff（或 NULL）的 row `decayed_score *= factor`；factor clamp [0,1]
  - `restore(memory_id)` — admin endpoint，複製 `quality_score` 回 `decayed_score`
  - `run_decay_loop` — 單例背景 coroutine，opt-in `OMNISIGHT_SELF_IMPROVE_LEVEL` 含 `l3`
- `backend/metrics.py`：`memory_decay_total{action}`（decayed/skipped_recent/restored）
- `backend/main.py` lifespan：`md_task = asyncio.create_task(md.run_decay_loop())`
- `backend/routers/memory.py`（新）：`POST /memory/{id}/restore`（require_admin）
- `.env.example`：`OMNISIGHT_MEMORY_DECAY_TTL_S=7776000`（90d）/
  `_FACTOR=0.9` / `_INTERVAL_S=86400`
- `backend/tests/test_memory_decay.py`：16 tests（is_enabled 參數化 /
  touch / decay skip-vs-apply / factor clamp / restore / loop singleton）
  全綠。

### 後續

Phase 63-E 完成 → **僅剩 Phase 64-C T3 Hardware Daemon**（10–14h，
等實機，獨立 track）。主線隊列清空。

---

## Phase 56-DAG-E — DAG Authoring UI 完成（2026-04-14）

Pain point：backend 的 DAG planner 功能齊備（7 rules / mutation loop /
storage），但 operator 只能手寫 JSON 走 curl、錯了盲改再丟，沒有任何前端。
本 phase 補上 MVP 編輯器 + dry-run 驗證端點。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `POST /api/v1/dag/validate` dry-run — 不入庫、不建 run、不跑 mutation loop；過 Pydantic schema + 7-rule validator 後回 `{ok, stage, errors[]}`，固定 200（payload 帶 ok flag，前端少寫 HTTP 分支）；4 tests | `7485c45` |
| S2 | `components/omnisight/dag-editor.tsx`（316 行）— JSON textarea + 500ms debounce live-validate + 3 範本（minimal / compile→flash / fan-out 1→3）+ Format/Copy/Submit + `mutate=true` toggle + cancel-previous AbortController + valid-only-enables-Submit；`lib/api.ts` 加 `validateDag`/`submitDag` + types | `d6e5292` |
| S3 | Mount — `PanelId` 加 `"dag"`、MobileNav/TabletNav chips 加 DAG Editor（Workflow icon）、`VALID_PANELS` 加入、`renderPanel` switch 加 case；deep link `/?panel=dag` 可用 | `a6e12b7` |
| S4 | 5 frontend tests（default template / JSON parse error / rule errors disable Submit / valid enables + POST / template load）；HANDOFF | _本 commit_ |

### 設計姿態

- **Dry-run 與 submit 分離**：validate 不污染儲存，editor 可以每個 keystroke 打一次。submit 仍走 `workflow.start` 完整路徑。
- **422 vs 200**：validate 固定 200，payload 帶 `ok`；submit 保留 backend 原本語意（422 = validation fail）。
- **mutate 預設 off**：UI 明示這會呼叫 LLM 自動修，不當黑盒。
- **不依賴 Monaco / react-flow**：純 textarea + lucide icons 無新 dep。升級到 Monaco（DAG-F）或視覺化 canvas（DAG-G）延後。

### 後續解鎖

- **DAG-G**：react-flow 視覺化（節點/邊、拖拉依賴、即時 cycle 偵測）。
- **可順手**：live-validate 結果面板加 **jump to line**（需切到 Monaco）。

---

## Phase 56-DAG-F — Form-based Authoring 完成（2026-04-15）

Pain point：DAG-E 解決「不用 curl」，但 operator 仍要手寫 JSON schema
（tier enum、expected_output 格式、depends_on 必須對應存在的 task_id）。
本 phase 加入表單式 authoring，和 JSON 編輯器互通。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `components/omnisight/dag-form-editor.tsx`（267 行）controlled component — row-per-task（task_id / tier dropdown / toolchain / expected_output / description / depends_on chip toggles）+ reorder ↑↓ + 刪除 + 自動清理 dangling deps + 自動命名不撞 id；95% 路徑專用，`inputs[]` / `output_overlap_ack` 保留在 JSON tab | `1f92d14` |
| S2 | DagEditor 加 tablist（JSON / Form）；`text` 保持 canonical，form value 從 `JSON.parse(text)` 推、`onChange` 反序列化回 text；parse 失敗時 Form 顯示「先去 JSON 修」提示（避免 WIP 覆蓋）；validate / submit / templates / jump-to-timeline 全部在上層共用 | `095f759` |
| S3 | 7 個 vitest：render row / 編輯 task_id / add task 自動命名 / 刪 task 清 downstream deps / chip toggle / Form→JSON tab flip 不丟 edits / JSON 損毀 Form 顯示 nudge | _本 commit_ |

### 設計姿態

- **單一真實來源**：`text` 是唯一 canonical，form 只是它的 view。解耦了 form shape 與 schema，backend 演進時只要 JSON 相容即可，表單升級是純前端事。
- **分工明確**：DAG-E（JSON）面向熟悉 schema 的 operator 與 diff review；DAG-F（form）面向不熟的新 operator；同一個 submit 路徑出去。
- **不引入 heavy dep**：純 React + lucide icons，延後 react-flow 到 DAG-G。

### 後續解鎖

- **DAG-G**：視覺化 DAG canvas（react-flow / dagre）— 給 5+ task 的 DAG 一個拓撲鳥瞰圖，拖拉連線即時看 cycle。
- **可順手**：inputs[] / output_overlap_ack 也進 Form（目前得切 JSON）；DAG template gallery 擴充（e.g. 含 tier mix 範本）。

---

## Phase 67-C — Speculative Container Pre-warm 完成（2026-04-15）

Engine 3 從 `lossless-agent-acceleration.md` 落地。DAG validate 通過
後，in-degree=0 的 Tier-1 任務容器在背景啟動；dispatch 時 consume
省掉 1–3s 冷啟動。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `backend/sandbox_prewarm.py`：`pick_prewarm_candidates`（in-degree=0 + Tier-1、depth 2 env clamp [0,8]）+ `prewarm_for`（走既有 `start_container` → 自動套 64-A image trust + 64-D lifetime cap、dedup）+ `consume` 原子 pop + `cancel_all`（mutation/abort 釋放、per-slot stopper 失敗不影響其他）；2 metrics；22 test | `ee64837` |
| S2 | DAG router 整合：submit validated → `asyncio.create_task(prewarm_in_background)` 不阻 response；mutate 前 `cancel_all` 防過時速推浪費 lifetime；opt-in env + 失敗 swallow；6 test；HANDOFF | _本 commit_ |

### 設計姿態

- **預設 off（opt-in）**：`OMNISIGHT_PREWARM_ENABLED=true` 才啟動。
  Fire-and-forget + 失敗 swallow 不影響 submit。
- **絕不繞過沙盒守門**：pre-warm 走既有 `start_container` → image trust + lifetime cap 自動套用。
- **Mutation 前必 cancel**：replanned DAG 的 in-degree=0 任務會不同。
- **In-degree ≠ 0 絕不 pre-warm**：上游未完成無從 useful。
- **只 Tier-1**：networked / t3 start-up 特性不同，v1 不 model。
- **Depth clamp [0, 8]**：operator 設 99 也只會跑 8。
- **Consumed slot 由 caller 擁有**：cancel_all 不 stop 已交付 container。

### 新環境變數

```
OMNISIGHT_PREWARM_DEPTH=2       # [0, 8] clamp
OMNISIGHT_PREWARM_ENABLED=false # 整合 opt-in gate
```

### 新 metrics

- `omnisight_prewarm_started_total` — Counter
- `omnisight_prewarm_consumed_total{result}` — Counter；
  `result ∈ {hit, miss, cancelled, start_error}`

### 驗收

`pytest test_sandbox_prewarm + test_dag_prewarm_wire` →
**28 passed**（22 + 6）。

### Phase 67 完成進度

```
67-A Prompt Cache         ✅
67-B Diff Patch           ✅
67-C Speculative Pre-warm ✅（本 commit）
67-D RAG Pre-fetch        ✅
```

Engine 1–4 全部 ship；`lossless-agent-acceleration.md` 落地完成。

### 已知限制（Phase 68+ 待續）

- **Workspace binding**：v1 pre-warm 使用 `_prewarm/` shared 空間。
  真正 dispatch 時 consume() 回傳 container，但 per-agent workspace
  尚未 mount。完整收益需「pre-warm → consume → mount workspace via
  docker cp / bind remount」流程。
- **Consume 未 wire 到執行器**：DAG dispatcher 尚未整合 `consume()`；
  現階段 pre-warm 帶來 image-pull cache 但尚未省 start。

### 後續

**Phase 65 Data Flywheel**（10–14h，64-B T2 已就位）或 **Phase 63-E
Memory Decay**（2–3h）可動工。64-C T3 硬體 track 獨立。

---

## Phase 67-B — Diff Patch + 強制契約 完成（2026-04-15）

把「agent 不可覆寫整檔」從宣告改成 enforced。五條路徑（patch/create/
write-new/write-small/write-big）全用 @tool 控管；規範透過 prompt
registry canary 推送，違規觸發 IIS 軟反饋。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `agents/tools_patch.py`：`parse_search_replace` + `apply_search_replace` (≥3 行 context、唯一匹配強制) + `apply_unified_diff` (多 hunk、CRLF 保留、last→first apply) + `apply_to_file` 原子寫入；4 種 exception 分類；22 test | `dacba89` |
| S2 | `@tool` 三劍客：`patch_file(path, kind, payload)` / `create_file(path, content)` / `write_file` 攔截器；既有檔超 cap overwrite → `[REJECTED]` + 餵 IIS `code_pass=False`；`patch_file` 失敗同樣餵 IIS；env `OMNISIGHT_PATCH_MAX_INLINE_LINES=50`；12 test | `c5c4a66` |
| S3 | `backend/agents/prompts/patch_protocol.md`（由 Phase 56-DAG-C S3 bootstrap 自動入 prompt_versions）+ `docs/operations/patching.md`（操作員 runbook / 失敗 mode 表 / IIS 連動）+ HANDOFF | _本 commit_ |

### 設計姿態

- **`write_file` 不強制刪除**：first-time writes 仍可用（scratch / fresh
  path 常見），只對既有檔超 cap overwrite 擋；漸進 deprecation，不破
  壞 agent 現有 workflow。
- **違規軟反饋而非硬阻擋**：`write_file` 超 cap + `patch_file` 失敗
  皆餵 IIS `code_pass=False`；3 次以上觸發 Phase 63-B L1 calibrate
  （prompt_registry 重 inject `patch_protocol.md`）；連續失敗再升 L2
  route。不做硬重啟避免無限迴圈（與 IIS 已鎖決策一致）。
- **SEARCH ≥3 行 context**：設計鎖死；1 行 SEARCH 在真實代碼幾乎必
  定 ambiguous。
- **唯一匹配強制**：zero match / multi match 都 raise；silent apply on
  wrong occurrence 是最糟失敗模式。
- **Atomic write**：temp file + rename；崩潰不留半檔。
- **CRLF 保留**：Windows-origin 檔不被悄悄轉 LF。
- **`create_file` 不 cap**：generated boilerplate（`__init__.py` /
  fixtures / templates）本就合理長檔。

### 新環境變數

```
OMNISIGHT_PATCH_MAX_INLINE_LINES=50    # write_file 既有檔 overwrite cap
```

### 新 agent tools

- `patch_file(path, patch_kind, payload)` — 既有檔編輯
- `create_file(path, content)` — 新檔
- `write_file` — deprecated for existing-file overwrites（保留 first-time writes）

### 新 prompt fragment

- `backend/agents/prompts/patch_protocol.md` — bootstrapped 進
  prompt_versions，可走 Phase 63-C canary。

### 驗收

`pytest test_tools_patch + test_tools_patch_wrappers + test_prompt_registry_bootstrap`
→ **41 passed**（22 + 12 + 7）。

### Phase 67 進度

```
67-A Prompt Cache       ✅
67-B Diff Patch         ✅（本 commit）
67-D RAG Pre-fetch      ✅
67-C Speculative Pre-warm  ← 下一個（需 DAG dispatcher，已就位）
```

### 後續

**Phase 67-C Speculative Pre-warm**（4–5h）可直接動工 — 需要 DAG
dispatcher（Phase 56-DAG-D 已就位）+ 64-A image trust（已就位）。

---

## Phase 56-DAG-D — Mode A 端點 完成（2026-04-14）

DAG suite (A/B/C) 由 Python 層推上 HTTP layer。Mode A = operator 手寫
DAG JSON，驗證 + 選擇性 mutation + workflow_run 連結。

### 交付

`backend/routers/dag.py`：
- `POST /api/v1/dag`（operator）：body `{dag, mutate, metadata}`；
  Pydantic schema fail → 422 stage=schema；semantic fail →
  422 + `validation_errors`；`mutate=true` + fail → 走
  `dag_planner.run_mutation_loop`：recovered → 200 + successor run_id
  + supersedes_run_id；exhausted → 422 stage=mutation_exhausted
  （DE `dag/exhausted` 已於 loop 內 file）。
- `GET /api/v1/dag/plans/{plan_id}`
- `GET /api/v1/dag/runs/{run_id}/plan`
- `GET /api/v1/dag/plans/by-dag/{dag_id}` — 完整 mutation chain

`_default_ask_fn` lazy-import `iq_runner.live_ask_fn`，避免 LangChain
拖累 router import 時間。`main.py` 已 wire。

`docs/operations/dag-mode-a.md`：7 rule 速查 / mutate 行為 / response
shape / 常見 pitfall。

### 設計姿態

- **Mode B 延後**：chat router 整合 AI auto-plan 另行規劃，避免動到 hot chat 路徑。
- **Schema error 早 fail**：Pydantic 在語意驗證前即擋下，省 DB round-trip。
- **Mutation opt-in**：預設 `mutate=false`；operator 須明確要求。
- **Recovered = 新 run**：保留舊 run audit trail（successor_run_id 雙向連）。
- **Exhausted = 422 + DE already filed**：endpoint 不重複 file。
- **operator role 即可**：與 chat 一致；admin 只用於破壞性 skill 操作。

### 驗收

`pytest test_dag_router` → **12 passed / 12.78s**。

### Phase 56-DAG 全套就位

```
[A] validator ✅ → [B] persistence ✅ → [C] mutation loop ✅ → [D] Mode A endpoint ✅
```

Mode B（chat-integrated auto-plan）留作未來 Phase。

### 後續

**Phase 67-B Diff Patch**（5–7h）或 **Phase 67-C Speculative Pre-warm**
（4–5h）可動工。67-C 已有 DAG dispatcher 可讀（56-DAG-D 就位）。

---

## Phase 56-DAG-C — DAG Mutation Loop + Orchestrator 完成（2026-04-14）

把 Phase 56-DAG-A（validator）+ Phase 56-DAG-B（persistence）串成真正
的自癒閉環：validate 失敗 → Orchestrator LLM 重新規劃 → 再 validate
→ 至多 3 round；超過即升級 Decision Engine admin gate。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `backend/agents/prompts/orchestrator.md`（Lead Orchestrator prompt、4 slicing laws、JSON-only contract）+ `dag_planner.py::propose_mutation`（inject ask_fn、JSON 容錯提取含 fence / prose prefix / brace balance、parse 失敗 loud raise、dag_id drift 強制還原）；20 test | `48a9bc0` |
| S2 | `run_mutation_loop(initial, ask_fn, max_rounds=3)` + `MutationAttempt`/`MutationResult` 三狀態（validated / exhausted / orchestrator_error）；exhausted → Decision Engine `kind=dag/exhausted severity=destructive default=abort` + timeout 1h；parse 失敗也消耗 round 防 orchestrator 壞掉無限迴圈；DE 失敗不影響 caller；新 metric `dag_mutation_total{result}`；11 test | `d6e19b7` |
| S3 | `prompt_registry.bootstrap_from_disk()` idempotent 把 `backend/agents/prompts/*.md` 注入 `prompt_versions` 當 active；wire 進 lifespan；拒絕 CLAUDE.md、拒絕 PROMPTS_ROOT 外、read 失敗跳過；7 test；HANDOFF | _本 commit_ |

### 設計姿態

- **Bounded retry = 3**：locked decision，防 orchestrator 壞了無限燒 token。
- **Status 三分**：validated / exhausted / orchestrator_error — operator 能立即區分「任務本身 intractable」vs「planner 本身壞了」。
- **Parse fail 消耗 round**：若純 parse 失敗不計 round，壞掉 orch 可永回 "not json" → 系統永不升級 admin。
- **DE default = abort**：destructive proposal 的安全默認是放棄而非 accept_failed。
- **DE failure swallowed**：mutation loop caller 不應因 DE 單點故障而死。
- **Orchestrator prompt 走 registry canary**：operator 改 `.md` 重啟 → registry 產生 v2 → 由 Phase 63-C canary 漸進部署。
- **Bootstrap idempotent**：body hash 相同即 no-op；重啟不堆積 version。
- **Path 白名單嚴格**：CLAUDE.md 永禁、PROMPTS_ROOT 外一律拒，即使絕對路徑也一樣。

### 新 Decision Engine kind

- `dag/exhausted` — severity=destructive, options={abort, accept_failed}, default=abort, 1h timeout

### 新 metric

- `omnisight_dag_mutation_total{result}` — recovered / exhausted

### 驗收

`pytest test_dag_planner_propose + test_dag_mutation_loop + test_prompt_registry_bootstrap`
→ **38 passed**（20 + 11 + 7）。

### 後續

**Phase 67-B Diff Patch**（5–7h）或 **Phase 56-DAG-D 雙模執行**
（2–3h）可動工。56-DAG-D 會把 mutation loop 接進 chat router
（Mode B auto-plan）與新 POST /api/v1/dag endpoint（Mode A manual）。

---

## Phase 63-D — Daily IQ Benchmark 完成（2026-04-14）

每晚跑固定題庫、量化 model 能力退化，連續 2 天低於 baseline 10pp 即
`action` level Notification。吸收原 Phase 65 hold-out eval 的題庫前身。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| D1 | `iq_benchmark.py` schema + loader + scorer + `configs/iq_benchmark/firmware-debug.yaml` 手工 10 題；deterministic match（keyword AND + optional regex + forbidden blacklist）；20 test | `a4be773` |
| D2 | `iq_runner.py` `run_benchmark` + `run_all` + injectable `ask_fn`；token budget cap 中途 truncate；per-Q timeout；失敗其他題仍跑；跨 model budget 隔離；`live_ask_fn` lazy-import LangChain；9 test | `ac8c8d5` |
| D3 | `iq_runs` 表 + `iq_nightly.py`：per-day 聚合 + median baseline + 10pp 門檻 regression；opt-in `OMNISIGHT_SELF_IMPROVE_LEVEL` 含 l3；notify level=action；Gauge `intelligence_iq_score{model}` + Counter `intelligence_iq_regression_total{model}`；18 test | `62824b4` |
| D4 | `run_nightly_loop` 背景循環 + singleton guard + cancel 清 flag；wire 進 `main.py` lifespan；2 loop test；HANDOFF | _本 commit_ |

### 設計姿態

- **題庫手工策展**：避免從 episodic_memory 自動生成造成的自我參照偏誤。
- **Deterministic scorer**：keyword + regex，無 LLM judge（judge 本身也會漂）。
- **Per-day 聚合**：多次同日 run → 取平均，避免單日雜訊誤觸發。
- **Baseline = 滾動中位數**：對極端值 robust。
- **連 2 天 + 10pp 雙 gate**：單日跌 15pp 不觸發（可能是 noise）；連續跌才算真 regression。
- **Notification level=action** 非 critical：operator 可處理但不該 3am 打 pager。
- **Opt-in L3**：與 Phase 63-B mitigation 同域 gate（都屬 intelligence track）。
- **Loop 單例 + 乾淨 cancel**：與 Phase 52 dlq_loop、47 sweep_loop 相同模式。

### 驗收

`pytest test_iq_benchmark + test_iq_runner + test_iq_nightly` →
**49 passed**（20 + 9 + 20；含 2 loop singleton/cancel test）。

### 後續

**Phase 67-D RAG Pre-fetch**（3–4h）可立即啟動。56-DAG-C mutation loop
是主鏈下個節點。

---

## Phase 67-A — Prompt Cache 標記層 完成（2026-04-14）

第一個 Phase 67 子任，純 LLM 層、無 dependency、與 56-DAG track 平行
完成。`CachedPromptBuilder` 統一封裝 5 段 message order contract +
provider-specific cache hint 注入。

### 交付（commit `e3c976d`）

`backend/prompt_cache.py`：
- `CachedPromptBuilder.add_*()` 5 個 typed adder（system / tools /
  static_kb / conversation / volatile_log）
- `.build_for(provider)` 回 provider-native message list
- 順序在 build 時排序強制（`_ORDER` 寫死），caller 加入順序不影響輸出
- Provider matrix：

| Provider | 處理 |
|---|---|
| Anthropic | system+tools 走 `_anthropic_system_blocks` wrapper、每 block 加 `cache_control: ephemeral`；static_kb user block 也標 cacheable；conversation/volatile_log **不**標 |
| OpenAI | 不加 markers（auto-cache prefix ≥1024 tokens）；只保持順序穩定 |
| Ollama | one-shot process-level warning + plain messages |
| 未知 | 同 Ollama，warning 帶 provider name |

- 空 / whitespace 段在 build 時 drop（避免污染 cache prefix）
- Master switch `OMNISIGHT_PROMPT_CACHE_ENABLED`（預設 true）
- `record_cache_outcome(provider, hit_tokens=, miss_tokens=)` 餵 SDK 回傳的 cache token 計數

### 新環境變數

```
OMNISIGHT_PROMPT_CACHE_ENABLED=true   # 預設 ON，prod 不應關
```

### 新 metrics

- `omnisight_prompt_cache_hit_total{provider}` — Counter（tokens）
- `omnisight_prompt_cache_miss_total{provider}` — Counter（tokens）

### 設計姿態

- **Builder 不呼 LLM**：純 message list 產生器；既有 `agents/llm.py` adapter 不變，整合留給後續 hot callsite 漸進。
- **Order at build time**：caller 自由 `.add_*()`，build 時統一排序；避免「呼叫順序錯就 silent miss」陷阱。
- **Cacheable vs volatile 二分**：3 段（system/tools/static_kb）標 cacheable、2 段（conversation/volatile_log）不標；conversation 雖然某輪內容固定，但下一輪即變，標反而會炸 cache invalidation。
- **未知 provider 不 raise**：graceful fallback + 一次性 warning，避免 prod 引入新 provider 時 404 callsite。
- **Empty drop**：空段不入 message list；保持 prefix 緊湊。

### 驗收

`pytest test_prompt_cache.py` → **23 pass / 0.06s**。覆蓋 order
enforcement / blank drop / Anthropic markers 三層 / OpenAI 無 marker /
Ollama warns-once / unknown fallback / empty provider / master switch
default + 10 個 truthy/falsy / metric round-trip / silent without prom。

### 後續

下一步可選：
1. **Phase 63-D Daily IQ Benchmark**（按主鏈順序，3–4h）
2. **Phase 67-D RAG Pre-fetch**（67-A 已就位、前置 episodic_memory 已有，3–4h）
3. **Phase 56-DAG-C mutation loop**（需 prompt_registry canary 推 Orchestrator prompt，4–6h）

Phase 67-A 不阻擋任何後續 phase；hot callsite 整合可在後續 phase 漸進
（如 56-DAG-C 的 Orchestrator agent 直接用 `CachedPromptBuilder`）。

---

## Phase 56-DAG-B — Storage + workflow 連動 完成（2026-04-14）

承 56-DAG-A validator 之後立即實作。新表 `dag_plans` + workflow_runs
雙向連結 + mutation chain，保留 Phase 56 append-only invariant
（舊 run 的 steps 永不改寫）。

### 交付（commit `b9a66d2`）

**DB 變更**：
- 新表 `dag_plans(id, dag_id, run_id, parent_plan_id, json_body, status, mutation_round, validation_errors, created_at, updated_at)` + 3 indexes。
- migrate `workflow_runs` 加 `dag_plan_id` + `successor_run_id`，`workflow_steps` 加 `dag_task_id`，全 nullable 向後相容。

**`backend/dag_storage.py`**（新）：
- `StoredPlan` dataclass + `.dag()` rehydrate + `.errors()`
- 狀態機（write-time guard）：
  ```
  pending → {validated, failed}
  validated → {executing, mutated, exhausted}
  failed → {mutated, exhausted}
  executing → {completed, mutated, exhausted}
  completed/mutated/exhausted → terminal
  ```
- CRUD：`save_plan` / `get_plan` / `get_plan_by_run` / `list_plans` / `set_status` / `attach_to_run` / `link_successor` / `get_dag_plan_id_for_run`

**`backend/workflow.py` 擴充**：
- `start(kind, *, dag=None, parent_plan_id=None, mutation_round=0)`：
  - `dag=None` → 既有行為完全不變（向後相容）
  - `dag=DAG` → 持久化 + dag_validator pass → status `validated→executing`；fail → status `failed`；雙向 link；persist 失敗不破壞 `workflow.start` 合約（全 try/except）
- `mutate_workflow(old_run_id, new_dag, *, mutation_round)`：
  - 開新 successor run
  - 舊 plan 標 `mutated`、舊 run 寫 `successor_run_id`
  - 新 plan `parent_plan_id` 指向舊 plan，mutation chain 完整可追溯

### 設計姿態

- **Append-only invariant 不破**：mutation 永遠開新 run/plan，舊資料只加 link，不 mutate steps。
- **狀態機 write-time guard**：illegal transition 在 set_status 即 raise，無法繞過。
- **Storage 失敗不傳染**：workflow.start 對 plan 持久化錯誤完全 swallow + log.warning，舊功能零中斷。
- **Validator 失敗不擋啟動**：DAG 失敗 → plan 標 `failed`，但 run 仍 `running`，由上層（56-DAG-C mutation loop）決定下一步。

### 驗收

`pytest test_dag_storage` → **13 pass / 132s**。覆蓋 CRUD round-trip / 狀態機 legal+illegal+terminal / workflow.start 含 dag+不含 dag 雙路徑 / mutation chain 雙端 link / list_plans 排序 / 防禦性測試（storage blowup 不破壞 start 合約）。

### 後續

下一個是 **Phase 67-A Prompt Cache**（純 LLM 層，與 DAG track 平行
可進）或 **Phase 63-D Daily IQ Benchmark**（依 HANDOFF 主鏈）。
56-DAG-C mutation loop 需先有 Orchestrator agent prompt（透過
prompt_registry 推上）→ 與 67-A 有間接依賴關係。

---

## Phase 56-DAG-A — DAG Schema + Validator 完成（2026-04-14）

第一個 DAG 子任，純 deterministic、無 LLM、無 DB。Validator 一次回所有
錯誤而非 first-fail，配合 Phase 56-DAG-C 的 mutation prompt 一輪可看
全貌。

### 交付（commit `bb42e0f`）

- `backend/dag_schema.py` — Pydantic `Task` + `DAG` 模型，schema_version=1，
  含 alnum task_id / 自依賴禁止 / depends_on 去重 / schema_version
  接受清單 / required_tier ∈ {t1, networked, t3}。
- `configs/tier_capabilities.yaml` — 三 tier × allow/deny toolchain
  外移；YAML 單一真實來源，Phase 65 訓練料可引用。
- `backend/dag_validator.py` — 7 條規則：
  - `duplicate_id` 同 task_id 重複
  - `unknown_dep` depends_on 指向不存在
  - `cycle` Kahn 拓撲排序；報未解 task 數
  - `tier_violation` toolchain 不在 allow 或在 deny
  - `io_entity` expected_output 必為 file path / `git:<sha>` / `issue:<id>`
  - `dep_closure` input 必來自 upstream `expected_output` 或 `external:` / `user:` 標記
  - `mece` 兩 task 同 output 必須 BOTH `output_overlap_ack=true`
  - 一次回所有錯，非 first-fail
- 新 metrics（with no-op fallback）：
  - `omnisight_dag_validation_total{result}` — passed / failed
  - `omnisight_dag_validation_error_total{rule}` — 7 rule label

### 驗收

`pytest test_dag_validator + intelligence + intelligence_mitigation +
prompt_registry + metrics` → **119 pass + 2 skip / 180s**（39 新 test
+ 80 既有，含 Pydantic schema 6 / happy path 2 / 結構違反 3 / tier
capability 4 / I/O entity 13 參數化 / dep closure 4 / MECE 3 / 全錯
彙整 / summary 格式 / metric pass + per-rule fail）。

### 設計姿態

- **Validator 不呼 LLM**：所有規則 deterministic，可 unit test 到鎖死；
  LLM Reviewer 留 v2。
- **All-errors-collected**：mutation prompt 一輪即可看到全部問題，避
  免「修一個 cycle、再被 tier 退一次」造成 mutation 振盪。
- **Tier 規則 YAML 外移**：新 toolchain 只改 yaml，不動 code。
- **MECE 留逃生口**：`output_overlap_ack=true` 雙方同意可允許，覆蓋
  並行 benchmark 等真實場景。
- **I/O 三類入口**：file path / `git:<sha>` / `issue:<id>` 對應檔案 /
  commit / 工單三類產物，已可涵蓋 95% 任務形態。

### 後續

**Phase 56-DAG-B Storage + workflow 連動** — 新表 `dag_plans` + workflow_runs
連動 + idempotency_key 加 `dag_task_id` 欄。

---

## Phase 56-DAG — Self-Healing Scheduling（重定，未實作；2026-04-14 規劃）

設計源：`docs/design/self-healing-scheduling-mechanism.md`（規劃 → 乾跑
→ 突變閉環 + 4 大黃金特徵 + Orchestrator 模板）。原 Phase 56 (Durable
Workflow Checkpointing) **已交付** (`4bb4b21`)，現擴充為 DAG-first
規劃層。原線性 step API 不變、向後相容；新增 `dag_plans` 表 + DAG
schema + validator + mutation loop + 雙模執行入口。

### 已敲定決策

1. 名稱：原 Phase 56 不重命名；新增子任 56-DAG-A/B/C/D。
2. **執行模式：B (AI auto-plan) 先，A (人手 DAG endpoint) 後** — B 改既有 chat 流即可，A 需新 endpoint+frontend。
3. **Validator：v1 純 deterministic**（Pydantic schema + 拓撲 + 規則表），不呼叫 LLM；LLM Reviewer 留 v2。
4. **Mutation bounded retry = 3 round**，超過 → Decision Engine `kind=dag/exhausted` severity=destructive，admin 介入。
5. **Tier capabilities** 抽到 `configs/tier_capabilities.yaml`，避免硬編碼且便於 Phase 65 訓練料引用。
6. **與 Phase 63-D / 65 順序**：56-DAG-A/B 先（驗證器越早就位、Phase 63-D IQ 題可加 DAG benchmark）。

### 子任 / 工時

| 子任 | 工時 | 內容 |
|---|---|---|
| **56-DAG-A** schema + validator | 4–5h | `backend/dag_schema.py`（Pydantic Task/DAG，schema_version 欄）+ `backend/dag_validator.py`（cycle detection、tier 合法性、tier-capability 規則、依賴閉包、I/O 實體化（accept file path / `git:<sha>` / `issue:<id>` 三類）、MECE on outputs（`output_overlap_ack=true` 例外））；deterministic、無 LLM、無 DB；~30 test |
| **56-DAG-B** storage + workflow 連動 | 3–4h | 新表 `dag_plans(id, dag_id, run_id, json_body, status, mutation_round, created_at)`；`workflow_steps.idempotency_key` 加 `dag_task_id` 欄；`workflow.start(dag_id=...)` 接 DAG plan；mutation 改 DAG 開新 run，舊 run 標 `mutated` 並記 `successor_run_id` |
| **56-DAG-C** mutation loop + Orchestrator agent | 4–6h | `backend/dag_planner.py::propose_mutation(dag, errors)` 把錯誤串成 prompt → call orchestrator agent → 新 DAG → re-validate → ≤3 round；超過 file Decision Engine `kind=dag/exhausted` severity=destructive；orchestrator prompt 註冊於 `backend/agents/prompts/orchestrator.md`（走 prompt_registry canary） |
| **56-DAG-D** 雙模 + ops 文件 | 2–3h | Mode B 改 chat router 內部走 Orchestrator → DAG → validator；Mode A `POST /api/v1/dag` 接 JSON（opt-in 進階模式）；ops doc + HANDOFF |

**累計工時**：13–18h，分 4 commit 批。

### 新環境變數（規劃）

```
OMNISIGHT_DAG_PLANNING_MODE=auto   # auto | manual | both
OMNISIGHT_DAG_MUTATION_MAX_ROUNDS=3
```

### 新 metrics（規劃）

- `omnisight_dag_validation_total{result}` — passed / failed
- `omnisight_dag_mutation_total{result}` — recovered / exhausted
- `omnisight_dag_validation_error_total{rule}` — cycle / tier_violation / mece / io_entity / dep_closure

### 新 audit actions（規劃）

- `dag_validated`、`dag_mutated`、`dag_exhausted`、`dag_dispatched`

### 新 Decision Engine kinds（規劃）

- `dag/validation_failed` (severity=routine，每次 mutation round)
- `dag/exhausted` (severity=destructive，admin 介入)

### 與既有系統的接點

- **Phase 56 Workflow** (`4bb4b21`)：`workflow_runs` 加 `dag_plan_id` FK 欄；既有線性 step API 不變。
- **Phase 64-A/B** Sandbox：`Task.required_tier` 強制 `container.start_container(tier=)` 一致。
- **Phase 63-A IIS**：DAG validation failure rate 是新指標餵 IIS window；mutation 振盪 → IIS L2 route。
- **Phase 63-C** prompt_registry：Orchestrator agent prompt 走 canary。
- **Phase 62 Knowledge Generation**：成功 DAG plan + workflow_run → skill candidate。
- **Phase 65** Data Flywheel：題庫第 11–20 題加 DAG planning benchmark；DAG validation 通過率作 quality signal。

### 風險摘要

| 風險 | 等級 | Mitigation |
|---|---|---|
| Mutation loop 振盪 | 高 | bounded retry=3 + Decision Engine destructive 升級 |
| Reviewer LLM 成本爆炸 | 高 | v1 純 deterministic，LLM Reviewer 留 v2 |
| `<thinking>` self-check 雞生蛋 | 嚴重 | **完全不信** — 全靠 deterministic validator + DE gate |
| 「人手 vs AI auto」雙模衝突 | 中 | Mode B 預設、Mode A opt-in |
| MECE output 偵測誤殺 | 中 | `output_overlap_ack=true` 註釋例外 |
| Tier 規則表硬編碼難維護 | 中 | YAML 外移 + unit test |
| 與 stuck_detector spawn_alternate 跨 tier | 中 | 重派也走 validator；fail → IIS L2 |
| DAG schema 變動向後不相容 | 低 | `schema_version` 欄 + validator 接受多版本 |

### 預估效益

- **Token 用量**：-20–40%（爛 DAG 在 dry-run 即被擋下）。
- **失敗時點**：執行中崩 → 規劃時拒；MTTR 大幅縮短。
- **Phase 64 沙盒守則自動執行**：`required_tier` 與 `container.start_container(tier=)` 強制一致。
- **Phase 63-A IIS 訊號乾淨**：規劃錯不再污染 code_pass_rate。
- **Audit 完整性**：mutation round 可追溯，Phase 65 訓練料品質提升。

### 啟動順序（已調整）

```
[已完成] 64-A ✅ + 64-D ✅ + 64-B ✅ + 62 ✅ + 63-A ✅ + 63-B ✅ + 63-C ✅
   ↓
56-DAG-A (validator)               ← 下一步、最高 ROI、無 LLM 依賴
   ↓
56-DAG-B (storage + workflow 連動)
   ↓
63-D (Daily IQ Benchmark — 含 DAG 題)
   ↓
56-DAG-C (mutation loop + Orchestrator)
   ↓
56-DAG-D (雙模執行 + ops)
   ↓
65 (Data Flywheel — 64-B 已就位)
   ↓
63-E (Memory Decay)
64-C (T3 Hardware Daemon) — 等實機，獨立 track
```

---

## Phase 67 — Lossless Agent Acceleration（重定，未實作；2026-04-14 規劃）

設計源：`docs/design/lossless-agent-acceleration.md`（4 引擎：Prompt
Cache / Diff Patch / Speculative Pre-warm / RAG Pre-fetch）。目標
prod 端 token -40~60%、end-to-end 延遲 -30~50%，**不犧牲精度**。

### 已敲定決策

1. **新編號 Phase 67**（不併入既有 Phase）— 4 引擎跨層級（LLM /
   tool / sandbox / RAG），不適合塞單一既有 Phase。
2. **E1 Provider 順序**：Anthropic-first → OpenAI auto → Ollama no-op +
   warning。抽象層在 `agents/llm.py`。
3. **E2 違規處置**：軟反饋（IIS L1 calibrate）而非硬重啟，避免無限迴圈。
4. **E2 既有 `write_file`**：標 deprecated 漸進，保留 1 phase fallback。
5. **E3 Pre-warm 觸發**：DAG validator pass + in-degree=0 + 前 **N=2** 名。
6. **E4 confidence 門檻**：v1 起 **0.5**，待 Phase 63-E memory decay 完成後可上調 0.7。
7. **與 56-DAG 順序**：**67-A 立即可平行啟動**（純 LLM 層無 dependency）；
   67-B/C/D 卡在後續 phase。

### 子任 / 工時

| 子任 | 工時 | 內容 |
|---|---|---|
| **67-A** Prompt Cache 標記層 | 3–4h | `agents/llm.py::CachedPromptBuilder` (`add_static` / `add_volatile`)；message 順序契約 `system → tools → static_kb → conversation → volatile_log`；Anthropic `cache_control: ephemeral` 注入；OpenAI auto；Ollama no-op + warning；新 metric `prompt_cache_hit_total{provider}` / `prompt_cache_miss_total` |
| **67-B** Diff Patch 工具 + 強制契約 | 5–7h | `agents/tools/patch.py::apply_search_replace`（≥3 行 context、唯一性檢查）+ `apply_unified_diff`；`write_file` 對既有檔 raise → 引導 patch；`create_file` 用於新檔不受 cap；攔截器 token>N 且 modify-existing → reject + 觸發 IIS L1；System prompt 規範段透過 prompt_registry (63-C) canary 推上 |
| **67-C** Speculative Pre-warm | 4–5h | `sandbox_prewarm.py::prewarm_for(dag, depth=2)` 對 in-degree=0 task 預先 pull image + start container（重用 64-A `start_container` 含 image trust）；DAG dispatcher (56-DAG-D) 呼叫；mutation/cancel 立即 stop_container 釋放 lifetime；新 metrics `prewarm_started_total` / `prewarm_consumed_total{result}` |
| **67-D** RAG Pre-fetch on Error | 3–4h | `rag_prefetch.py::intercept_failed_step(error_log)` rc≠0 即從 `episodic_memory` (Phase 18) FTS5 查 → confidence ≥ 0.5 過濾 → top 3 包成 `<related_past_solutions>` block 標 cacheable；注入點 workflow.py step error path + invoke.py error_check_node；與 Phase 63-E quality_score 共用 |

**累計工時**：15–20h（4 子任分批，可與 56-DAG / 63-D 部分平行）。

### 與既有系統的接點

- **Phase 56-DAG**：E2 patch 是 step-level，與 DAG `expected_output`
  (task-level artifact) 解耦；E3 pre-warm 直接讀 DAG dependency
  graph；E4 RAG 注入點在 step error path。
- **Phase 63 IIS**：E2 違規 → L1 calibrate（教 SEARCH/REPLACE 格式）；
  連 3 次 → L2 route；token entropy baseline 需加 `mode={normal,patch}`
  區分（避免 patch 短回覆觸發 entropy 警報）。
- **Phase 63-C prompt_registry**：E2 規範段 + E1 cache hint marker
  皆走 canary 推上。
- **Phase 64-A image trust**：pre-warm 必須通過同樣的 trust check，
  不可繞 trust list。
- **Phase 64-D lifetime cap**：pre-warm 啟動的容器同樣受 45min cap；
  cancel 釋放避免資源浪費。
- **Phase 65 Data Flywheel**：patch diff 比 full file 更易做 fine-tune
  料；E1 cache hit log 可作 prompt quality signal；E4 命中歷史解法的
  成功率作 quality score。

### 新環境變數（規劃）

```
OMNISIGHT_PROMPT_CACHE_ENABLED=true        # 67-A
OMNISIGHT_PATCH_ENFORCE_MODE=warn|reject   # 67-B 漸進
OMNISIGHT_PATCH_MAX_INLINE_LINES=50
OMNISIGHT_PREWARM_DEPTH=2                  # 67-C
OMNISIGHT_RAG_MIN_CONFIDENCE=0.5           # 67-D
OMNISIGHT_RAG_TOP_K=3
```

### 新 metrics（規劃）

- `omnisight_prompt_cache_hit_total{provider}` / `prompt_cache_miss_total`
- `omnisight_patch_apply_total{result}` — applied / search_ambiguous / not_found / size_violation
- `omnisight_patch_violation_total{reason}`
- `omnisight_prewarm_started_total` / `prewarm_consumed_total{result}` — hit / miss / cancelled
- `omnisight_rag_prefetch_total{result}` — injected / no_hit / below_confidence

### 風險摘要

| 風險 | 等級 | Mitigation |
|---|---|---|
| Diff 唯一性失敗無限重試 | 高 | ≥3 行 context + 連 3 次失敗→IIS L1 calibrate |
| Generated/template 50-line cap 誤殺 | 中 | modify vs create 區分；create 不受 cap |
| Pre-warm 浪費 docker / lifetime | 中 | 只對 DAG-validated + in-degree=0 + 前 N=2；mutation 立即釋放 |
| L3 poisoning → RAG 注入錯解 | 高 | confidence ≥ 0.5 過濾 + 等 63-E decay |
| RAG 注入導致 input token 反增 | 中 | top 3 cap + cacheable marker |
| 違規重啟造成模型 stuck | 高 | 軟反饋（IIS）而非硬重啟 |
| 跨 provider 不對稱 | 中 | ops doc + healthz `prompt_cache_supported{provider}` |
| 與 IIS token entropy 警報互斥 | 中 | patch response 走獨立 baseline |
| Anthropic API cost 結構變動 | 低 | 抽象層集中、易調 |

### 預估效益（量化）

| 引擎 | 預估改善 | 條件 |
|---|---|---|
| E1 Prompt Cache | TTFT -80% / Input token -50% | Anthropic / OpenAI / 重複任務 |
| E2 Diff Patching | Output token -70% / 生成時間 -85% | 既有檔修改 |
| E3 Pre-warm | 任務感知延遲 -2~5s/task | DAG 已驗證 |
| E4 RAG Pre-fetch | 重複錯誤 MTTR -10~15s | L3 命中 |

合計：prod 端 token **-40~60%**、end-to-end 延遲 **-30~50%**。

### 啟動順序（已調整入主鏈）

```
[已完成] 64-A ✅ + 64-D ✅ + 64-B ✅ + 62 ✅ + 63-A ✅ + 63-B ✅ + 63-C ✅ + 56-DAG-A ✅
   ↓
56-DAG-B (storage + workflow 連動)  ──┐
   ↓                                  │ 平行
67-A (Prompt Cache)                   ┘ — 純 LLM 層、無 dependency
   ↓
63-D (Daily IQ Benchmark)
   ↓
67-D (RAG Pre-fetch)                  — 需 episodic_memory + 強過濾
   ↓
56-DAG-C (mutation loop + Orchestrator)
   ↓
67-B (Diff Patch + 強制契約)          — 需 prompt_registry canary
   ↓
56-DAG-D (雙模執行 + ops)
   ↓
67-C (Speculative Pre-warm)           — 需 DAG dispatcher
   ↓
65 (Data Flywheel) → 63-E (Memory Decay) → 64-C(平行)
```

---

## Phase 63-C — Prompt Registry + Canary 完成（2026-04-14）

吸收原 Phase 63 Meta-Prompting Evaluator 主體並落地。Prompt 從 code 抽
為 DB 行；5% deterministic canary、7 天窗口、自動 rollback。

### 交付（commit `65a98ea`）

**新表 `prompt_versions`**：(path, version, role, body, body_sha256,
success/failure_count, created/promoted/rolled_back_at, rollback_reason)；
UNIQUE(path, version)，索引 (path, role)。

**`backend/prompt_registry.py`**：

| 函式 | 行為 |
|---|---|
| `_normalise_path` | 白名單：僅 `backend/agents/prompts/**.md`；明確拒 `CLAUDE.md`（L1-immutable） |
| `register_active(path, body)` | 同 body idempotent；否則舊 active → archive、version+1 |
| `register_canary(path, body)` | 取代既有 canary（rollback_reason=superseded） |
| `pick_for_request(path, agent_id) → (version, role)` | blake2b(agent_id) % 100 < 5 走 canary；deterministic 可重播 |
| `record_outcome(version_id, success)` | 累加 per-version counter（Phase 63-A IIS 餵 source） |
| `evaluate_canary(path, min_samples=20, regression_pp=5, window_s=7d)` | 回 `{no_canary, insufficient_samples, rollback, keep_running, promote_canary}`；regression > 5pp 即 auto-archive canary |
| `promote_canary(path)` | operator action：canary → active、舊 active → archive |

### 設計姿態

- **deterministic canary**：incident replay 不會「碰運氣」走到不同 lane。
- **path 白名單嚴格**：CLAUDE.md / L1 規則文件永禁；`.md` 副檔強制；
  路徑 escape 一律 PathRejected。
- **auto-rollback 但非 auto-promote**：跌過 5pp 自動回滾；通過則回
  `promote_canary` 等 operator 拍板。
- **idempotent register_active**：同 body 不會無謂炸版本號。
- **outcome 累計而非個別行**：節省寫入；版本級 pass rate 即為信號。

### 新 metrics

- `omnisight_prompt_outcome_total{role,outcome}` — Counter
- `omnisight_prompt_rolled_back_total{path}` — Counter

### 驗收

`pytest test_prompt_registry + test_intelligence + test_intelligence_mitigation + test_db + test_metrics`
→ **93 pass + 2 skip / 5.02s**。19 test 覆蓋路徑白名單 4 邊界、
register_active 三路徑、canary supersession、pick 5% 偏差容忍 (1000
draws / 期待 20–90)、deterministic per agent_id、evaluate 五決策、
promote 兩路徑。

### 後續

**Phase 63-D Daily IQ Benchmark** — 手動策展 10 題、nightly 跑 active
+ chain 中其他 model、低於 baseline 連 2 天 → Notification。

---

## Phase 63-B — IIS Mitigation Layer 完成（2026-04-14）

承 Phase 63-A 之後立即實作。把 signal-only 的 alerts 對應到 Decision
Engine 三級 kind，**只負責提案，不執行 strategy**（與 stuck/* 同模式，
應用層在 consumer 側）。

### 交付（commit `860be3a`）

`backend/intelligence_mitigation.py`：

| 級 | kind | severity | default | 內容 |
|---|---|---|---|---|
| L1 | `intelligence/calibrate` | routine | calibrate | options {calibrate, skip}；calibrate 描述帶 profile-aware COT char budget |
| L2 | `intelligence/route` | risky | calibrate（safer than switch_model） | options {switch_model, calibrate, abort} + warning Notification |
| L3 | `intelligence/contain` | destructive | halt | options {halt, switch_model} + critical Notification + 可選 Jira |

### 對應規則

```
empty alerts            → no proposal
any warning             → L1 calibrate
any critical            → L2 route
critical + L2 already open → escalate to L3 contain
```

`map_alerts_to_level` 永不從單次 snapshot 直接產出 contain — escalation 是唯一路徑。

### 鎖定決策實裝

- **Profile-aware COT**：cost_saver=0 / sprint=100 / BALANCED=200 / QUALITY=500（讀 `budget_strategy.get_strategy()`，profile 切換立即生效）。
- **Jira containment 預設 off**：`OMNISIGHT_IIS_JIRA_CONTAINMENT=true` 才走 [IIS-CONTAIN] tagged Jira。
- **Dedup 同 stuck/***：`_open_proposals[(agent_id, level)] = dec.id`，consumer 側 `on_decision_resolved(agent_id, level)` 釋放。

### 新環境變數

```
OMNISIGHT_IIS_JIRA_CONTAINMENT=false   # 預設 off
```

### 驗收

`pytest test_intelligence_mitigation + intelligence + decision_engine + decision_api + dispatch + observability`
→ **105 pass / 2.61s**。20 test 覆蓋 4 profile COT 長度 + fallback / 4 map 規則 / 3 tier 提案 kind+severity+default / dedup 同 agent + 跨 agent / route→contain 升級 / resolved callback 釋放 / L3 critical Notification / Jira default off / Jira env-on / snapshot 暴露狀態。

### 後續

**Phase 63-C Prompt Registry + Canary** — 把 prompt 從 code 抽到
`backend/agents/prompts/*.md` + DB 版本表 + 5% canary + 7 天監控 +
auto-rollback。

---

## Phase 63-A — IIS Signal Layer 完成（2026-04-14）

設計源：`docs/design/intelligence-immune-system.md` §一. 第一個 IIS
子任：訊號收集 + Prometheus 公開，**完全不觸發應變**（mitigation 是
63-B 的職責）。

### 交付（commit `cd34dae`）

`backend/intelligence.py` 提供四指標滑動窗口：

| 指標 | 計算 | 警報門檻 |
|---|---|---|
| `code_pass` | 通過 / 總數 | warn < 60%、critical < 30%（升級式，互斥） |
| `compliance` | HANDOFF.md 觸碰率 | warn < 70%（**git diff 餵入，禁 LLM 自查**） |
| `consistency` | Jaccard(proposed, L3 historical) 平均 | warn < 0.3 |
| `entropy` | 最新 response_tokens vs window z-score | warn |z| > 2 |

公開 API：
- `IntelligenceWindow(agent_id, size=10).record(...)` / `.score()` / `.alerts()`
- `get_window(agent_id)` — 進程內 singleton
- `record_and_publish(agent_id, **kw) → (score, alerts)` — 同步回傳並 push 到 Prometheus

### 新 metrics

- `omnisight_intelligence_score{agent_id,dim}` — Gauge
- `omnisight_intelligence_alert_total{agent_id,dim,level}` — Counter

### 設計姿態

- **signal-only**：本層完全不觸發任何 mitigation；只負責計算 + 公開。Phase 63-B 才把 alerts 餵給 Decision Engine。
- **Jaccard v1 而非 embedding**：deterministic、可測；真實 embedding 留到後期。
- **escalation 互斥**：critical 觸發時不再 warn 同一 dim，避免 pager double-fire。
- **HANDOFF compliance 由 caller 餵 bool**：本模組不自己 check，徹底排除 LLM 自查的雞生蛋。
- **空窗口 / 不足樣本回 None**：alert 也 None-safe，不會在 cold start 噴假警。

### 驗收

`pytest backend/tests/test_intelligence.py + metrics + skills_extractor + observability`
→ **66 pass + 2 skip / 0.81s**。27 test 覆蓋 Jaccard 邊界 / window 基礎 / 4 指標數學 / 閾值觸發 / critical-supersedes-warning / singleton / Prometheus publish。

### 後續解鎖

**Phase 63-B Mitigation Layer** — 把本層的 `(level, dim, reason)` 對應到
Decision Engine 三 kind（intelligence/calibrate, route, contain），重
用 Stuck Detector 的 `_open_proposals` 去重。

---

## Phase 62 — Knowledge Generation 完成（2026-04-14）

設計源：`docs/design/agentic-self-improvement.md` L1。沙盒前置已完成
（64-A/D/B），技能檔可安全產生 + 審核 + 執行。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | `backend/skills_scrubber.py` — 12 類 deny-list（AWS / GitHub PAT / GitLab PAT / OpenAI / Anthropic / Slack / JWT / SSH 私鑰 / env 賦值 / email / /home /Users /root paths / IPv4 非 loopback / 高 entropy 通用），`SAFETY_THRESHOLD=25` 拒絕過敏感來源；20 test | `1ab7cb3` |
| S2 | `backend/skills_extractor.py` — `should_extract`（≥5 step OR ≥3 retry）+ template 渲染 + 自動 scrub + Decision Engine `kind=skill/promote` severity=routine 24h timeout default-safe=`discard`；`is_enabled` 讀 `OMNISIGHT_SELF_IMPROVE_LEVEL`；新 metrics `skill_extracted_total{status}` + `skill_promoted_total`；17 test | `9dcbe8d` |
| S3 | `workflow.finish()` hook（completed run + L1 enabled → extract + propose，全 `try/except` 包覆不破壞 finish 合約）；`backend/routers/skills.py` 提供 `/skills/pending`（list/read operator+）+ `/skills/pending/{name}/promote`（admin，移入 `configs/skills/<slug>/SKILL.md`）+ `DELETE`（admin）；audit log `skill_promoted` / `skill_discarded`；path traversal 防護；10 test | `5b25e77` |
| S4 | `docs/operations/skills-promotion.md` 操作員指南 + 本 HANDOFF | _本 commit_ |

### 設計姿態

- **v1 模板而非 LLM**：deterministic、可測試、可審；LLM 重寫留作 Phase 62.5。
- **opt-in 預設 off**：`OMNISIGHT_SELF_IMPROVE_LEVEL` 不設則整個 hook 不跑。
- **default-safe = `discard`**：Decision Engine 24h timeout 後自動丟棄而非自動上架。
- **失敗 run 不入庫**：避免「記住失敗解法」造成負面 feedback。
- **scrubber 過敏感即拒寫**：超過 25 個 redaction 直接不產出檔案，連標記都不留。

### 新環境變數

```
OMNISIGHT_SELF_IMPROVE_LEVEL=l1  # off | l1 | l1+l3 | all
```

### 新 metrics

- `omnisight_skill_extracted_total{status}` — written / skipped_threshold / skipped_unsafe
- `omnisight_skill_promoted_total` — operator-approved 移入 live tree

### 新 audit actions

- `skill_promoted`（actor admin email）
- `skill_discarded`（actor admin email）

### 新 endpoints

- `GET    /api/v1/skills/pending`
- `GET    /api/v1/skills/pending/{name}`
- `POST   /api/v1/skills/pending/{name}/promote`（admin）
- `DELETE /api/v1/skills/pending/{name}`（admin）

### 驗收

`pytest backend/tests/test_skills_*.py + decision_engine + observability + metrics + audit` → **100 pass + 2 skip / 2.11s**。47 新 test 覆蓋 scrubber 12 redaction 類別、extractor trigger gate / 模板輸出 / scrub 整合 / opt-in 7 級別 / Decision Engine wiring、workflow.finish hook 4 路徑、4 個 endpoint。

### 後續解鎖

**Phase 63-A IIS Metrics Collector** 可立即啟動（Phase 62 產出的技能檔
即將成為 Phase 63-B mitigation L1 的 few-shot 注入來源）。

---

## Phase 64-B — Tier 2 Networked Sandbox 完成（2026-04-14）

承 Phase 64-A + 64-D 之後。**T2 與 T1 完全相反**：公網 ACCEPT、
RFC1918 / link-local / ULA DROP。用於 MLOps 資料下載、第三方 API
測試，及 Phase 65 訓練資料外送。

### 設計分工

- **Python 側 (backend)**：擁有 docker bridge `omnisight-egress-t2`、
  決定 `--network` 旗標、重用 64-A 的 runtime / image trust /
  lifetime。**無 env 雙 gate** — 進入點 `start_networked_container()`
  即是 gate（呼叫端負責 Decision Engine 審核）。
- **Host 側 (operator)**：跑一次 `scripts/setup_t2_network.sh` 安裝
  iptables IPv4/IPv6 規則。

### 子任 / commit

| 子任 | 內容 |
|---|---|
| S1 | `sandbox_net.ensure_t2_network` / `resolve_t2_network_arg`；`start_container(tier=...)` 加 `tier` 參數；`start_networked_container()` 公開別名；metric / audit / lifetime tier 全程貫穿 |
| S2 | `scripts/setup_t2_network.sh` — IPv4 + IPv6 雙 chain，DROP RFC1918 / 100.64/10 / link-local / 多播 / ULA / fe80::/10，預設 ACCEPT 公網 |
| S3 | ops doc 增 §7 Tier 2 + 本 HANDOFF 條目 |

### 驗收

`pytest backend/tests/test_sandbox_t2.py 加 既有 sandbox bundle`
→ **77 pass + 2 skip / 1.66s**。

T2 9 test 覆蓋：
- bridge name 與 T1 區隔
- bridge create 冪等 / 重複跳過
- `resolve_t2_network_arg` happy path / fail-fast raise
- `start_networked_container` 傳遞 `--network omnisight-egress-t2`
- T1 預設仍走 `--network none`
- launch metric `tier="networked"` / audit `after.tier="networked"`

### 後續解鎖

**Phase 65 Data Flywheel** 解除阻擋（外送訓練資料現可走 T2 egress
而不違反「T0 不執行外送」原則）。

---

## Phase 64-D — Killswitch 統一 完成（2026-04-14）

承 Phase 64-A 完成後立即實作。原計畫 4 小項，**D2 重審後刪除**，
理由：`subprocess_orphan_total{target}` 既有 label 描述 CI 整合
（Jenkins / GitLab）的子程序，與沙盒 tier 不同領域，硬塞 `tier`
label 會稀釋語義；保留現狀。

### 子任 / commit

| 子任 | 內容 | commit |
|---|---|---|
| D1 | 驗證 `_lifetime_killswitch(tier=...)` 對 T2 已可重用（S4 已預留） | （無新 code） |
| D2 | **不做** — 見上述理由 | — |
| D3 | `exec_in_container` 輸出超 `OMNISIGHT_SANDBOX_MAX_OUTPUT_BYTES`（預設 10 KB）即截斷 + marker；新 metric `omnisight_sandbox_output_truncated_total{tier}` | _本 commit_ |
| D4 | `/healthz` 增 `sandbox: {launched, errors, lifetime_killed, image_rejected, output_truncated}` 區塊（從 Counter 即時計算） | _本 commit_ |

### 新環境變數

```
OMNISIGHT_SANDBOX_MAX_OUTPUT_BYTES=10000   # 0 = 停用
```

### 新 metric

- `omnisight_sandbox_output_truncated_total{tier}` — Counter

### 驗收

`pytest backend/tests/test_sandbox_killswitch.py 加 既有 sandbox bundle`
→ **68 pass + 2 skip / 1.36s**。

### 後續解鎖

Phase 64-A + 64-D 全套就位 → 沙盒可觀測 + 可控制 + 可破壞性 cap。
**Phase 62 / 64-B 正式可啟動**。

---

## Phase 64-A — Tier-1 Sandbox Hardening 完成（2026-04-14）

設計源：`docs/design/tiered-sandbox-architecture.md`。整個 Phase 64
拆為 A/B/C/D，本次完成 A 全部六子任務。

### 子任務與 commit

| 子任 | 內容 | commit |
|---|---|---|
| S1 | gVisor (`runsc`) opt-in + runc fallback + cached probe | `a192ba4` |
| S2 | T1 egress 雙 gate + `omnisight-egress-t1` bridge + iptables operator script | `9ae5134` |
| S3 | image digest allow-list（拒絕 fail-open；`.Id` 非 `RepoDigest`） | `4a993b8` |
| S4 | 45 min wall-clock killswitch + audit `sandbox_killed reason=lifetime` | `987b695` |
| S5 | `sandbox_launch_total{tier,runtime,result}` + audit `sandbox_launched` / `sandbox_image_rejected`；附帶修一個 prod-blocker UnboundLocalError | `4ebe7a6` |
| S6 | `docs/operations/sandbox.md` 操作員指南 + 本 HANDOFF 條目 | _本 commit_ |

### 新環境變數

```
OMNISIGHT_DOCKER_RUNTIME=runsc            # gVisor，缺則 fallback runc
OMNISIGHT_T1_ALLOW_EGRESS=false           # 雙 gate 之一
OMNISIGHT_T1_EGRESS_ALLOW_HOSTS=          # 雙 gate 之二（CSV）
OMNISIGHT_DOCKER_IMAGE_ALLOWED_DIGESTS=   # CSV sha256:..；空 = 開放
OMNISIGHT_SANDBOX_LIFETIME_S=2700         # 45 min；0 = 停用
```

### 新 metrics

- `omnisight_sandbox_launch_total{tier,runtime,result}` — success / error / image_rejected
- `omnisight_sandbox_image_rejected_total{image}`
- `omnisight_sandbox_lifetime_killed_total{tier}`

### 新 audit actions

- `sandbox_launched`（actor `agent:<id>`）
- `sandbox_killed`（actor `system:lifetime-watchdog`）
- `sandbox_image_rejected`（actor `agent:<id>`）

### 驗收

`pytest backend/tests/test_sandbox_t1_*.py test_metrics.py
test_observability.py test_audit.py` → **66 passed + 2 skip / 1.87s**。

### 副產

S5 testing 揭露並修復 `start_container` 中 `from backend.events
import emit_pipeline_phase` 局部 import 因 Python scope 規則整個函式
遮蔽 module-level 名稱 → 大多數啟動路徑都會 `UnboundLocalError`。
真實 prod-blocker，已修。

### 後續解鎖

Tier-1 沙盒就位 → **Phase 62 Knowledge Generation** 與 **Phase 64-D
Killswitch 統一**可立即啟動。**Phase 64-B** (T2 networked) 是
**Phase 65** Data Flywheel 的硬性前提。**Phase 64-C** (T3 hardware
daemon) 為獨立 track，需實機環境。

---

## Phase 52-Fix-D 進行中 — 測試覆蓋補強（2026-04-14）

### D1 — `backend/db.py` CRUD smoke（commit `a859329`）

1,214 LOC 資料層過去僅靠 router/engine 測試間接觸及；新增
`backend/tests/test_db.py` 13 case，每區一個 round-trip + 至少一項
mutation：

| 涵蓋表 | 測試重點 |
|---|---|
| agents | upsert 冪等、JSON progress round-trip、delete idempotent |
| tasks | labels/depends_on JSON 解碼、default child_task_ids |
| task_comments | ORDER BY timestamp DESC、多筆 |
| token_usage | ON CONFLICT(model) 更新 |
| handoffs | upsert 置換、get missing 回空字串 |
| notifications | level filter、mark_read、count_unread 多 level、failed list |
| artifacts | task_id/agent_id filter、delete |
| npi_state | get empty default、save 覆寫 |
| simulations | whitelist 列更新（bogus column 被過濾）、status filter |
| debug_findings | INSERT OR IGNORE 冪等、update status |
| event_log | event_type filter、cleanup days=0 |
| episodic_memory | 完整 CRUD |
| decision_rules | replace_rules 原子置換 |

### 驗收

`pytest backend/tests/test_db.py` → **13 passed in 1.34s**。  
與 observability / decision_api / audit / dispatch 合併 → **49 passed**。

### D2 — `backend/models.py` Pydantic validation（commit `71693c5`）

`backend/tests/test_models.py` 20 case，0.06s：

- Required-field enforcement（Agent / Task / Notification / Simulation）
- Enum coercion + rejection（AgentType / TaskPriority / TaskStatus /
  NotificationLevel / MessageRole / SimulationTrack / SimulationStatus）
- `default_factory` 產生獨立 instance（sub_tasks / progress / workspace）
- ISO-8601 timestamp default
- Nested model round-trip（Agent w/ sub_tasks + workspace、Task w/ list
  fields、OrchestratorMessage w/ suggestion）
- Subset model default（AgentCreate / TaskCreate）
- ChatRequest("") 接受 — 明列為當前 contract，後續若要加 min_length 會
  自動在此失敗

### D3 — `backend/events.py` EventBus（commit `de36358`）

`backend/tests/test_events_bus.py` 10 case，0.19s：

- subscribe/unsubscribe 計數正確、`discard` 冪等
- publish 單點 / 多點 fan-out、自動 timestamp、尊重 caller timestamp
- 無訂閱者時 publish no-op（不 raise，不計 drop）
- **Backpressure**：用 monkeypatch 縮 Queue maxsize=2 驗 slow subscriber
  被移出 `_subscribers`、`subscriber_dropped` 遞增
- `emit_agent_update` 走 singleton bus
- `emit_tool_progress` output 硬上限 1000 char
- singleton `bus` 為 EventBus 實例

### D4 — DLQ edge cases（commit `66b8a77`）

`backend/tests/test_notifications_dlq.py` 4 pass + 1 env-skip，0.56s：

- 兩個並發 sweep 在同一 failed row 上 → 合計 dead/retried 有界；掃完不再
  現身於 `list_failed_notifications`。
- 可重試 row 並發 → retried 合計 ≤ 2（每 sweep 至多一次）。
- `run_dlq_loop` cancel → task 乾淨結束、`_DLQ_RUNNING` 於 `finally`
  歸 False。
- 已在跑時第二次呼叫 `run_dlq_loop()` → 立即返回，不起第二組迴圈。
- `persist_failure_total` label cardinality 允許集合白名單測試
  （env-skip 若 prometheus_client 缺席）。

### D5 — `backend/metrics.py` registry integrity（commit `76d2e91`）

`backend/tests/test_metrics.py` 16 case（14 prom + 2 no-op）：

**真實 registry 支線**（prom 安裝時）：
- `reset_for_tests` 後 11 個模組級 metric attr 全部 rebind
- 9 個 labelled metric 參數化測試，各自接受聲明 label
- 未知 label raise（prom 不變量）
- 無 label Gauge `set/collect` round-trip
- `render_exposition` 回 text/plain + `omnisight_decision_total`
- `REGISTRY.collect()` 包含全部 11 族

**No-op fallback 支線**（prom 缺席時）：
- `_NoOp` chaining `labels().inc()` / `.observe()` / `.set()` 全不 raise
- `render_exposition` 回 placeholder body

雙向驗證：安裝 prom → 14 pass + 2 skip；無 prom → 2 pass + 14 skip。

### D6 — core smoke: budget_strategy / config / structlog（commit `7c77f75`）

三個小型基礎模組補 smoke test，總計 30 pass + 1 documented-skip / 0.12s：

- `test_budget_strategy.py`（10）：default=balanced、list_strategies 4 筆
  鍵齊全、set_strategy 參數化 4 strategy × tier/retries、enum + string
  接受、unknown raise、`quality` 不 downgrade 不變量、`sprint` 唯一
  `prefer_parallel=True`。
- `test_config.py`（13）：預設值、`OMNISIGHT_*` env 覆寫（provider /
  numeric / bool）、`get_model_name` 對 5 provider 的 fallback、明確
  override 優先、未知 provider 降回 anthropic 預設。
- `test_structlog_setup.py`（7 + 1 skip）：`is_json` 大小寫容忍、
  `configure` idempotent 雙模式、`bind_logger` 兩後端（structlog /
  LoggerAdapter）、empty context、`get_logger(None)` 回 root。

### D7 — frontend hooks coverage（commit `90eb637`）

`test/hooks/use-mobile.test.tsx`（6）：desktop/mobile 返回值、767/768 邊界、
matchMedia change 回應、add/remove listener 生命週期。

`test/hooks/use-engine.test.tsx`（5）：初始 state + 完整 callable 表面、
`patchAgentLocal` 僅更目標 agent、missing id no-op、`setAgents` functional
updater、offline addAgent fallback（`connected=false` 時本地合成 agent）。

Vitest 全套 13 files / 66 pass / 1.8s。

### Fix-D 整體完成

Fix-D 七個子批全數交付：

| 子項 | 檔案 | 測試數 | commit |
|---|---|---|---|
| D1 | `test_db.py` | 13 | `a859329` |
| D2 | `test_models.py` | 20 | `71693c5` |
| D3 | `test_events_bus.py` | 10 | `de36358` |
| D4 | `test_notifications_dlq.py` | 4+1skip | `66b8a77` |
| D5 | `test_metrics.py` | 16（雙模式） | `76d2e91` |
| D6 | `test_budget_strategy.py` / `test_config.py` / `test_structlog_setup.py` | 30+1skip | `7c77f75` |
| D7 | `test/hooks/use-mobile.test.tsx` / `use-engine.test.tsx` | 11 | `90eb637` |

**總計新增**：~104 backend test + 11 frontend test。

**Tier-3 延期**：`github_app`、`issue_tracker`、`sdk_provisioner`、
`model_router`、`container`、`workspace`、`ambiguity`、`decision_defaults`、
`main`、`sse_schemas`、`report_generator`、`git_credentials` 共 12 模組
仍無直接 test，已排入未來 Phase 66。

### Phase 62 解鎖

Fix-D 完成 → **Phase 62 Knowledge Generation 可啟動**。workflow_runs /
audit_log / notifications 皆有 test 保護，skills_extractor 可安心讀取。

---

## Phase 52-Fix-C — 前端穩定性 + A11y（2026-04-14）

Fix-B 之後的第三批（前端）。原前端審計 14 項中 8 項重審後為誤判
（`tabIndex` 已存在、`mountedRef` 已存在、grid `overflow-x-auto` 已吸
收、WSL2 header 已有 `minWidth: 56`、`testResult` 已 render、i18n 等
大範圍工作排往未來 Phase），剩下 4 項合併成 4 個 commit。

| Commit | 項 | 內容 |
|---|---|---|
| `4357bad` | C1 | `hooks/use-toast.ts` effect deps 從 `[state]` 改 `[]`；修 listener array unbounded growth；3 新測試（20-dispatch burst / 單次 unmount / 5 mount-unmount cycle） |
| `b234edb` | C3 | `forecast-panel` 截斷 span 加 `aria-label`；`agent-matrix-wall` sub-task 色點加 `role="img"` + `aria-label="Status: ..."`（色盲 / SR parity）|
| `a0fa35c` | C2 | `app/page.tsx` provider fetch 失敗從 `.catch(() => {})` 改 `console.debug(...)`；其他兩處已自備 error UI / mount guard |
| `27bfb1c` | C5 | `invoke-core.tsx` energy-beam glow `width: 40%` → `min(40%, 120px)` 防寬螢幕視覺溢出 |

### 驗收

`npx vitest run` → **55 passed / 11 files**（含新增 `test/hooks/use-toast.test.tsx`）。

### 重審降級項（未實作，已記錄）

- Layout shift：`global-status-header.tsx:161–172` 已用 `width: 110` + `minWidth: 56` + `tabular-nums` 固定。
- `integration-settings.tsx:62`：`tabIndex={0}` + `onKeyDown(Enter/Space)` 已存在。
- `decision-dashboard.tsx`：`mountedRef.current` + `AbortController` 已在 init-load 與 handler 兩側都做 guard。
- `page.tsx:513` desktop grid：`grid-cols-[...minmax()...1fr...]` + `overflow-x-auto` 已自動吸收。
- i18n 缺席、Final Report panel UI、三模式 Auth UI：scope 大，排往未來 Phase 67+。

### 後續

Fix-D（測試覆蓋補強）可立即啟動。Phase 62 Knowledge Generation 仍等 Fix-D。

---

## Phase 52-Fix-B — 穩定性修補（2026-04-14）

Fix-A 之後的第二批。重審時將 4 項原審計列項降為誤判（`list_pending`
copy、`get()` 已在鎖內、`_RULES_LOCK` await-outside、`asyncio.wait_for`
實際會 cancel），剩下的 6 項合併成 3 個 commit。

| Commit | 項 | 內容 |
|---|---|---|
| `9a61ec0` | B7 | 4 個 `threading.Lock` 宣告加 intent docstring；新增 `scripts/check_lock_await.py` 偵測 `with _lock:` 中的 `await`，含 self-test，base clean |
| `1d84502` | B1+B3 | 新增 `backend/routers/_pagination.py::Limit()`；9 個 list endpoint（decisions / logs / notifications / auto-decisions / audit / simulations / workflow / artifacts / task comments+handoffs）套用 `ge=1, le≤500`；13 bound test |
| `80435f9` | B2+B4+B5+B6 | 新增 `omnisight_persist_failure_total{module}` counter；notifications skipped/dead persist fail 補 log+metric；budget_strategy SSE、project_report manifest、release git describe、routers/system._sh、observability._watchdog_age_s 補 log.debug/warning |

### 驗收

```
pytest backend/tests/test_pagination_bounds.py test_silent_catch_logged.py \
       test_observability.py test_shell_safe.py test_decision_engine.py \
       test_decision_rules.py test_decision_api.py test_dispatch.py \
       test_external_webhooks.py test_audit.py test_tools.py
```
→ **144 passed, 1 skipped**（skip 為 prometheus_client 不在時的 env-gated
  case）。

`python3 scripts/check_lock_await.py` → clean ✓。

### 後續

Fix-C (UI/UX) + Fix-D (測試補強) 可並行啟動。Phase 62 Knowledge Generation
仍等待 Fix-D 完成以確保 workflow_runs 有足夠 coverage 再開。

---

## Phase 52-Fix-A — 緊急安全修補（2026-04-14）

源自 Fix-A 五項深度審計發現（S1/S2 auth bypass、S3' shell injection、
S6 orphan subprocess、S7 watchdog false positive）。各點獨立 commit
以便單獨 revert。

| Commit | 項 | 內容 |
|---|---|---|
| `9f18e2c` | S7 | `routers/invoke.py` watchdog tick 移至 stuck-detection 掃描完成後更新；hang 時 `/healthz` 可見 watchdog-age 增長 |
| `e51bbda` | S6 | `routers/webhooks.py` Jenkins/GitLab `proc.kill()` 失敗從 silent pass 改為 log + `omnisight_subprocess_orphan_total{target}` counter |
| `e0939cd` | S1+S2 | `/chat`/`/chat/stream`/`/chat/history` + `/system/settings` / `/system/vendor/sdks` mutators 加上 RBAC dependency；open 模式維持向後相容 |
| `c85c544` | S3' | `agents/tools.py` 5 處 `create_subprocess_shell` + f-string → `_shell_safe.run_exec` argv exec；新增 `backend/agents/_shell_safe.py` + 16 測試 |

### 驗收

`pytest backend/tests/test_observability.py test_shell_safe.py test_tools.py
test_git_platform.py test_external_webhooks.py test_integration_settings.py
test_decision_engine.py test_stuck_detector.py` → **139 passed**。

### 假陽性回補

審計報告原列 S4「Gerrit webhook 缺簽章」為誤判：`routers/webhooks.py:41–95`
已有 HMAC-SHA256 驗證（含 host-scoped secret fallback）。本次不動。

CLAUDE.md `checkpatch.pl --strict` / Valgrind CI gate 列入未來 Fix-E（文件合規），不屬 Fix-A 安全批。

### 後續

Fix-B / Fix-C / Fix-D / Fix-E 仍待排程。**Phase 62–65（Agentic
Self-Improvement）必須在 Fix-B + Fix-D 完成後才能啟動**，因為 Phase 64
toolmaking 會放大 shell-exec 攻擊面 — Fix-A 僅將 host 路徑補上，真正
sandbox 待 Phase 64 本身交付。

---

## Phase 52 — Production Observability（2026-04-14）

**Scope**：Prometheus `/metrics`、Deep `/healthz`、結構化 JSON log、Webhook DLQ
retry worker、Prom+Grafana sidecar 可選 profile。

### 交付

- `backend/metrics.py` — `CollectorRegistry` 與 10 組核心 metric（decision /
  pipeline / provider / sse / workflow / auth / uptime）。缺 prom 套件時自動
  退化為 no-op stub，呼叫端不需 guard。
- `backend/routers/observability.py` — `/metrics`（exposition）與 `/healthz`
  （db probe + watchdog age + sse + profile + auth_mode，1s timeout，503 on fail）。
- `backend/structlog_setup.py` — `configure()` / `bind_logger(**ctx)` /
  `get_logger(name)`；僅於 `OMNISIGHT_LOG_FORMAT=json` 時啟用 stdlib bridge。
- `backend/notifications.py::run_dlq_loop()` — 背景 worker 掃描
  `dispatch_status='failed'`，用盡 retry 後標記 `'dead'`；已併入 lifespan。
- `backend/routers/invoke.py` — watchdog 迴圈每次 tick 更新
  `_watchdog_last_tick`，供 `/healthz` 計算 watchdog age。
- `docker-compose.prod.yml` — 新增 `prometheus` + `grafana` service，置於
  `observability` compose profile（`docker compose --profile observability up`）。
- `configs/prometheus.yml` — backend scrape @15s，targets `backend:8000`。
- `backend/tests/test_observability.py` — 8 項測試涵蓋 `/metrics` 輸出、counter
  反映 decision propose、`/healthz` 200/503、structlog idempotent、DLQ
  exhausted→dead、DLQ re-dispatch。

### 依賴

`backend/requirements.txt` += `prometheus-client==0.21.1`、`structlog==24.4.0`。

### Commit

Phase 52 完成於 commit `TBD`（下一個 commit）。

---

## Phase 54 — RBAC + Sessions + GitHub App scaffold（2026-04-14）

第三波單一 phase。取代「optional bearer token」過渡方案，建立完整
session + role 授權層；同時導入 GitHub App scaffold（Open Agents 借鑑 #3）。

### 三模式設計

`OMNISIGHT_AUTH_MODE` env 控制：

| 模式 | 行為 | 適用 |
|---|---|---|
| **open**（預設）| 任何呼叫視為 anonymous-admin，bearer token 仍可用 | 單機 dev、向後相容 |
| **session** | mutator 需 session cookie；GET 仍開放 | 多人共用 dev / staging |
| **strict** | 所有請求需 cookie + CSRF | 上線環境 |

### 角色階層

`viewer < operator < admin`：

| 端點 | 最低角色 | 額外條件 |
|---|---|---|
| `GET *` | viewer | audit list 非 admin 自動 force `actor=user.email` |
| `POST /decisions/*/approve` | operator | destructive severity 額外要 admin |
| `POST /decisions/*/reject` `/undo` `/sweep` | operator | — |
| `PUT /budget-strategy` `/decision-rules` | operator | — |
| `PUT /operation-mode` | operator | `mode=turbo` 要 admin |
| `PUT /profile` | operator | `GHOST` / `AUTONOMOUS` 要 admin（GHOST 仍需雙 env gate）|
| `POST /decisions/bulk-undo` | operator | — |
| `GET /audit/verify` | admin | — |
| `GET/POST/PATCH /users` | admin | — |

### 元件

- Migration `0005_users_sessions_github_app.py`：3 表
  - `users`(id, email, name, role, password_hash, oidc_*, enabled, ...)
  - `sessions`(token, user_id, csrf_token, created/expires/last_seen,
    ip, ua) + 索引
  - `github_installations`(installation_id, account_login, repos_json,
    permissions_json, ...)
- `backend/auth.py`：
  - `User`/`Session` dataclass、`ROLES = (viewer, operator, admin)`
  - PBKDF2-SHA256（320k iters）密碼 hash（純 stdlib）
  - `create_user / authenticate_password / create_session / cleanup_expired_sessions`
  - `current_user(request)` FastAPI dependency 三模式分流；
    `require_role('operator')` / `require_admin` factory
  - `csrf_check` 雙提交 token 驗證
  - `ensure_default_admin()` 啟動時若 `users` 空則建一個（env
    `OMNISIGHT_ADMIN_EMAIL/PASSWORD`）
- `backend/routers/auth.py`：6 端點（login/logout/whoami + oidc stub
  + users CRUD）
- `backend/github_app.py`（Open Agents 借鑑 #3）：
  - 純 stdlib + cryptography 的 RS256 JWT 簽署
  - `app_jwt()` 6 min TTL；`get_installation_token()` 50 min cache
  - `upsert_installation` / `list_installations`
  - webhook handler 留待 v1
- 5 個既有 router 加 role gate：decisions × 5、profile × 2、audit × 2

### Tests（14 個新 test，全部一次過）

主檔 `test_auth.py`：role ladder、密碼 hash 防篡改、user CRUD、session
expire 清理、auth_mode 三模式、GitHub App JWT 環境檢查 + 用 ad-hoc
RSA-2048 簽出標準 RS256 JWT、installation upsert idempotent。

回歸：132 個 backend test 全綠（含 9 個 phase 加總）。

### 端到端驗證

- 啟動 log 出現 `[AUTH] default admin bootstrapped: admin@omnisight.local`
- `POST /auth/login` 成功設 `omnisight_session` (HttpOnly) + `omnisight_csrf`
- `POST /auth/logout` 清 session
- `GET /auth/whoami` 在 open mode 回 `role=admin email=anonymous@local`
- `PUT /operation-mode {mode:turbo}` 在 open mode 200；session/strict
  下 non-admin 會 403
- GitHub App `app_jwt()` 環境缺時 raise `GhostNotAllowed`-style；
  ad-hoc RSA 簽出的 JWT 通過 header / payload base64url 驗證

### v1 待補（不影響 MVP）

- OIDC（Google / GitHub）真實 redirect + callback
- Frontend User Management UI（admin only）
- session/strict 模式下 frontend 自動帶 cookie + CSRF header
- GitHub App webhook handler（installation_repositories / push）
- 記住「上次 mode 切換是 turbo」並提示 admin role 才能維持

---

## Phase 58 / 59 / 61 — 一次性實作（2026-04-14）

第二批一次性實作三個 phase，共 4 個 commit、~1900 LoC、22 個新後端 test。

### Phase 58 — Smart Defaults + Decision Profiles（commit `5c127fd`）
- Migration `0004_profiles_and_auto_log.py`：`decision_profiles` +
  `auto_decision_log` + `decision_rules.{negative, undo_count}`
- `backend/decision_profiles.py`：4 builtins（STRICT / BALANCED /
  AUTONOMOUS / GHOST），`CRITICAL_KINDS` 包含 git_push/main、deploy/prod、
  release/ship、workspace/delete、user/grant_admin
- GHOST 雙重 gate：`OMNISIGHT_ALLOW_GHOST_PROFILE=true` +
  `OMNISIGHT_ENV=staging`，否則 `set_profile()` 拋 `GhostNotAllowed`
- `backend/decision_defaults.py`：14 個 v0 chooser seed
- `decision_engine.propose()` 整合：rule 沒命中 → consult chooser →
  profile gate → 自動執行寫 `auto_decision_log` 並把 confidence /
  rationale / profile_id 放進 `dec.source`
- API：`GET/PUT /profile`、`GET /auto-decisions`、`POST /decisions/bulk-undo`
- 9 個 test 含 GHOST 雙 gate / 各 profile threshold / critical kind queue

### Phase 59 — Host-Native Target Support（commit `f656b40`）
- `configs/platforms/host_native.yaml`：toolchain=gcc，cross_prefix /
  qemu / sysroot 全空
- `backend/host_native.py`：`is_host_native()` /
  `should_use_app_only_pipeline()` / `app_only_phases()`（[concept,
  build, test, deploy] 4 階段）/ `host_device_passthrough()` /
  `context_dict()` 統一查詢點，60s 快取
- `decision_engine.propose()` 注入 `is_host_native` + `project_track`
  到 chooser Context
- 兩個 host-native chooser：
  - `deploy/dev_board` / `deploy/host`：host-native 0.92，cross-arch 0.65
  - `binary/execute`：host-native 0.95，cross-arch 0.70
- 8 個 test 含 chooser confidence ladder 對比 / yaml exists 健全性

### Phase 61 — Project Final Report Generator（commit pending）
- `backend/project_report.py`：6 段聚合 builder
  - Executive Summary（v0 templated；v1 交給 Reporter agent）
  - Compliance Matrix（manifest spec lines × tasks × tests）
  - Metrics Forecast vs Actual（從 token_usage 拉 actuals）
  - Decision Audit Timeline（最近 50 筆 audit_log）
  - Lessons Learned（episodic_memory top 20）
  - Artifact Catalog（最近 200 筆 artifacts）
- `render_html()` self-contained CSS（無外部依賴 → WeasyPrint 可直接消費）
- `render_pdf()` WeasyPrint；缺 system libs 時 fallback 為 .html 並設
  `X-Render-Fallback: html` header
- `requirements.txt` 加 `weasyprint>=63.0; sys_platform != 'win32'`
- API（`backend/routers/projects.py`）：
  - `POST /projects/{id}/report` 觸發生成
  - `GET /projects/{id}/report` JSON
  - `GET /projects/{id}/report.html` HTML
  - `GET /projects/{id}/report.pdf` PDF（fallback HTML）
  - 內存最後一次 build 結果於 `_LAST` dict
- 5 個 test 含 6 sections 完整性 / metrics 對應 / HTML self-contained /
  PDF fallback 不崩潰 / etag 16 hex chars

### 累計

| Phase | commit | LoC 增 | 新後端 test |
|---|---|---|---|
| 58 | `5c127fd` | +891 | 9 |
| 59 | `f656b40` | +294 | 8 |
| 61 | （本次）| ~640 | 5 |
| **合計** | | **~1825** | **22** |

實測：health 200、profile API 200（PUT BALANCED OK / PUT GHOST 403）、
host_native context 正確、`POST /projects/demo/report` 200、
`GET .html` + `.pdf` 皆 200（PDF 在缺 cairo/pango 環境會 fallback 為
HTML 並標 `X-Render-Fallback` header）。

跨檔測試確認：`test_decision_profiles` 加 finally 重置 module-level
singletons，避免 `_current` profile / `_current_mode` 洩漏到後續測試檔。

---

## Phase 51 / 56 / 53 / 60 — 一次性實作（2026-04-14）

四個 phase 依 SOP 子任務制連續實作，每 phase 完成後 targeted test +
uvicorn health + commit。共 4 個 commit、~1700 LoC、18 個新後端 test，
93 個受測項全綠。

### Phase 51 — Backend coverage + CI + Alembic（commit `4e23303`）
- `pytest-cov` + `pytest.ini [coverage:run/report]`
- `.github/workflows/ci.yml` — 5 job pipeline（lint / backend-tests
  sharded by domain / backend-migrate / frontend-unit / frontend-e2e）；
  shard 矩陣分 decision (85% min) / pipeline / schema / rest (60% min)
- Alembic：`alembic.ini` + `env.py`（env-aware、`render_as_batch=False`）
  + baseline migration `0001_baseline.py` 反向 dump 13 表（用
  `bind.exec_driver_sql()` 避開 `:` JSON DEFAULT 被當 bind param）；
  downgrade 拒絕；既有 `db._migrate()` 保留為 defence-in-depth
- v0：lint 與 tsc 暫設 warn-only；待 v1 收斂

### Phase 56 — Durable Workflow Checkpointing（commit `4bb4b21`）
- Migration `0002_workflow_runs.py` + db._SCHEMA mirror：
  `workflow_runs`（id/kind/status/last_step_id/metadata）+
  `workflow_steps`（UNIQUE(run_id, idempotency_key)）+ 索引
- `backend/workflow.py`：
  - `start()` / `get_run()` / `list_runs()` / `list_steps()`
  - `step(run, key)` decorator — cache-hit 返回快取、cache-miss 執行並寫入、
    UNIQUE collision 回讀
  - `finish()` / `replay()` / `list_in_flight_on_startup()`
- `backend/routers/workflow.py` — 4 端點（list / in-flight / replay / finish）
- `main.py` lifespan：startup 掃描 status='running' 的 workflow，logger.warning
  列出（前端可後續加 banner）
- 7 個 test 含 headline use case「resume after simulated crash」

### Phase 53 — Audit & Compliance（commit `9df9b73`）
- Migration `0003_audit_log.py` + db._SCHEMA mirror：`audit_log`
  with `prev_hash` / `curr_hash` + 索引（ts / actor / entity）
- `backend/audit.py`：
  - `log()`：sha256(prev_hash || canonical(payload) || ts) → curr_hash，
    asyncio.Lock 序列化避免 race
  - `log_sync()`：sync 呼叫端 fire-and-forget
  - `query()` 三維篩選；`verify_chain()` 走訪 + 報告第一個 broken row id
- DecisionEngine 三點掛載 audit：`set_mode` / `resolve` / `undo`，
  全部 try/except 包裝確保 audit 失敗不影響主流程
- `backend/routers/audit.py` — `GET /audit?...` + `GET /audit/verify`，
  受 `OMNISIGHT_DECISION_BEARER` 保護
- CLI：`python -m backend.audit verify | tail [N]`
- 5 個 test 含 chain_detects_tampering（forge row 3 → bad=3）

### Phase 60 v1 — History-Calibrated Forecast（commit pending）
- `backend/forecast.py · _load_history_sync()`：從 `token_usage`
  （avg tokens/request）+ `simulations`（avg duration_ms / count）萃取
- 信賴度 ladder：
  - `sample < 5` → `method=template`，confidence 0.50（v0 行為）
  - `sample 5..19` → `method=template+history`，50/50 blend，confidence 0.70
  - `sample ≥ 20` → `method=history`，全 history-driven，confidence 0.80
- `ProjectForecast.method` Literal 擴充
- 6 個 test：v0 baseline、track 輕重對比、5/20 sample blend、profile
  順序、provider 路由

### 累計

| Phase | commit | LoC 增 | 新後端 test |
|---|---|---|---|
| 51    | `4e23303` | +474 | (CI yml + shard config) |
| 56    | `4bb4b21` | +654 | 7 |
| 53    | `9df9b73` | +477 | 5 |
| 60 v1 | (本次)    | ~120 | 6 |
| **合計** | | **~1700** | **18** |

健康端點 200、forecast/audit/workflow API 全 200、alembic migrations 全
idempotent、93 個 backend test 綠（forecast + audit + workflow +
decision_engine + decision_rules + stuck_detector + schema）。

---

## Phase 50-Layout — Header / Panel 寬度穩定性掃修（2026-04-14）

操作員回報「某個元件狀態變動造成版面跑掉」是在多輪 commit 中陸續發現
的同類 bug。集中於 9 個 commit，徹底解決所有 dashboard 元件的寬度抖動。

### 根本原因

flex 列裡的可變寬度文字 / badge / 邊框 → 鄰居被推；無 `tabular-nums`
的數字會微抖；`border-2` 替換 `border` 會撐 box；loading placeholder 與
實際元件寬度不一致造成 mount 時跳動。

### 修法總綱

| 模式 | 套用對象 |
|---|---|
| 容器 `width: Npx` + `flexShrink: 0` | EmergencyStop / ArchIndicator / WSL2 / USB |
| 內 span `min-width` 預留最寬狀態空間 | EmergencyStop 文字槽、所有計數 |
| `tabular-nums` 確保數字等寬 | task counts / decision pending / progress |
| `truncate + maxWidth + title` 保完整字串 | hint text / advice 串 |
| `visibility: hidden` 預留隱藏槽位 | DETECTING 計數 (0 / N 切換) |
| `border-2` → `outline outline-2 outline-offset` | EmergencyStop CONFIRM 狀態 |
| `absolute` 定位脫離 flex flow | MODE error badge / popover / tour outline |

### 修復清單（commit 順序）

```
024804a fix(layout): 5 panel header sweep — task-backlog 計數、decision pending、
                                            budget hint、pipeline 3 metrics、
                                            decision-rules 計數、host CONNECTED/DETECTING
c0b254f fix(emergency-stop): 100×32 鎖 box + outline 取代 border-2 + 50px 文字槽
a3ef235 fix(header): WSL2 (110px) + USB (140px) 固定容器
628c655 fix(arch-indicator): 142/124 px 鎖 chip + truncate 7 字 + 後端 cap 16 字
2db910b fix(mode-selector): error chip absolute -top-1.5 -right-1.5 圓 badge + popover
```

### 影響面

- header 任何狀態組合（WSL OFFLINE / USB Detecting / MODE 500 / target
  toolchain missing / EmergencyStop 4 種狀態 / 100+ tasks）都不再造成
  鄰居元素位移。
- panel header 任何 counter / hint 變動也不再推 PanelHelp / tab / button。
- mount 時 placeholder 與實際元件同尺寸，無 layout shift。

### 設計沿用

未來新增 header / panel 元件須遵守 5 條規則：

1. 任何 flex row 的可變內容必有 `min-width` 或 `width` + `flex-shrink: 0`
2. 數字一律 `tabular-nums`
3. 任意字串 (provider / arch / hint / status) 須 `truncate` + `maxWidth` +
   完整內容於 `title` / `aria-label`
4. loading placeholder 須與真實元件同尺寸
5. 強調狀態變化用 `outline` / `box-shadow` / `transform`，**避免 `border-N`
   或 `padding` 改變 box 維度**

---

## Phase 50-Docs — 操作員文件 / 內建導覽（2026-04-14）

Phase 50-Fix 審計後補完的另一個大缺口：系統有 ~80 個 API 端點、12 個
panel、4 種 MODE × 4 種 Budget 策略，但使用者拿到介面後除了 tooltip
以外完全沒文件入口。以下全部原生內建、無外部依賴：

### D1/D2/D3 — 文件內容 × 4 語言

- **`docs/operator/{en,zh-TW,zh-CN,ja}/`** 6 份核心 reference：
  `operation-modes` / `decision-severity` / `panels-overview` /
  `budget-strategies` / `glossary` / `troubleshooting` — 每份分
  *TL;DR for PMs* + *matrix/table* + *under the hood* + *related
  reading* 三段，同檔頂部標 `source_en:` 以便翻譯漂移追蹤。
- **`app/docs/operator/[locale]/reference/[slug]/page.tsx`** +
  **`.../troubleshooting/page.tsx`** — Next.js App Router 頁面，讀取
  `.md` 並以 `lib/md-to-html.ts` 渲染（~170 行輕量 md 解析，支援
  headings / tables / lists / code / blockquote / inline links；link
  `.md` 後綴自動剝除轉 Next.js route）。

### E1 — `<PanelHelp>` `?` 圖示全面掛載

12 個 panel header 皆掛 `<PanelHelp doc="…">` 小元件：hover + 點擊
顯示 locale-aware TL;DR popover + 「完整文件 →」連結。 tolerant-locale
fallback（無 I18nProvider 時用 `en`），個別元件測試不受影響。

### E2 — 首次導覽（`?tour=1`）

**`components/omnisight/first-run-tour.tsx`** ~400 行，無 react-joyride
依賴：
- 新瀏覽器 localStorage 無 `omnisight-tour-seen` 時自動啟動，或任何 URL
  帶 `?tour=1` 手動觸發
- 5 步錨定到 `data-tour="mode|decision-queue|budget|orchestrator|panel-help"`
- SVG `evenodd` 路徑挖洞背景 + cyan pulse 框線 + 自動 viewport clamp
- 鍵盤 ← / → / Esc、4 語言 copy、`prefers-reduced-motion` 自動關動畫

### E3 — Help dropdown + docs 索引/搜尋

- **`HelpMenu`** 在 `GlobalStatusHeader` 桌機與手機版皆掛載：Reference /
  Tutorials / Troubleshooting / Run tour / Search / Swagger，每項 4 語
  標籤與 icon。
- **`/docs/operator/<locale>`** docs landing 頁：伺服器端讀取所有 .md
  抽 `{ title, headings, paragraphs }` → client 加權搜尋（title×5 /
  heading×3 / paragraph×1），顯示 100 字上下文 snippet。

### F1 — Tutorials × 4 語言

- **`docs/operator/<locale>/tutorial/first-invoke.md`**（10 分鐘 handon）
- **`docs/operator/<locale>/tutorial/handling-a-decision.md`**（8 分鐘
  含 undo / rule 設定）
- `/docs/operator/[locale]/tutorial/[slug]/page.tsx` 新 viewer route。
- HelpMenu 新分類「Tutorials」含兩筆。

### 產出一覽

| 類別 | 數量 |
|---|---|
| `.md` 文件（6 reference + troubleshooting + 2 tutorial × 4 langs） | 36 |
| Next.js routes 新增 | 4（reference viewer / troubleshooting viewer / tutorial viewer / docs landing）|
| 新元件 | 4（`PanelHelp` / `FirstRunTour` / `HelpMenu` / `DocsSearchClient`）|
| 共用 helper | 1（`lib/md-to-html.ts`）|

### 關鍵設計決策

- **英文為權威源**：每個譯文檔頭標 `source_en: <date>`，未來 CI 可比對。
- **無外部搜尋引擎**：六個 < 200 行的 .md，記憶體掃描 + 加權足夠。
- **無 markdown 函式庫**：避免 react-markdown / remark 的依賴重量；
  ~170 行自刻 renderer 涵蓋 90% 需求，其餘留給 D4+。
- **tolerant i18n hook**：`useLocale()` 在無 `I18nProvider` 時回傳 `en`，
  讓 PanelHelp / HelpMenu / FirstRunTour 可於單元測試獨立渲染。

### commits（時序）

```
09b6671 E3: Help dropdown + docs landing/search + md extract
6a7b934 E2: first-run 5-step walkthrough (?tour=1)
6b77088 E1: panel ? icons on every remaining panel
deebae8 fix: restore clickability on sci-fi MODE pills
864a941 feat: cockpit-grade MODE styling
c1037fc D3: budget-strategies + troubleshooting × 4 langs
897377a D2: in-app ? help popover + markdown viewer
2a40ff5 D1: 4 reference docs × 4 languages (20 files)
```
（F1 tutorials + HANDOFF 本段為本次 commit）

## Phase 51-61（未來排程）

為 Phase 50 完成後的下一批工作。每個 phase 維持既有節奏：實作 → 深度審計 → 補修 batch → commit。

> **2026-04-14 更新**：
> - 吸收 [vercel-labs/open-agents](https://github.com/vercel-labs/open-agents)
>   分析 → 新增 **Phase 56**（durable workflow）+ **Phase 57**（AI SDK +
>   voice），於 47-Fix 加 **Batch E**（docker pause hibernate）。
> - 全自動化目標的介入最小化驗證 → 新增 **Phase 58**（Smart Defaults +
>   Decision Profiles，含完整 UX 補強）。
> - x86_64 host-native 嵌入式場景（Hailo / Movidius / Industrial PC）
>   → 新增 **Phase 59**（Host-Native Target Support）。
>
> 詳見本段末三個分析小節。

### Phase 51 — Backend coverage + CI pipeline + schema migrations
讓 Python 測試與前端同級可觀測，同時把手刻 ALTER TABLE 升級成正式 migration 工具。
- `pytest-cov` 安裝 + `pyproject.toml` 設定（或 pytest.ini），coverage source 限制 `backend/`
- `.github/workflows/ci.yml`：跑 ruff / pytest（batched by folder）/ vitest / playwright（install deps: chromium-deps）
- Coverage threshold：`backend/decision_engine`, `stuck_detector`, `ambiguity`, `budget_strategy`, `pipeline` ≥ 85%；其餘 ≥ 60%
- 補齊 Phase 47 尚未被測的分支（`_handle_llm_error` 的 budget-strategy 接入路徑、`_apply_stuck_remediation` 每個 strategy 分支）
- **新增（Open Agents 借鑑）**：引入 **Alembic** migration tool（Open Agents 用
  `drizzle-kit`，Python 對應品為 Alembic）。
  - `backend/db.py` 的 `_migrate()` 手刻 ALTER TABLE 區塊改寫為 Alembic env，每個 migration 一個版本檔
  - 既有 12 表的 schema 反向產出第一個 baseline migration
  - Phase 50-Fix M1 的 PRAGMA-fail-fast 邏輯保留，作為 Alembic 之外的 invariant 防線
  - CI 加 `alembic upgrade head` 步驟確保新 schema 都過 dry-run
- 產出：`coverage.xml` + HTML report、CI artifact、`backend/alembic/versions/*.py`

### Phase 52 — Production observability
把系統從「能跑」升級到「能線上」。
- `/metrics` Prometheus endpoint（`prometheus_client`）：`decision_total{kind,severity,status}`、`pipeline_step_seconds`、`sse_subscribers`、`provider_failure_total`
- 結構化 JSON logging（`structlog`）：取代既有 `logger.info` 散落字串，每條含 `agent_id/task_id/decision_id/trace_id`
- `/healthz` 深度 health check：DB ping + backend version + watchdog heartbeat age
- `docker-compose.prod.yml` 掛 Prometheus + Grafana sidecar（可選 profile）
- OpenTelemetry trace hook 預留（不強制 span export）
- 產出：metrics 抓 scrape 可驗證、一個 Grafana dashboard 樣板

### Phase 53 — Audit & compliance layer
Decision 有記錄但目前無保留策略、無 actor 追蹤、無 tamper-evident。
- `audit_log` DB 表：`id, ts, actor, action, entity_kind, entity_id, before_json, after_json, prev_hash, curr_hash`
- Hash chain 每筆串接（Merkle-ish），防事後竄改
- DecisionEngine `resolve()` / `set_mode()` / `set_strategy()` 寫入 audit
- `GET /audit?since=&actor=&kind=&limit=`（有 `OMNISIGHT_DECISION_BEARER` 驗證）
- 保留策略 config：`OMNISIGHT_AUDIT_RETENTION_DAYS`（默認 365），超出由 nightly task 歸檔至 `audit_archive/{year-month}.jsonl.gz`
- GDPR 友善：`actor` 可為 hash（隱匿實姓）；`redact_fields` config
- 產出：audit chain 完整性驗證 CLI `python -m backend.audit verify`
- **設計沿用**：Phase 50-Fix Cluster 5（A1）建立的 `replace_decision_rules
  + load_from_db` 樣式可直接套用到 audit_log 的歸檔 CLI

### Phase 54 — RBAC + authenticated sessions + GitHub App
取代目前「optional bearer token」這個過渡方案，順帶把 GitHub PAT 升級為 App。
- Session-based auth（cookie + CSRF token），支援 OIDC（Google/GitHub/自建）
- User model：`id, email, role ∈ {viewer, operator, admin}`
- Per-endpoint role gate：mode=turbo 只 admin；approve destructive decision 只 operator+；/audit 全 role 可讀但 actor filter 強制自己
- Settings UI 加 User Management（admin only）
- Migration：若未啟用 OIDC，維持單用戶本地模式（default admin）以免破壞既有 dev 流程
- **新增（Open Agents 借鑑）**：**GitHub App 取代 PAT**
  - Open Agents 用 installation-based GitHub App（org 級授權 +
    per-installation token + 細權限：`Contents: write` /
    `Pull requests: write`）
  - 新增 `backend/github_app.py`：`PyGithub` + `pyjwt` 實作
    App JWT → installation token cache（5 min TTL）
  - DB 新表 `github_installations` (id, account_login,
    installation_id, repos[], created_at)
  - Settings UI 加「Install GitHub App」按鈕；callback 寫入 installation
  - `OMNISIGHT_GITHUB_TOKEN` PAT 路徑保留為 fallback（向後相容）
  - 補強 Phase 18 既有 GitHub 整合
- 產出：驗證矩陣（role × action → allow/deny）tests + 前端 role-aware UI
  （disabled vs hidden）+ GitHub App webhook handler

### Phase 55 — Agent plugin system
新增 agent type 目前要改 Python 核心；目標是配置化。
- `configs/agents/*.yaml` schema：`{id, type, sub_types[], tools_allowed[], system_prompt_template, default_model_tier, skill_files[]}`
- 啟動時掃描載入，暴露 `GET /agents/plugins`
- 動態 agent spawn（`POST /agents` 帶 plugin id）不再 hardcode `AgentType` enum
- Skill file 支援 Markdown frontmatter 聲明 `required_tools` / `mode_gate`
- 範例：加 `ai_safety_reviewer` plugin、`security_audit` plugin，不碰 core
- 產出：2 個示範 plugin YAML + loader tests + 前端 plugin picker UI

### Phase 56 — Durable Workflow Checkpointing（**新增 / Open Agents 借鑑 #1**）

當前 `pipeline.py` / `invoke.py` 是手刻 watchdog（30 min timeout）+
asyncio.Lock；後端 crash → in-flight invoke 全部丟。Open Agents 用 Vercel
Workflows SDK 提供 **durable multi-step execution**：每 step idempotent
checkpoint、stream reconnect 可從上一個 step 接續。我們用同模式但不綁
Vercel 平台。

- 新增 `backend/workflow.py` + DB 表：
  ```sql
  CREATE TABLE workflow_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- "invoke" | "pipeline_phase" | "decision_chain"
    started_at REAL NOT NULL,
    completed_at REAL,
    status TEXT NOT NULL,          -- "running" | "completed" | "failed" | "halted"
    last_step_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
  );
  CREATE TABLE workflow_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    idempotency_key TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    output_json TEXT,
    error TEXT,
    UNIQUE(run_id, idempotency_key)
  );
  ```
- LangGraph node 入口 / 出口透過 `@workflow_step` decorator 自動 checkpoint
- 新端點 `POST /invoke/resume?run_id=…` 從最後成功 step 之後續跑
- 配合 Phase 50-Fix #18 EventBus dead queue 的記憶體無界成長修復，把
  in-flight decision 也納入 workflow_runs 追蹤
- 前端 `app/page.tsx` 加「Resume in-flight runs」notification banner
- 估時：8-10 h；產出：跨重啟可續執行的 invoke、watchdog timeout race
  根本解、decision queue 隨 run 自動清理

### Phase 57 — AI SDK wire-protocol + 語音輸入（**新增 / Open Agents 借鑑 #2 + #5**）

Open Agents `apps/web` 用 Vercel AI SDK (`ai`, `@ai-sdk/react`) 統一 chat
streaming UX。我們前端 `package.json` 已裝了 8 個 `@ai-sdk/*` provider
client，但 backend FastAPI 是直接呼 SDK，wire 格式不符 SDK 的 UI message
stream protocol。

- **AI SDK wire protocol**
  - `backend/routers/chat.py` 的 `streamChat` 改為輸出 SDK v5 streaming
    format（`0:"text"` / `2:[{toolCallId,...}]` / `9:{...}` /
    `d:{finishReason,usage}`）
  - 前端 `lib/api.ts` 的 `streamChat()` / `streamInvoke()` 改用
    `useChat()` hook
  - 把 Phase 50-Fix Cluster 3 修的 `stream_truncated` 邏輯收斂成 SDK
    內建的 `onError` callback
  - `Orchestrator AI` panel 改為 `<Conversation>` 元件（@ai-sdk/react），
    streaming UX 與 Vercel AI Playground 一致
- **語音輸入（Open Agents #5 借鑑）**
  - 加 `lib/voice.ts` wrapper：`@ai-sdk/elevenlabs` Speech-to-Text
  - ⌘K palette 加「🎤 Voice command」入口（按住空白鍵 push-to-talk）
  - Mic 錄音 → 文字 → dispatch 為 slash command（與既有 Orchestrator 命令系統共軌）
  - `OMNISIGHT_ELEVENLABS_API_KEY` env 可選；未設則 mic 按鈕 disabled + tooltip
- 估時：8-10 h（AI SDK 整合）+ 4 h（語音）= 12-14 h；產出：與業界
  AI dashboard 一致的 chat UX、無痛切到任何 AI SDK 相容前端

### 既有 Phase 47-Fix 補充：Batch E（**Open Agents 借鑑 #4**）

stuck_detector 目前提案 4 種補救：`switch_model` / `spawn_alternate` /
`escalate` / `retry_same`。Open Agents 的 sandbox **snapshot-based hibernate**
很適合作為第 5 種 lightweight 策略：

- 新 strategy `hibernate_and_wait`：
  - `docker pause <container>` 凍結 agent 但保留 worktree state
  - DB 加 `agents.hibernated_at` 欄位
  - 操作員回來時 `docker unpause` resume；超過 24 h 自動 `docker rm`
- MODE = MANUAL 時預設 idle 即 hibernate（省 LLM token + container CPU）
- 估時 3 h；併入 47-Fix 既有 batch 序列

---

> **總體估時與順序見本段末「更新後總體估時」表**（含 Phase 58 / 59）

---

## Open Agents 借鑑分析（2026-04-14）

完整深度比較見對話歷史；以下為 **wontfix 決策**（不採用的部分）與
**rationale**，避免未來重複評估：

| 拒絕項 | Rationale |
|---|---|
| **Vercel Workflows SDK 直接套用** | 平台綁死 Vercel；OmniSight 是 self-host / WSL2 / 邊緣部署友善。借鑑「step checkpointing」模式但自實作為 Phase 56 |
| **Vercel Sandbox 取代 Docker** | 我們 sandbox 要做 aarch64 cross-compile + QEMU + Valgrind + RTK 壓縮，Vercel Sandbox 為一般 Linux VM 不支援 |
| **PostgreSQL 取代 SQLite** | 單機 dashboard 為主、12 表規模合理；Postgres 引入部署複雜度而無對應收益。Phase 53 audit chain 需要再評估 |
| **Drizzle ORM** | JS 生態，不適 Python 後端；對應品 Alembic 已於 Phase 51 排入 |
| **Open Agents 的 Skills / Subagents 模型** | 我們 8 agent type × 19 role skill + 4 個 Anthropic Skills（webapp-testing/pdf/xlsx/mcp-builder）已更成熟，反向借鑑無收益 |
| **Session 唯讀分享連結** | 我們的 `?panel=…&decision=…` 深鏈 + Phase 50-Docs 已涵蓋 80% 共享需求；做 read-only token 屬 Phase 54 RBAC 範疇 |

**已採納項**（如上 Phase 51 / 54 / 56 / 57 / 47-Fix Batch E 所列）：

1. Step-checkpointed durable workflow → **Phase 56**
2. GitHub App installation-based auth → **Phase 54** 擴充
3. docker pause hibernate as stuck strategy → **47-Fix Batch E**
4. Alembic migration tool（drizzle-kit 對應品）→ **Phase 51** 擴充
5. AI SDK v5 wire protocol + `useChat()` hook → **Phase 57**
6. ElevenLabs 語音輸入 → **Phase 57**

---

## 介入最小化驗證（2026-04-14）

針對「全自動化系統應讓操作員介入最小化」目標，以 9 個既有中斷場景對照
**今天 / +Phase 56（durable workflow）/ +Phase 58（smart defaults）** 三階段：

| # | 中斷場景 | 今天 | +Phase 56 | +Phase 58 | 殘留介入 |
|---|---|---|---|---|---|
| 1 | 後端 crash | 檢查 `[RECOVERY]` agents、pending decisions 全失 | resume from last step、idempotency 防重複 | smart defaults 在 resume 後仍套用 | **無** ✅ |
| 2 | 單 agent LLM error | 已自動（retry / failover / circuit breaker）| step idempotency 防重複 spend | confidence-gated provider switch | **無** ✅ |
| 3 | 卡住 agent | supervised 下要批 `switch_model`/`spawn_alternate` | （無變化）| BALANCED 自動解 risky-stuck，僅 escalate 找人 | **僅 escalate 情境** ⚠️ |
| 4 | Pipeline blocked | `force_advance` 手動推進 | （無變化）| 非關鍵 phase 加 `auto_force_advance_after`；關鍵 phase 保 HITL | **僅關鍵 phase** ⚠️ |
| 5 | Decision queue 中斷 | 重啟全失 | 持久化至 workflow_runs | smart defaults 自動消化 ~80% | **僅 critical kinds** ⚠️ |
| 6 | Container / workspace 故障 | 已自動清理 | （無變化）| （無變化）| **無** ✅ |
| 7 | LLM provider quota / webhook 失敗 | failover + 冷卻；無 DLQ | webhook idempotency 可重投 | profile 自動切 fallback | **全 provider 都掛**（外部依賴）❌ |
| 8 | Halt / Emergency Stop | 操作員觸發 | resume 智慧復原 idle agent | （無變化）| **觸發瞬間需人意志**（語意上必要）❌ |
| 9 | 前端斷線 | SSE replay 已自動 | （無變化）| （無變化）| **無** ✅ |

### 結論

**4 / 9 場景**（#1 / #2 / #6 / #9）介入完全消除  
**3 / 9 場景**（#3 / #4 / #5）縮減為僅 critical kinds  
**2 / 9 場景**（#7 / #8）結構性不可消除（外部依賴 / 操作員意志）

### 殘留 critical kinds 量化

依目前 17 種 decision kind 觀察：

- **必 HITL**：5 種（push/main、deploy/prod、release/ship、workspace/delete、grant_admin）≈ **30%**
- **BALANCED profile 自動化**：12 種 ≈ **70%**
- **AUTONOMOUS profile**：剩 3 種（push/main、deploy/prod、grant_admin）≈ **18%** 需介入

換算到日常使用：每日提案數從 **30+** 降到 **5 個內**（BALANCED）或 **2-3 個**（AUTONOMOUS）。

### 設計上保留的人類介面

「介入最小化」≠「介入歸零」。5 個 critical kinds + Emergency Stop 是**設計上**保留的人類意志介面，非技術 gap。把它們也自動化會讓系統具備「不請示就 ship 給客戶」的能力——通常被視為 bug 而非 feature。

### 達成介入最小化所需 phase 組合

**Phase 56 + Phase 58 + Phase 52 webhook DLQ 補強**（額外 3h）三件套即可達成。

---

## Phase 58 — Smart Defaults / Decision Profiles（**新增**）

讓系統真正全自動：把現有的 Decision Engine（severity × MODE × Rules）擴充
為**四層**：severity → **smart default chooser** → **profile 嚴格度** → 規則覆寫。
將「事前 approve」轉為「事後 review + bulk undo 安全網」。

### 4 個元件

#### 1. 智慧預設註冊表 `backend/decision_defaults.py`

```python
@dataclass
class SmartDefault:
    kind_pattern: str                              # fnmatch
    chooser: Callable[[Context], ChosenOption]     # 回傳 (option_id, confidence, rationale)
    confidence_min: float = 0.7                    # 低於此 → queue
    fallback_chain: list[str] = field(default_factory=list)
    auto_in_profiles: list[ProfileId] = field(default_factory=list)
```

每個 `kind` 對應一個 SmartDefault；首批種子 ~20 個常見 kind（branch
naming、commit style、model 選擇、test framework、retry strategy、provider
fallback order…）。

#### 2. Decision Profiles（4 預設，與 Budget Strategy 對稱）

| Profile | HITL 嚴格度 | 適用 | 預設？ |
|---|---|---|---|
| **STRICT**（≈ 現在 SUPERVISED 行為）| 所有 `risky+` 都問 | 上線前一週、新團隊接手 | |
| **BALANCED** | `risky` 若 confidence ≥ 0.7 自動；`destructive` 仍問 | 日常開發 | ✅ **新預設** |
| **AUTONOMOUS** | 連 `destructive` 都自動，僅 critical_kinds 白名單問；24 h 內可 bulk undo | 週末批次、demo 衝刺 | |
| **GHOST** | 連 critical 也只 5 s 倒數通知（強制 audit_log 完整 + staging-only 環境檢查）| dry-run / staging | **disabled by default** |

GHOST 啟用條件：`OMNISIGHT_ALLOW_GHOST_PROFILE=true` + 環境變數
`OMNISIGHT_ENV=staging` 雙重確認，否則 PUT `/profile` 拒絕切換。

#### 3. Confidence-gated auto-resolve

chooser 回傳 confidence score（來源 3 種）：
- LLM 自評（`temperature=0` + structured output 要求 `confidence: 0.0-1.0`）
- 歷史成功率（過去 N 筆同 kind 採 default option 的成功比）
- Episodic L3 memory（FTS5 找類似 decision 結果）

`confidence ≥ profile.threshold` → 自動 + 標 `source.auto_chosen=true`；
否則進佇列。

#### 4. Postmortem & Bulk Undo（**Q1 對應 UX 補強**）

BALANCED 為新預設代表使用者**少看到 30 個提案** → 為避免「失控感」加 6 項 UX 補強：

##### a. **Activity Digest Card**（dashboard 載入即顯示）
> 「過去 24 小時自動處理 47 件，44 件成功、3 件已撤銷。最近一筆：5 分鐘前
> auto-approved `branch/create` (confidence 0.92)」  
> 點開展開詳細 timeline。

##### b. **HISTORY tab 加 `auto-only` filter + bulk undo**
- 多選 checkbox + 「Undo selected」按鈕
- 每筆顯示 `confidence` bar + chooser rationale 縮圖

##### c. **Real-time Auto Activity 浮窗**（左下角，可關）
SSE `decision_auto_executed` 事件來時冒一個 1.5 s 半透明 chip：
> ✓ branch/create → agent/foo/refactor-x  (BALANCED · 0.92)

讓使用者**感受到系統正在工作**而非靜默吞動作。

##### d. **「Would have asked you under STRICT」標記**
HISTORY row hover 顯示：「此筆在 STRICT profile 下會進佇列」。讓使用者
知道 BALANCED 為他省了多少 click。

##### e. **Negative Rule 自動學習**
若操作員對同一 kind undo ≥ 2 次 → 自動建議：
> 「您撤銷了 `model_switch/refactor` 兩次。要為此 kind 加一條
> STRICT rule 嗎？」

接受 → 寫入 `decision_rules` 表 `negative=true` 欄位，往後此 kind 一律 queue。

##### f. **End-of-day Summary**（可選 opt-in）
每日 18:00 推一次（Slack / email / 站內通知）：
> 「今日 OmniSight 自動處理 152 件決策，含 0 件破壞性。最有信心的：
> branch naming（avg 0.94）。最低信心：provider fallback（0.71，3 件
> queue 等您）」

設定在 Settings → Notifications。

### DB schema

```sql
CREATE TABLE decision_profiles (
    id TEXT PRIMARY KEY,           -- "strict" | "balanced" | "autonomous" | "ghost"
    threshold_risky REAL NOT NULL,
    threshold_destructive REAL NOT NULL,
    auto_critical BOOLEAN NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE auto_decision_log (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    chosen_option TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT,
    profile_id TEXT NOT NULL,
    auto_executed_at REAL NOT NULL,
    undone_at REAL,                -- NULL = still standing
    undone_by TEXT
);

ALTER TABLE decision_rules ADD COLUMN negative BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE decision_rules ADD COLUMN undo_count INTEGER NOT NULL DEFAULT 0;
```

### 與既有元件整合

| 既有 | 改動 |
|---|---|
| `decision_engine.propose()` | `decision_rules.apply()` 之後加 `decision_defaults.consult()`；rule 沒命中再走 default |
| `OperationMode` | **語意僅保留「平行度預算」**（manual=1 / supervised=2 / full_auto=4 / turbo=8）；舊的 severity 自動執行矩陣**整體移到 Profile** |
| `BudgetStrategy` | 不變 |
| 新增 `DecisionProfile` | 與 BudgetStrategy 對稱：`/profile` GET/PUT、SSE `profile_changed` |

### 端點 + SSE

- `GET /profile` / `PUT /profile`（rate-limited、bearer-token 同 mode）
- `GET /auto-decisions?since=&undone=&limit=`（postmortem digest）
- `POST /decisions/bulk-undo` body `{ids: []}`
- SSE: `profile_changed`、`decision_auto_executed`（已存在，補 confidence 欄）

### 估時 & 順序

| 工項 | 估時 |
|---|---|
| `decision_defaults.py` 註冊表 + `consult()` 接入 propose | 4 h |
| 4 個 Profile + `/profile` API + SSE event + GHOST 雙重 gate | 4 h |
| Confidence chooser（LLM structured output + 歷史成功率）| 4 h |
| Postmortem card + bulk undo + auto-only filter | 4 h |
| Activity chip + Would-have-asked tooltip + Negative rule 學習 | 4 h |
| End-of-day summary（與 notifications.py 整合）| 2 h |
| Decision Defaults seed（前 20 個常見 kind）| 3 h |
| Tests + docs（operator/<lang>/reference/profiles.md ×4）| 5 h |
| **合計** | **~30 h** |

---

## Phase 59 — Host-Native Target Support（**新增**）

### 動機

OmniSight 目前主力 platform profile：`aarch64 / armv7 / riscv64`（embedded
SoC）。但實務上越來越多 AI camera 開發場景是**在 x86_64 host 上跑 x86_64
SoC / 評估板**：

- **Hailo-8 / Hailo-15** 評估板：x86_64 host + PCIe/USB 加速器
- **Intel Movidius / Myriad** dev kit：x86 NUC 為 host
- **AMD Versal / Xilinx Kria** 部分配置：x86 控制器
- **Industrial PC SoC**（COM Express、Mini-ITX）：x86 SoC 直接運行 AI workload
- **NVIDIA Jetson Orin x86 AGX 開發機**：交叉開發但部分階段 native 跑

當 `target_arch == host_arch` 時，**整個 cross-compile / QEMU 流程都是浪費**：

- ❌ 不需要 cross-compiler → 用 system gcc / clang
- ❌ 不需要 QEMU 模擬 → 直接執行 binary
- ❌ 不需要 sysroot → 用 host filesystem
- ❌ Docker container 可同時 build + run + test

### 影響的決策嚴重度

許多今天標 `risky` 的決策在 host-native 模式下實際是 `routine`：

| Kind | 今天 severity | host-native | 理由 |
|---|---|---|---|
| `deploy/dev_board` | risky | routine | container 隔離下「部署到 host」=「跑進 docker」 |
| `simulation/qemu_run` | risky | **N/A** | 完全不需要 |
| `cross_compile/toolchain_switch` | risky | **N/A** | 用 system toolchain |
| `binary/execute` | risky | routine | 同架構直接跑 |
| `firmware/flash` | destructive | risky | host-native 通常無 flash 步驟（軟體部署） |

### 新增 platform profile

`configs/platforms/host_native.yaml`（自動偵測 host arch 產生）：

```yaml
id: host_native
target_arch: x86_64                # auto-detect
host_arch: x86_64
cross_compile: false
toolchain:
  cc: gcc
  cxx: g++
  cmake_toolchain: null            # use system default
sysroot: null
qemu: false
docker_isolation: required         # 仍需 container 防誤刪 host
build_then_run_same_container: true
```

### 新增 `app_only` 模式

對於「**只開發應用層、不碰韌體 / driver**」的專案（多數 x86 邊緣 AI 應用都
是這類），加 project-level flag：

```yaml
# hardware_manifest.yaml
project_track: app_only            # firmware | driver | algo | app_only
target_platform: host_native
```

`app_only` 自動關閉的功能：
- 不啟動 firmware agent
- 不要求 BSP / HAL skill 載入
- 略過雙軌模擬中的 hw track（algo 直接跑）
- Pipeline 階段精簡：`Concept → Build → Test → Deploy`（從 8 階段縮為 4）

### Skill loader 偵測

`backend/prompt_loader.py` 載入 role 時若偵測 `host_native + app_only`：
- 載入 `roles/app-developer.yaml` 取代 `roles/bsp.yaml`、`roles/isp.yaml`
- 把 system prompt 中 cross-compile / QEMU 段落抑制
- 工具預設 set 縮減（不需要 `qemu_emulate`、`flash_firmware` 等）

### Pipeline / 雙軌模擬簡化

`backend/pipeline.py` 對 host_native + app_only：
- 只跑 `algo_track`（資料驅動回放）
- `hw_track` 改為直接 `pytest -m hardware`（host native 即 target）
- 無需 `simulate.sh` 切 platform profile
- Build & test 同一 container → 時間從 ~5 min 縮至 ~1 min

### Decision Engine 整合

新增 `Context.is_host_native: bool`，傳入 `decision_engine.propose()`：
- chooser 可基於此 flag 調整 severity（用 `decision_defaults.py` 內的 modifier）
- `decision_rules.yaml` 可宣告 `host_native_only_routine: true` 表示 host-native 下降級

範例 rule：
```yaml
- kind_pattern: "deploy/*"
  severity: routine            # host-native 下
  conditions:
    is_host_native: true
    project_track: app_only
  auto_in_modes: [supervised, full_auto, turbo]
```

### 對 BALANCED profile 的相乘效應

host_native + app_only + BALANCED profile 組合：

- 介入頻率從 **每日 5 個** 降到 **每日 1-2 個**
- 完整 build-test-deploy cycle 從 **15 min** 縮到 **2-3 min**
- LLM token 消耗減少 **~40%**（少了 cross-compile 錯誤分析）

### Sandbox 安全考量

雖然 arch 一致，**仍需 docker 容器隔離**（防呆）：

- container 內仍受 `--memory` / `--cpus` / `--pids-limit` 限制（既有 Phase 16 機制）
- workspace mount 仍 `:ro` 防主機檔案污染
- 但允許 USB / PCIe device passthrough（Hailo 等加速卡需要）：新 env
  `OMNISIGHT_HOST_DEVICE_PASSTHROUGH=hailo|movidius|none`

### 估時 & 工項

| 工項 | 估時 |
|---|---|
| `host_native.yaml` profile + 自動偵測 host arch | 2 h |
| `project_track: app_only` schema + manifest 解析 | 2 h |
| `prompt_loader` 偵測 + skill 抑制邏輯 | 3 h |
| `pipeline.py` 簡化（4 階段精簡 + 單 container build-run）| 4 h |
| 雙軌模擬：跳過 hw_track 改 pytest 直跑 | 3 h |
| Decision Engine `Context.is_host_native` + severity modifier | 2 h |
| Sandbox device passthrough（PCIe/USB）| 4 h |
| 範例 manifest（Hailo-8 + x86 host）+ tests | 3 h |
| Operator docs：新增 host-native getting-started × 4 langs | 5 h |
| **合計** | **~28 h** |

### 排序建議

放在 Phase 58 之後、Phase 55 之前：

1. **Phase 58**（Smart Defaults）已建立 confidence-based auto-resolution
2. **Phase 59** 給 host-native 場景注入 `is_host_native` context flag
3. 兩者相乘 → 介入頻率最低化的最大化收益

---

## Phase 60 — Project Forecast Panel（**新增**）

當 `hardware_manifest.yaml` 設定完成時，使用者應該能立即看到本專案的
預期：任務數 / agent 數 / cycle time / token 消耗 / 預計費用 / 信賴度。
用以建立心理預期、與管理層對齊預算、選擇合適的 MODE × Profile 組合。

### 資料來源（皆已存在）

| 來源 | 提供 |
|---|---|
| `hardware_manifest.yaml` | 專案範圍：sensor、target_platform、project_track、商業模式 |
| `configs/platforms/*.yaml` | toolchain、cross-compile 與否（影響工時） |
| `configs/roles/*.yaml` | 19 role × 各自典型工序 |
| `pipeline.py · PIPELINE_STEPS` | NPI 8 phase × 已知步驟序列 |
| `token_usage` 表 | 歷史每 task token 消耗（per agent / model） |
| `simulations` 表 | 歷史 task duration |
| `episodic_memory` (FTS5) | 過往類似專案的可搜事件 |
| 新增 `configs/provider_pricing.yaml` | provider × tier 單價（USD per 1M tokens） |

### 後端 API

新增 `backend/forecast.py`：

```python
@dataclass(frozen=True)
class ProjectForecast:
    tasks:    TaskBreakdown        # total + by_phase + by_track
    agents:   AgentBreakdown       # total + by_type
    duration: DurationBreakdown    # optimistic / typical / pessimistic
    tokens:   TokenBreakdown       # total + by_model_tier
    cost_usd: CostBreakdown        # total + by_provider
    confidence: float              # 0.0..1.0 based on history sample size
    method: Literal["fresh","template","template+regression"]
    profile_sensitivity: dict      # STRICT/BALANCED/AUTONOMOUS 對照
```

端點：
- `GET /api/v1/forecast` — 即時計算 + 5min cache
- `POST /api/v1/forecast/snapshot` — 凍結當前預估存入 `forecast_snapshots` 表
- SSE event `forecast_recomputed`（manifest 變更時）

### Forecasting model 演進

| 階段 | 模型 | 信賴度 |
|---|---|---|
| **v0**（本 phase 內首版）| 純 template — `track × phase × role` 查表得任務數，`avg_minutes_per_task` 預設值乘上 | ~0.5 |
| **v1** | template + 歷史校準（同 sensor / 同 track 過去 N 筆 token_usage 中位數） | ~0.7 |
| **v2** | 簡單線性回歸（features: project_track, target_arch, sensor_resolution, role_count）→ tokens / hours | ~0.8 |

### 前端

新增 `components/omnisight/forecast-panel.tsx`：
- 6 KPI 卡：TASKS / AGENTS / HOURS / TOKENS / USD / CONFIDENCE
- 折疊區：Phase breakdown + Profile sensitivity 對照表
- Recompute 按鈕（手動觸發）
- 位置：Spec panel 旁，或 Project tab 第一頁

### 估時

| 工項 | 時 |
|---|---|
| `forecast.py` template + 6 種 breakdown dataclass | 4 |
| `provider_pricing.yaml` + 載入器 | 1 |
| 端點 + cache + SSE | 2 |
| forecast_snapshots 表 + history（為 Phase 61 鋪路）| 2 |
| `<ForecastPanel>` 6 KPI + breakdown chart | 3 |
| Tests + docs（operator/<lang>/reference/forecast.md ×4）| 2 |
| **合計** | **~14 h** |

---

## Phase 61 — Project Final Report Generator（**新增**）

當專案完成（NPI 進入 Mass Production）時，自動產出一份**完整報告**。
給 PM / 客戶 / 稽核三類受眾。沿用 Phase 50-Docs 的 markdown → HTML
渲染管線，加上 PDF 輸出。

### 報告內容

1. **Executive Summary**（Reporter agent 用 templated prompt 生成，PM 視角）
2. **Compliance Matrix** — `hardware_manifest.yaml` 每行 spec → 哪些 task 實作 / 哪些 test 通過
3. **Metrics: forecast vs actual**（依賴 Phase 60 開頭 snapshot + 結尾實測）
4. **Decision Audit Timeline**（依賴 Phase 53 audit_log）
5. **Lessons Learned** — 從 `episodic_memory` FTS5 萃取「踩過哪些坑、解法為何」
6. **Artifact Catalog** — 自動 BOM + checksum + 下載清單

### PDF 渲染策略（依您的指示）

| 報告類型 | 渲染器 | 理由 |
|---|---|---|
| **純文字 / 表格報告**（compliance matrix、artifact catalog、lessons）| **WeasyPrint** | 純 Python，CSS print 支援好，無 chromium 依賴；中日文字型靠 fontconfig + Noto |
| **含圖表報告**（forecast vs actual chart、decision timeline graph）| **Playwright** + Next.js print page | 可重用 dashboard FUI 圖表元件（recharts），輸出風格一致 |

實作：
- `backend/project_report.py · render_pdf(report, kind="text"|"chart")` 路由到對應 renderer
- 文字版用 WeasyPrint 直接渲染 `.html`（已用 `lib/md-to-html.ts` 同邏輯的 Python 版）
- 圖表版用 Playwright 開 `http://localhost:3000/projects/<id>/report/print` print-only Next.js page

### 端點

```
POST /api/v1/projects/{id}/report      # 觸發生成（async workflow，依賴 Phase 56）
GET  /api/v1/projects/{id}/report      # 取得最近一次 report JSON
GET  /api/v1/projects/{id}/report.pdf  # 下載 PDF
GET  /api/v1/projects/{id}/report.html # 下載 HTML
```

### 估時

| 工項 | 時 |
|---|---|
| `project_report.py` 6 段聚合邏輯 | 5 |
| Compliance matrix 萃取（spec line × task × test）| 3 |
| WeasyPrint 文字版 + Noto 中日文字型 | 2 |
| Playwright print page（Next.js route + chart 版面）| 4 |
| Forecast vs actual diff 計算（依 Phase 60 snapshot）| 1 |
| Lessons learned FTS5 萃取（依 Phase 53 audit_log + episodic）| 2 |
| 新前端 panel：Final Report tab 於 Vitals & Artifacts panel 內 | 1 |
| **合計** | **~18 h** |

### 依賴關係

- **Phase 53** audit chain：`Decision Audit Timeline` 段需要 audit_log
- **Phase 60** forecast snapshot：`Forecast vs Actual` 段需要開頭快照
- **Phase 56** durable workflow：報告生成本身是長 task，需 step checkpoint

故 61 必須排在 53 + 60 + 56 之後。

---

### 更新後總體估時

| Phase | 主題 | 估時 |
|---|---|---|
| 51 | Backend coverage + CI + Alembic | 5-7 h |
| 52 | Production observability（含 webhook DLQ）| 9-11 h |
| 53 | Audit & compliance | 5-7 h |
| 54 | RBAC + sessions + GitHub App | 14-18 h |
| 55 | Agent plugin system | 6-10 h |
| 56 | Durable workflow checkpointing | 8-10 h |
| 57 | AI SDK wire-protocol + voice | 12-14 h |
| **58** | **Smart Defaults / Decision Profiles** | **30 h** |
| **59** | **Host-Native Target Support** | **28 h** |
| 47-Fix Batch E | docker pause hibernate | 3 h |
| **60** | **Project Forecast Panel** | **14 h** |
| **61** | **Project Final Report Generator** | **18 h** |
| **合計** | | **~152-170 h** |

### 更新後執行順序建議

**51 → 56 → 53 → 60 → 58 → 59 → 61 → 54 → 52 → 57 → 55**

關鍵理由：
1. CI/coverage 先（51）
2. workflow checkpoint 是後續所有 phase 的可靠性前置（56）
3. audit chain 在加 auto decision 之前（53），確保自動化決策皆有跡可循
4. **Forecast（60）排在 audit 之後 + Smart Defaults 之前**——audit log 是
   actual 資料權威來源，且 Profile 切換時可即時看到 forecast 對照
5. **Smart Defaults（58）+ Host-Native（59）相鄰執行**——兩者相乘效益最大
6. **Final Report（61）依賴 53 + 60 + 56 全部完成**才能聚合
7. RBAC（54）建立在已有完整審計與 profile 之上
8. observability（52）→ UX polish（57）→ plugin system（55）收尾

---

## 0. 專案理解與未來開發藍圖

### 專案本質

OmniSight Productizer 是一套專為「嵌入式 AI 攝影機（UVC/RTSP）」設計的全自動化開發指揮中心。
系統以 `hardware_manifest.yaml` 和 `client_spec.json` 為唯一真實來源（SSOT），
透過多代理人（Multi-Agent）架構，實現從硬體規格解析、Linux 驅動編譯、演算法植入到上位機 UI 生成的全端自動化閉環。

### 目前系統能力

- **前端**：Next.js 16.2 科幻風 FUI 儀表板，16 組件，全部接真實後端資料，零假資料，Error Boundary + fetch timeout/retry
- **後端**：FastAPI + LangGraph 多代理人管線，14 routers ~70 routes，28 sandboxed tools
- **LLM**：9 個 AI provider（含 OpenRouter 聚合 200+ 模型）可熱切換 + failover chain（含 5min circuit breaker cooldown）+ token budget 三級管理（80% warn → 90% downgrade → 100% freeze → 每日自動重置）+ per-agent model routing（`provider:model` 格式）+ model 驗證機制（建立/分派時檢查 API key）
- **Settings UI**：LLM Provider 聯動下拉選單 + API Key 狀態指示（✅/⚫）+ 雙入口即時同步（Settings ↔ Orchestrator 透過 SSE）
- **Agent 角色**：8 種 agent type，19 個角色 skill file，7 個模型規則
- **隔離工作區**：git worktree（Layer 1）+ Docker 容器（Layer 2，含 aarch64 交叉編譯 + RTK 壓縮 + Valgrind + QEMU + 記憶體/CPU/PID 限制）
- **雙軌模擬**：simulate.sh（algo 資料驅動回放 + hw mock/QEMU 驗證）+ 3 個 platform profiles（aarch64/armv7/riscv64）+ 輸入驗證 + :ro 防呆
- **即時通訊**：EventBus → SSE 持久連線 + REPORTER VORTEX log + SSE 自動重連（exponential backoff，失敗 5 次降級 polling）
- **INVOKE 全局指揮**：上下文感知 → 智慧匹配（sub_type + ai_model 評分）→ task 自動拆解 → 非同步 pipeline → 回報
- **Gerrit 整合**：AI Reviewer agent + webhook + `refs/for/main` push + 最高 +1/-1
- **工單系統**：state machine（7 狀態）+ fact-based gating + task comments + 外部同步（GitHub/GitLab/Jira）
- **通知系統**：4 級路由（L1-L4）+ 前端通知中心
- **RTK 壓縮**：100% tool 輸出覆蓋（28/28 tools），retry bypass
- **NPI 生命週期**：8 phase × 3 track × 4 商業模式（ODM/OEM/JDM/OBM）+ 科幻 vertical timeline
- **錯誤回復**：4 層防禦（預防 → 偵測 → 回復 → 降級）+ watchdog（30min timeout）+ startup cleanup + asyncio.Lock 防競爭
- **持久化**：SQLite WAL 模式（12 tables: agents, tasks, simulations, artifacts, notifications, token_usage, handoffs, task_comments, npi_state, event_log, debug_findings, episodic_memory）+ FTS5 全文搜索 + integrity check + busy_timeout
- **Task Skills**：4 個 Anthropic 格式任務技能（webapp-testing, pdf-generation, xlsx-generation, mcp-builder）+ 自動 keyword 匹配載入
- **對話系統**：Orchestrator 面板支援純對話（問答、建議、狀態查詢），自動意圖偵測（LLM + rule-based），系統狀態注入，無工具對話節點
- **Debug Blackboard**：跨 Agent 除錯黑板（debug_findings DB + 語義迴圈斷路器 + /system/debug API + SSE 事件 + 對話注入）
- **消息總線**：EventBus bounded queue (1000) + 事件持久化（白名單 6 類事件 → event_log 表）+ 事件重播 API + 通知 DLQ 重試 (3x exponential backoff) + dispatch 狀態追蹤
- **生成-驗證閉環**：Simulation [FAIL] → 自動修改代碼 → 重新驗證迴圈（max 2 iterations）+ Gerrit -1 自動建立 fix task
- **調度強化**：Pre-fetch 檢索子智能體（codebase 關鍵字搜索注入 handoff）+ 任務依賴圖（depends_on）+ 動態重分配（watchdog blocked→backlog）
- **Agent 團隊協作**：CODEOWNERS 檔案權限（soft/hard enforcement）+ pre-merge conflict 偵測 + write_file 權限檢查
- **Provider Fallback UI**：Orchestrator 面板 FAILOVER CHAIN 區塊（health 狀態 + cooldown 倒數 + 上下箭頭排序）+ GET /providers/health + PUT /providers/fallback-chain
- **雙向 Webhook 同步**：External → Internal（GitHub HMAC/GitLab Token/Jira Bearer 驗證 + 5s debounce）+ CI/CD trigger（GitHub Actions/Jenkins/GitLab CI）
- **Handoff 視覺化**：Orchestrator 面板 HANDOFF CHAIN 區塊（agent-to-agent 接力時間線 + 色彩對應）
- **NPI 甘特圖**：Timeline/Gantt 雙模式切換（垂直時間線 + 橫向進度條圖）
- **SoC SDK 整合**：Platform vendor 擴展 + Container SDK :ro mount + simulate.sh cmake toolchain + get_platform_config tool + Vendor SDK API + BSP 參數化
- **系統整合設定**：Settings 面板（Git/Gerrit/Jira/Slack 配置 + Test Connection 6 種 + Vendor SDK CRUD + Token masking + Hot Reload）
- **快速指令**：/ 前綴指令系統（22 指令 × 6 分類）+ Autocomplete dropdown（InvokeCore + Orchestrator 雙入口）+ 後端 chat.py 攔截
- **分層記憶**：L1 核心規則（CLAUDE.md immutable → 所有 prompt 首段注入）+ L2 工作記憶（summarize_state tool + context_compression_gate 自動壓縮 90% 上限）+ L3 經驗記憶（episodic_memory FTS5 DB + search_past_solutions + save_solution + Gerrit merge 自動寫入 + error_check 自動查詢）
- **安全強化**：Gerrit webhook HMAC 驗證 + vendor SDK path traversal 防護 + workspace path `relative_to()` 防護 + FTS5 sync 日誌 + rebuild 機制
- **Schema 正式化**：12 個 Pydantic response models + 7 個端點 response_model 掛載 + 13 個 SSE event payload schemas + GET /sse-schema export + DB upsert 修復 3 欄位 + 前端 TypeScript 同步 10 個欄位 + SimulationStatus enum 對齊
- **NPU 部署**：NPU simulation track（algo/hw/npu 三軌）+ simulate.sh run_npu() CPU fallback 推論 + get_platform_config NPU 欄位 + 4 個 AI Skill Kits（detection/recognition/pose/barcode）+ 前端 NPU 面板（track selector + model/framework 表單 + latency/accuracy 顯示）
- **智慧路由**：select_model_for_task()（agent type 偏好 + 任務複雜度 + 成本感知 + budget 預算），LLM 輔助任務拆分 + 子任務自動依賴鏈，取代 regex 切分
- **硬體整合**：deploy_to_evk + check_evk_connection + list_uvc_devices 工具，simulate.sh deploy track（mock 模式），GET /system/evk + POST /system/deploy API，V4L2 裝置偵測，/deploy + /evk + /stream 快速指令（25 個），前端 EVK/UVC 面板增強
- **產物管線**：finalize() 自動收集 build outputs → .artifacts/ + SHA-256 checksum + register_build_artifact 工具（所有 agent 可用）+ 前端下載按鈕接線 + Gerrit merge 自動打包 tar.gz + ArtifactType 11 種（含 binary/firmware/model/sdk）
- **Release 打包**：resolve_version()（git tags/VERSION/package.json）+ release manifest JSON + tar.gz bundle + GitHub/GitLab release upload + CI/CD workflows（ci.yml + release.yml）+ /release 指令（26 個）
- **錯誤韌性**：LLM Error Classifier（11 類 × 9 provider）+ exponential backoff（429/503/529 自動等待 + Retry-After 解析）+ invoke-time failover + 401/402 永久標記 + context overflow→L2 壓縮 + 前端 retry 429/503 + SSE 錯誤通知 + 統一 max_retries=3
- **Multi-Repo**：git_credentials.yaml registry + per-host token/SSH key 解析 + webhook multi-instance secret routing + Settings UI credential list + /repos platform/authStatus + 向後相容 scalar fallback
- **權限自動修復**：Permission Error Classifier（9 類）+ auto-fix（chmod/cleanup/lock/port）+ 預防性環境檢查（disk/docker/git/ssh）+ error_check_node 智慧處理（auto-fix 不計 retry）+ SSE 通知
- **SDK 自動偵測**：sdk_git_url 欄位 + SDK provisioner（clone + scan sysroot/cmake/toolchain）+ validate_sdk_paths + POST install API + 路徑缺失警告（tools/container/simulate.sh）
- **容器化**：Dockerfile.backend（Python 3.12-slim + uvicorn）+ Dockerfile.frontend（Node 20 multi-stage standalone）+ docker-compose.yml（dev hot-reload）+ docker-compose.prod.yml（named volumes + healthcheck + restart:always）+ 生產配置參數化（debug/CORS/DB/proxy 全部 env var 化）
- **測試**：678 tests（45 個 test 檔案）
- **E2E Pipeline**：7 步自動串聯（SPEC→開發→審查→測試→部署→打包→文件）+ 人類 checkpoint（Gerrit +2 / HVT）+ force advance + /pipeline 指令 + 3 個 API 端點

### 未來開發藍圖

| Phase | 內容 | 模式覆蓋 | 狀態 |
|-------|------|---------|------|
| 18 | Anthropic Skills 選擇性導入（webapp-testing, pdf, xlsx, mcp-builder）| — | ✅ 核心完成 |
| 19 | 智慧對話系統（意圖偵測 + conversation_node + 系統狀態注入）| — | ✅ |
| 20 | 共享狀態強化 — Debug Blackboard + 語義迴圈斷路器 + 跨 Agent 狀態 API | 模式5 | ✅ |
| 21 | 消息總線強化 — Dead-letter Queue + 事件持久化 + 事件重播 API | 模式4 | ✅ |
| 22 | 生成-驗證閉環 — Gerrit -1 自動重派 + Simulation fail → 代碼修正迴圈 | 模式1 | ✅ |
| 23 | 調度強化 — 檢索子智能體（預取 codebase 上下文）+ 任務依賴圖 + 動態重分配 | 模式2 | ✅ |
| 24 | Agent 團隊協作 — CODEOWNERS 檔案權限 + Merge Conflict 預防 | 模式3 | ✅ |
| 25 | Provider Fallback Chain 前端 UI（排序 + 健康狀態 + cooldown 倒數）| — | ✅ |
| 26 | External → Internal Webhook 雙向同步 + CI/CD 管線觸發 | 模式4 | ✅ |
| 27 | Agent Handoff 視覺化 + NPI 甘特圖 | — | ✅ |
| 28 | SoC SDK/EVK 整合開發自動化（三軌並行：Infra + Software + Hardware）| — | ✅ |
| 29 | 快速指令系統（/ 前綴 + autocomplete + 22 開發指令 + 前端攔截 + 後端路由）| — | ✅ |
| 30 | 硬體整合（deploy tools + simulate.sh deploy track + EVK API + V4L2 偵測 + /deploy /evk /stream 指令 + 前端 EVK/UVC 面板）| — | ✅ |
| 31 | Schema 正式化（12 response models + 13 SSE schemas + DB upsert 修復 + 前端 type 同步 + enum 對齊）| — | ✅ |
| 32 | 分層記憶架構（L1 核心規則 + L2 context 壓縮 + L3 FTS5 經驗記憶 + search/save tools + Gerrit 自動寫入）| 模式1,5 | ✅ |
| 33 | 前端直連 LLM 快速對話（Vercel AI SDK useChat 整合 + /api/chat 串接 + 雙路對話模式）| — | 待實作 |
| 34 | 系統整合設定 UI（Settings 面板 + Test Connection + Vendor SDK CRUD + Hot Reload）| — | ✅ |
| 35 | 多國語言完整覆蓋（i18n 全組件翻譯 + 動態切換 + slash command 翻譯 + agent 回應語言偏好）| — | 待實作 |
| 36 | Edge AI NPU 部署自動化（Inference HAL + npu simulation track + 4 AI Skill Kits + 前端 NPU 面板）| — | ✅ |
| 37 | OpenRouter 整合（第 9 個 provider + 16 模型含 10 獨有 + failover chain 倒數第二位）+ per-agent model routing + Settings UX 改進 + model 驗證機制 | — | ✅ |
| 38 | 智慧模型路由（複雜度評估 + type→model 偏好 + 成本感知 + LLM 任務拆分 + 子任務自動依賴鏈）| 模式2 | ✅ |
| 39 | 產物管線（finalize 保存 build outputs → .artifacts/ + register_build_artifact tool + ArtifactType 11 種 + 前端下載 + Gerrit merge tar.gz）| — | ✅ |
| 40 | Release 打包（version resolver + manifest JSON + tar.gz bundle + GitHub/GitLab upload + CI/CD workflows + /release 指令）| — | ✅ |
| 41 | 系統容器化（Dockerfile backend/frontend + docker-compose dev/prod + standalone output + 生產配置參數化 debug/CORS/DB/proxy + healthcheck）| — | ✅ |
| 42 | 統一錯誤處理與韌性強化（11 類 Error Classifier + backoff + failover + 401/402 永久標記 + context→L2 壓縮 + 前端 retry 429 + SSE 通知 + 統一 max_retries）| — | ✅ |
| 43 | Multi-Repo Credential Registry（git_credentials.yaml + per-host token/SSH key + webhook multi-instance routing + Settings UI credential list + /repos platform/authStatus）| — | ✅ |
| 44 | Permission & Environment Auto-Fix（9 類分類器 + auto-fix chmod/cleanup/lock/port + 不可修復→SSE 通知 + 預防性環境檢查 + error_check 智慧處理）| — | ✅ |
| 45 | SDK Auto-Discovery（sdk_git_url + provisioner clone/scan + validate paths + install API + 路徑缺失警告 tools/container/simulate.sh）| — | ✅ |
| 46 | E2E Orchestration Pipeline（一鍵 SPEC→規劃→開發→審查→測試→部署→打包→文件 全流程串聯 + NPI phase 自動推進 + 人類 checkpoint 自動等待通知 + /pipeline 指令 + 19 tests）| — | ✅ |
| 47 | Autonomous Decision Engine（4 模式 Manual/Supervised/FullAuto/Turbo + Decision Dashboard + deadline 感知 + budget 預測 + 並行 Agent + stuck 策略切換 + auto-decision rules UI + 通知 toast approve/reject）| — | 待實作 |

### 開發注意事項

| 項目 | 說明 |
|------|------|
| **測試執行策略** | 全套 437+ tests 跑一次需 60-180 分鐘，開發迭代時**禁止跑全套**。改用分批策略：每個子階段只跑受影響的 test files（`pytest backend/tests/test_xxx.py`），Phase 完成時跑較大批次驗證，全套僅在 major milestone 或明確要求時執行。快速冒煙測試用 `timeout 4 python3 -m uvicorn backend.main:app --port XXXX`。 |
| **DB 狀態洩漏** | 部分測試間有 DB 狀態洩漏（已知 MEDIUM issue），單獨跑 pass 但批次跑可能 fail。根因：conftest.py 的 `client` fixture 不清理資料。短期用 `rm -f data/omnisight.db*` 規避，長期需加 DB truncation fixture。 |
| **協調者任務拆分** | Phase 38 已改善：LLM 輔助拆分（fallback regex）+ 子任務自動 depends_on 鏈 + 移除 bare "and" 誤切。已知限制：LLM 不可用時 regex 仍無法處理逗號分隔。 |
| **Provider/Model 架構** | 全域設定（Settings）= 預設 model。Agent Matrix 可指定 `provider:model` 格式覆蓋 per-agent。Orchestrator chat 走全域。INVOKE 走 agent 指定的。兩個 Settings 入口已透過 SSE 同步。 |
| **產物管線** | Phase 39 已修復：finalize() 自動收集 build outputs 到 .artifacts/ + register_build_artifact tool 供所有 agent 使用 + 前端下載按鈕已接線 + Gerrit merge 自動打包。 |
| **LLM 錯誤韌性** | Phase 42 已修復：11 類錯誤分類器 + exponential backoff（429/503/529 + Retry-After）+ invoke-time failover + 401/402 永久標記 + context overflow→L2 壓縮 + 前端 retry 429/503 + SSE 通知 + 統一 max_retries=3（6 個 SDK）。 |
| **Git 多 Repo 認證** | Phase 43 已修復：git_credentials.yaml registry + JSON map 欄位 + 3 層 fallback（YAML → JSON map → scalar）。per-host token/SSH key 解析。webhook secret per-instance routing。向後相容：舊 .env 單一值自動建立 default 條目。 |
| **權限自動修復** | Phase 44 已修復：9 類權限錯誤分類器 + auto-fix（chmod/cleanup/lock/port）不計 retry。不可修復的（docker/command_not_found）emit SSE 帶具體修復指令。workspace provision 前預防性環境檢查（disk/docker/git/ssh）。 |
| **SDK 自動偵測** | Phase 45 已修復：sdk_git_url 欄位 + SDK provisioner 自動 clone/scan → 發現 sysroot/cmake → 自動更新 platform YAML。get_platform_config/container.py/simulate.sh 在路徑缺失時明確警告而非靜默跳過。POST /vendor/sdks/{platform}/install 一鍵安裝。 |
| **全流程自動串聯（Phase 46 ✅）** | 已實作 E2E Pipeline：7 步自動串聯 + NPI phase linkage（npi_phase_id）+ auto_advance + 人類 checkpoint（Gerrit +2 / HVT）+ force_advance API + on_task_completed 自動推進 + finalize 自動呼叫 + /pipeline 指令（start/advance/status）。 |
| **自主決策缺口（Phase 47 修復）** | INVOKE 一次只跑一個（`_invoke_lock`），多 Agent 不能真正並行。系統不知道 deadline，不會自動趕工。Budget 凍結後需人工 reset，不會自動調整策略。Agent 卡住時盲目 retry 同一方法，不會切換 model 或 spawn 另一個 Agent 用不同方法。沒有 ambiguity 處理（遇到不確定的決策就卡住）。沒有操作模式概念（Manual/Supervised/FullAuto/Turbo）。 |

### 待辦事項（Backlog — 非 Phase 排程）

| 項目 | 說明 | 觸發條件 |
|------|------|---------|
| Protocol Buffers（protobuf）定義 | 為所有 Agent 間通訊、API 契約、事件格式定義 .proto 檔案 | 微服務拆分 or gRPC 需求出現時 |
| gRPC 服務介面 | 將 REST API 轉為 gRPC（高效能跨語言通訊） | 跨語言 agent 或外部系統整合時 |
| API 版本管理機制 | 支援多版本 API 並行（/api/v1, /api/v2） | 有外部 API 消費者時 |

---

## Phase 62-65 — Agentic Self-Improvement（未來排程，2026-04-14 規劃）

設計源：`docs/design/agentic-self-improvement.md`（四階進化架構 L1-L4）。
總原則：**提案 → Decision Engine → operator/admin 審核 → 執行**；所有自
寫/自改路徑強制通過 audit log。`CLAUDE.md` (L1 memory) 與 `configs/roles/*`
永禁自動寫入，Evaluator 白名單僅允許 `backend/agents/prompts/`。

新環境開關：`OMNISIGHT_SELF_IMPROVE_LEVEL` ∈ `{off, l1, l1+l3, all}`，預設
`off`，企業部署可分級授權。

### Phase 62 — Knowledge Generation（L1，3–4h，先行低風險）
- `backend/skills_extractor.py`：訂閱 `workflow_runs.status=success` 事件，
  門檻 `step_count ≥ 5` 或 `retry_count ≥ 3`。
- LLM 摘要器 → `configs/skills/_pending/skill-<slug>.md`，含 frontmatter
  `trigger_kinds / platform / symptoms / resolution / confidence`。
- PII / secret scrubber（regex 白名單 + secret pattern 黑名單）。
- Decision Engine `kind=skill/promote` severity=`routine`，operator 核可
  後才移入 `configs/skills/`。
- Metrics：`skill_extracted_total{status}`、`skill_promoted_total`。

### Phase 63 — Intelligence Immune System (IIS)（重新拆分為 63-A→E）

設計源：`docs/design/intelligence-immune-system.md`。原 Phase 63
（Meta-Prompting Evaluator）已**整體吸收**為 63-C，並擴充為四指標
監控 + 三級應變 + prompt 版控 + IQ benchmark + memory decay 完整套件。

**已敲定決策**：
1. Phase 63 重命名為 IIS 全套（吸收原 Meta-Prompting Evaluator）。
2. Tier-1 COT 強制長度 profile-aware：`cost_saver=0`、
   `BALANCED=200 char`、`QUALITY=500 char`。
3. Tier-3 Jira 自動掛單預設 **off**（`OMNISIGHT_IIS_JIRA_CONTAINMENT=false`）。
4. Daily IQ Benchmark 題庫：**手動策展**（`configs/iq_benchmark/*.yaml`），
   避免從 episodic_memory 自動產生造成的自我參照偏誤。
5. 與 Phase 62 順序：**先 62 後 63-A**，62 產出的技能檔可餵給 63-B
   的 few-shot 注入。

#### 子任 / 工時

| 子任 | 工時 | 內容 |
|---|---|---|
| **63-A** Intelligence Metrics Collector | 4–5h | `backend/intelligence.py` 滑動窗口（size=10）收集 4 指標：code pass rate / constraint compliance / logic consistency vs L3 / token entropy z-score；新 Gauge `intelligence_score{agent_id,dim}` + Counter `intelligence_alert_total{agent_id,level}`；只發訊號，不觸發應變 |
| **63-B** Mitigation Layer (Decision Engine 接口) | 5–6h | `intelligence_mitigation.py` 把指標換成 Decision Engine `propose()`：L1 `kind=intelligence/calibrate` severity=routine（context reset + few-shot 注入 + profile-aware COT）；L2 `kind=intelligence/route` severity=risky（重用 `_apply_stuck_remediation(switch_model)`）；L3 `kind=intelligence/contain` severity=destructive（halted + critical Notification + 可選 Jira） |
| **63-C** Prompt Registry + Canary（原 63 主體） | 4–6h | `prompt_registry.py` + DB `prompt_versions`；5% canary、7 天監控、自動 rollback；路徑白名單僅 `backend/agents/prompts/`，`CLAUDE.md` 永禁 |
| **63-D** Daily IQ Benchmark | 3–4h | `configs/iq_benchmark/*.yaml` 10 題手動策展；nightly cron 跑 active model + chain 中其他 3 model；`intelligence_iq_score{model}` Gauge；連 2 天低於 baseline → Notification level=action；token budget cap |
| **63-E** Memory Quality Decay | 2–3h | `episodic_memory` 加 `last_used_at` + `decayed_score` 欄（Alembic）；nightly worker 未用 >90 天 `decayed_score *= 0.9`；FTS5 排序加權；**只降權不刪除**，提供 admin restore endpoint |

**累計工時**：18–24h，分 5 commit 批。

#### 與既有系統的接點

- **Decision Engine**：所有 mitigation 走既有 propose/resolve；新增 3 類 kind 預估 queue 壓力 +60%。
- **Stuck Detector (Phase 47B)**：與 IIS L2 共用 `switch_model` 策略；用 `(agent_id, kind)` de-dupe 防雙重觸發。
- **Audit Log (Phase 53)**：每次 mitigation 寫一筆，hash chain 不變；體積 +15–25%。
- **Notification (Phase 47)**：L3 走既有 critical → PagerDuty。
- **Profile (Phase 58)**：Tier-1 COT 長度由 profile 決定。
- **Skills (Phase 62)**：Phase 62 產出的 `configs/skills/*.md` 是 63-B few-shot 注入的來源。
- **Phase 65 Hold-out Eval**：與 63-D 共用題庫，省一份維護成本。

#### 風險摘要

| 風險 | 等級 | Mitigation |
|---|---|---|
| Alert fatigue | 高 | 雙重門檻（連 3 次）+ profile-aware threshold |
| Tier-1 COT 拖垮 cost_saver | 中 | profile-aware COT 長度 |
| Tier-3 Jira 洩密 | 高 | PII scrubber + opt-in |
| 模型切換 code style 不一致 | 中 | commit `[via=<model>]` + 同 task 內鎖 provider |
| Decision Engine 雙重觸發 | 中 | 既有 `_open_proposals` de-dupe |
| L3 Memory 降權誤殺 | 中 | 只降權、不刪除 + admin restore |
| Logic Consistency NLP 假警 | 高 | v1 簡化 cosine threshold；不觸發 L3 |
| `CLAUDE.md` compliance 由 LLM 檢查 | 嚴重 | **嚴禁** — 改用 git diff 規則 |

#### 觀測性

- 新 metrics：`intelligence_score{dim}`、`intelligence_alert_total{level}`、
  `intelligence_iq_score{model}`、`prompt_version_active{agent}`、
  `prompt_canary_success_rate`、`prompt_reverted_total`、`memory_decayed_total`。
- `/healthz` 加 `intelligence: {<dim>: score}` 區塊（沿用 64-D sandbox 區塊範本）。

#### 啟動順序

```
Phase 62 → 63-A → 63-B → 63-C → 63-D → 63-E
```

63-A 可在沙盒 (64-A/D/B) 任何後啟動；63-B 起需 Phase 62 技能檔。

### Phase 64 — Tiered Sandbox Architecture（重新拆分為 64-A/B/C/D）

設計源：`docs/design/tiered-sandbox-architecture.md`（四層隔離模型）。
原 Phase 64 (Toolmaking + Sandbox L2) 已併入 64-A 與「自我進化 Phase 64
(L2 Toolmaking)」整合：toolmaking 提案流程仍在 Phase 63A、sandbox
runtime 升格為 Tier 1 的子集。

**現況盤點（與設計對應）**：

| Tier | 完成度 | 缺口 |
|---|---|---|
| T0 Control Plane | 70% | `agents/tools.run_bash` host fallback 仍直接 exec |
| T1 Strict Sandbox | 70% | gVisor/Firecracker 未採；無 git-server 白名單 egress |
| T2 Networked Sandbox | 0% | 整層待建 |
| T3 Hardware Bridge | 15% | 模型有，daemon 程序不存在 |
| Killswitch | 80% | 無統一 45min sandbox lifetime cap |

**決策（已敲定）**：
- 沙盒引擎：**gVisor (`runsc`)**，保留 docker CLI 相容；不採 Firecracker。
- 編排：**docker 直驅，不引入 K8s/Nomad**；多 host 規模再評估。
- T3 daemon 部署：**per-machine systemd**，agent 透過 mDNS 發現。
- 啟動順序：**先 64-A 後 Phase 62**，避免技能檔執行時無沙盒裸奔。

#### Phase 64-A — Tier 1 Hardening（5–8h，**最高 ROI，建議優先**）
- `backend/container.py` 新增 `runtime: "runsc" | "runc"` 設定（env
  `OMNISIGHT_DOCKER_RUNTIME`），預設 `runsc` 若可用，fallback `runc`。
- 白名單 egress：自建 `omnisight-egress` bridge + iptables ACCEPT 配置
  的 git host 清單（`OMNISIGHT_T1_EGRESS_ALLOW_HOSTS`）。
- Image immutability check：`docker image inspect` 比對 sha256，未授權
  image 拒絕 launch。
- 統一 `OMNISIGHT_SANDBOX_LIFETIME_S=2700`（45 min），watchdog SIGKILL。
- **驗收**：sandbox 內 `socket.connect(('1.1.1.1',80))` timeout；
  `git clone github.com/...` 通過。
- Metrics：`sandbox_launch_total{tier,runtime}`、`sandbox_egress_blocked_total`。

#### Phase 64-D — Killswitch 統一（2h，64-A 之後立即補）
- 全 sandbox lifetime cap = `OMNISIGHT_SANDBOX_LIFETIME_S=2700`（45 min）
  共享於 T1/T2。
- 既有 `subprocess_orphan_total` (Fix-A S6) 擴 label `tier`。
- output 截斷與 `rtk` 串接；cap 由各 Tier 表設定。

#### Phase 64-B — Tier 2 Networked Sandbox（4–6h，**Phase 65 硬性前提**）
- `container.start_network_container()` 走自建 `omnisight-egress-only`
  bridge：iptables DENY `10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`
  + 特定企業 CIDR；ACCEPT 其餘外部 IP。
- Caller 必須標 `tier="networked"` opt-in；Decision Engine 列為
  `risky` severity，operator approve 才可啟動。
- 重用 64-A 的 lifetime cap 與 metrics labels。
- 用例：MLOps 資料下載、第三方 API 測試、Phase 65 訓練資料外送。

#### Phase 64-C — Tier 3 Hardware Daemon（10–14h，**最重，獨立 track**）
- 新 `tools/hardware_daemon/`（FastAPI 服務，systemd unit）。
- 白名單 action map：`flash_board` / `read_uart` / `power_cycle` / `i2c_read`。
- mTLS 雙向認證 + 每 action 寫 audit log（hash chain，重用 Phase 53）。
- 後端 `routers/hardware.py` 改純 HTTP proxy，移除 `host_native.py`
  直接 exec 路徑。
- **絕不允許** agent SSH 進 daemon host；違反偵測由 Phase 53 audit
  log 監控。
- 用例：EVK 燒錄、UART 串口讀取、I2C/SPI 訊號擷取。

### Phase 65 — Data Flywheel / Auto-Fine-Tuning（L4，10–14h）

**依賴更新**：原僅依 Fix-D，現追加 **Phase 64-B 必須先完成**（訓練資料
外送至 OpenAI fine-tune / 本地 Llama via Unsloth → 必走 T2 egress；
若無 T2 會違反「T0 不執行外送」原則）。

- `scripts/export_training_set.py`：`workflow_runs` ⨝ `audit_log` ⨝ git
  diff → JSONL。雙閘：`status=success` AND `hvt_passed=true` AND
  resolver ∈ {user, auto+user-approved} AND pii_scrub_pass。
- 最短路徑演算法：從 final commit 回推 DAG，剔除失敗 branch 步驟（避免
  feedback-loop poisoning）。
- Hold-out evaluation set：人工標記 100 題，每次微調後必跑 benchmark
  （成功率、平均回合數），未通過則不 promote。
- MLOps：nightly GitHub Action 或 `make finetune`；訓練 job **必在
  Phase 64-B Tier 2 sandbox 內執行**，由 `OMNISIGHT_FINETUNE_BACKEND`
  選 Unsloth/Llama 或 OpenAI API。
- Metrics：`training_set_rows`、`finetune_run_total{outcome}`、`finetune_eval_score`。

### 風險 / Mitigation 總表

| 風險 | 等級 | Mitigation |
|---|---|---|
| Agent 寫惡意 / 蠢 script | 高 | Admin approve + sandbox + deny-list + audit |
| Prompt drift 致退化 | 中 | Canary + metric gate + 7 天自動 revert |
| 技能檔 / 訓練集洩密 | 中 | PII/secret scrubber + 強制 review |
| Feedback-loop poisoning (L4) | 高 | 只採 user-approved + HVT-passed；hold-out eval |
| Evaluator token 爆炸 | 中 | Sample + cache + 每晚 budget cap |
| `CLAUDE.md` 被自動改 | 嚴重 | Path 白名單 + pre-commit hook 雙保險 |
| 對小型 deployment 過重 | 中 | `OMNISIGHT_SELF_IMPROVE_LEVEL` opt-in |

### 預估影響

- Token 用量：L1+L2 預期 -30–50%（skill 複用 + 專用 parser）。
- 任務成功率：L3 對失敗 cluster 預期下降 15–25%。
- Audit log 體積：+20–40%（tool_exec event） → 需 retention policy。
- CI 時間：+ script test gate + finetune eval；離峰執行降低影響。
- Decision Engine queue 壓力：新增 3 類 `kind`（`skill/promote`、
  `prompt/patch`、`tool/register`）→ 搭配 `BALANCED` profile 以上自動
  消化。

### 優先序建議（已調整 — 沙盒前置）

```
[已完成] 64-A ✅ → 64-D ✅ → 64-B ✅
   ↓
Phase 62  (Knowledge Generation, 3–4h)        ← 沙盒就位後解鎖
   ↓
Phase 63-A (IIS Metrics Collector, 4–5h)      ← IIS 訊號層
   ↓
Phase 63-B (IIS Mitigation Layer, 5–6h)       ← 接 Decision Engine
   ↓
Phase 63-C (Prompt Registry + Canary, 4–6h)   ← 原 Meta-Prompting
   ↓
Phase 63-D (Daily IQ Benchmark, 3–4h)         ← 與 Phase 65 共題庫
   ↓
Phase 65   (Data Flywheel, 10–14h)            ← 64-B 已就位
   ↓
Phase 63-E (Memory Quality Decay, 2–3h)       ← 任意時段
   ↓
Phase 64-C (T3 hardware daemon, 10–14h)       ← 獨立 track，需實機，可平行
```

**取代原 62→63→64→65 線性排程**。Self-improvement（62/63/65）皆為
`OMNISIGHT_SELF_IMPROVE_LEVEL` opt-in 預設 off；沙盒分層（64-A/B/C/D）
為基礎建設，預設啟用 64-A，64-B/C 須環境準備（runsc / EVK 實機）。

### Phase 64 副作用（補充）

- **效能**：gVisor + per-task ephemeral container → cold-start +1–3s；
  長編譯不受影響。
- **Dev 體驗**：本機需安裝 `runsc`，onboarding 多一步；可 fallback runc。
- **Ops**：T3 daemon 需獨立部署 + 監控；mDNS 需在 prod LAN 開放 5353 mDNS。
- **平台限制**：Firecracker 已決策不採；macOS/WSL2 dev 用 runc fallback
  即可，CI/prod 強制 runsc。
- **與 docker-compose.prod.yml**（Phase 52）對齊：sidecar Prometheus +
  Grafana 屬 T0，不走 sandbox runtime。

---

## 1. 本次對話完成的核心邏輯

### Phase 1-5（commit `b386199`）
- **Bug 修復**：tool error detection（[ERROR] prefix → success=False）、INVOKE 併發保護（asyncio flag）、backend 緊急停止（halt/resume endpoints）
- **Token Usage 追蹤**：LangChain TokenTrackingCallback → `track_tokens()` → DB 持久化
- **Self-Healing Loop**：error_check_node → retry（最多 3 次）→ 人類升級（awaiting_confirmation）
- **單元測試框架**：pytest + conftest.py（workspace fixture, DB init）
- **SQLite 持久化**：agents, tasks, token_usage 表 + lifespan init + seed defaults

### Phase 6（commit `0f6ed86`）
- **Git 認證**：`git_auth.py`（SSH key + HTTPS token + GIT_ASKPASS），支援 GitHub/GitLab/Gerrit platform detection
- **PR/MR 建立**：`git_platform.py`（GitHub via `gh` CLI + GitLab via REST API）
- **多 Remote 管理**：`git_add_remote` tool + `git_remote_list` tool
- **Base Branch 偵測**：`_detect_base_branch()`（自動偵測 main/master/develop）

### Phase 7（commit `0f6ed86`）
- **Prompt Loader**：`prompt_loader.py`（fuzzy model matching + role skill 載入 + handoff context 注入）
- **模型規則**：7 個 `configs/models/*.md`（Claude Opus/Sonnet/Mythos, GPT, Gemini, Grok, default）
- **角色技能**：12 個 `configs/roles/**/*.skill.md`（BSP/ISP/HAL/Algorithm/AI-Deploy/Middleware/SDET/Security/Compliance/Documentation/Code-Review/CICD）
- **Handoff 自動產生**：`handoff.py` → workspace finalize 時自動生成 + DB 持久化 + 下個 agent 載入

### Phase 8（commit `0f6ed86`）
- **Gerrit Client**：`gerrit.py`（SSH CLI → query/review/inline comments via stdin/submit）
- **AI Reviewer Agent**：AgentType.reviewer + restricted tools（read-only + review）+ code-review.skill.md
- **Gerrit Webhook**：`POST /webhooks/gerrit`（patchset-created → auto-review、comment-added -1 → notify、change-merged → replication）
- **Gerrit Push**：`git_push` 自動偵測 Gerrit → `refs/for/{target_branch}`

### Phase 9（commit `cec0e6a`）
- **Token Budget**：三級閾值（80% warn → 90% auto-downgrade → 100% freeze）+ `GET/PUT /token-budget` + `POST /token-budget/reset`
- **Provider Failover**：`llm_fallback_chain` config → `get_llm()` 自動遍歷 chain → 全失敗 emit 通知
- **智慧匹配**：`_score_agent_for_task()`（type 10分 + sub_type keywords 5分 + ai_model 1分 + base 2分）
- **加權路由**：`_rule_based_route()` 回傳 `(primary, secondary_routes)` + skill file keywords 合併
- **Task 拆解**：`_maybe_decompose_task()` 偵測 "and/then/然後" → 自動拆分 + parent/child 關聯

### Phase 10（commit `21b0912`）
- **通知模型**：NotificationLevel (info/warning/action/critical) + Notification model + DB 持久化
- **路由引擎**：`notifications.py` `notify()` → SSE push + L2 Slack + L3 Jira Issue + L4 PagerDuty
- **事件源標註**：Gerrit webhook → L1/L2, Token budget → L2/L3/L4, Agent retries exhausted → L3, Agent error → L3
- **前端通知 UI**：鈴鐺 badge + NotificationCenter slide-in panel + filter tabs + 已讀管理

### Phase 11（commits `f4e57e9` → `aa83172`）
- **Task 模型擴展**：external_issue_id, issue_url, acceptance_criteria, labels, in_review status
- **State Machine**：TASK_TRANSITIONS dict + `GET /transitions` + `PATCH /tasks/{id}` 驗證 + `force=true` 繞過
- **Fact Gate**：in_review 需 workspace commit_count > 0
- **Task Comments**：task_comments DB 表 + `GET/POST /tasks/{id}/comments`
- **Wrapper Tools**：get_next_task（context window 保護）, update_task_status（state machine 驗證）, add_task_comment
- **外部同步**：`issue_tracker.py`（GitHub Issues via gh + GitLab Issues via REST + Jira via transition query）

### Phase 12（commit `0721d13`）
- **輸出壓縮引擎**：`output_compressor.py`（dedup + ANSI strip + progress bar removal + pattern collapse）
- **100% Tool 覆蓋**：在 `tool_executor_node` 統一攔截所有 25 個 tool 的輸出
- **Retry Bypass**：`rtk_bypass` flag → retry_count >= 2 時 bypass 壓縮 → 成功後 reset
- **壓縮統計**：`GET /system/compression` + OrchestratorAI OUTPUT COMPRESSION 面板
- **Docker**：Dockerfile.agent 加入 RTK install

### Phase 13 — NPI 生命週期（commits `7f587b8` → `9a0f100`）
- **NPI 資料模型**：NPIPhase, NPIMilestone, NPIProject, BusinessModel + npi_state DB 表
- **8 Phase × 3 Track × 4 商業模式**：PRD → EIV → POC → HVT → EVT → DVT → PVT → MP，Engineering/Design/Market 三軌
- **商業模式切換**：ODM（1 軌）/ OEM / JDM / OBM（3 軌），4 種色彩區分
- **7 個 NPI 角色 skill**：mechanical, manufacturing, industrial-design, ux-design, marketing, sales, support（19 total）
- **科幻 Timeline UI**：垂直 timeline + 展開/收合 milestone + 自動 phase status 計算
- **修復**：phase auto-compute pending fallback、grid overflow、mobile nav、status validation、error handling

### Phase 14 — Artifact 生成管線（commits `9c8a005` → `90d6b6f`）
- **Jinja2 模板引擎**：`report_generator.py` + `configs/templates/` (compliance_report.md.j2, test_summary.md.j2)
- **generate_artifact_report tool**：LLM 或 rule-based 皆可觸發 + task_id 自動注入
- **Artifact 下載 + 路徑安全**：`GET /artifacts/{id}/download` + resolve() + startswith() 驗證
- **修復**：path traversal 防護、Jira transition 驗證、task_id 注入

### Phase 15 — 雙軌模擬驗證（commits `d6345cf` → `07d20fb`）
- **simulate.sh**：統一模擬腳本（algo 資料驅動回放 + hw mock sysfs / QEMU 交叉執行）
- **run_simulation tool**：120s timeout、JSON 報告解析、DB 持久化、SSE 事件
- **防呆機制**：test_assets/ :ro 掛載、simulate.sh :ro、coverage 強制、run_bash 攔截引導
- **多 SoC 預埋**：3 platform profiles（aarch64/armv7/riscv64）、--platform 參數
- **Dockerfile.agent**：+valgrind +qemu-user-static
- **Container 強化**：Dockerfile hash 版本化、條件 :ro mount
- **修復**：API route 404、shell injection、SQL injection、JSON escape、Valgrind XML tag、stderr 遺失

### Phase 16 — 錯誤處理與回復機制（commits `8079bc1` → `4fdbba1`）
- **DB 強化**：WAL 模式 + busy_timeout 5s + integrity check
- **Graph Timeout**：5 分鐘上限 via asyncio.wait_for
- **Startup Cleanup**：重置 stuck agents（>1hr）、stuck simulations、孤兒容器、stale git locks
- **Watchdog**：60s 掃描 + 30min task timeout + 2hr stuck task → blocked + asyncio.Lock 防競爭
- **Container 資源限制**：--memory=1g --cpus=2 --pids-limit=256
- **LLM Circuit Breaker**：5min provider cooldown + failover chain 改進
- **Token Budget 每日重置**：midnight auto-unfreeze
- **Emergency Halt 強化**：cancel background tasks + stop containers + update agents
- **Agent Force Reset API**：`POST /agents/{id}/reset` 清理 workspace + container
- **前端**：Error Boundary (error.tsx) + fetch 15s timeout + 2x retry（僅冪等方法）+ Promise.allSettled
- **修復**：POST 重試限制、watchdog race condition、task cancel await、memory leak

### Phase 18 — Anthropic Skills 導入（commits `e8e95a8` → `e605b3c`）
- **Task Skill 系統**：`configs/skills/{name}/SKILL.md` 格式，`load_task_skill()` + `match_task_skill()` + `list_available_task_skills()` + 快取
- **4 個 Anthropic Skills**：webapp-testing（Playwright 自動化）、pdf-generation（PDF 報告）、xlsx-generation（Excel 試算表）、mcp-builder（MCP Server 開發）
- **Prompt 注入**：`build_system_prompt()` 新增 `task_skill_context` 參數，注入於 role skill 和 handoff 之間
- **自動匹配**：`_run_agent_task()` 自動比對 task 標題關鍵字 → 載入最佳匹配的 task skill
- **格式改進**：19 個 role skill 加入 `description` 欄位 + `list_available_roles()` 回傳 description
- **GraphState 擴展**：新增 `task_skill_context` 欄位，`run_graph()` 完整傳遞
- **延後子項**：18D Docker+Playwright、18E Validator 整合、18F Reporter 整合（待有實際測試/報告需求時實作）

### Phase 19 — 智慧對話系統（commit `cee8f6b`）
- **意圖偵測**：orchestrator_node 先判斷「對話 vs 任務」— LLM 回傳 CONVERSATIONAL 或 specialist 名稱
- **Rule-based fallback**：`_is_question()` 正則偵測中英文問句（what/how/why/什麼/怎麼/為什麼/建議...）
- **conversation_node**：無工具綁定 LLM，注入即時系統狀態（agent/task 數量），直接回答
- **Graph 平行路徑**：orchestrator → conversation → summarizer（完全繞過 specialist + tool_executor）
- **前端統一入口**：Orchestrator 面板只保留 help/clear 本地回應，其他全部送到後端 LLM（含 token streaming）
- **離線 fallback**：無 LLM 時回傳系統狀態摘要

### Phase 34 — 系統整合設定 UI（commits `2f9dded` → `e932239`）
- **GET /system/settings**：分類回傳所有設定（llm/git/gerrit/jira/slack/webhooks/ci/docker）+ token masking
- **PUT /system/settings**：runtime 更新 + 白名單驗證 + LLM cache 清除
- **POST /system/test/{type}**：6 種整合測試（SSH/Gerrit/GitHub/GitLab/Jira/Slack）+ 15s timeout
- **Vendor SDK CRUD**：POST 建立 + DELETE 移除（保護 built-in）
- **前端 Integration Settings Modal**：5 個收合 section + TEST 按鈕 + 狀態指示 + Save/Discard
- **Header Settings 按鈕**：齒輪圖示觸發 modal

### Phase 29 — 快速指令系統（commits `a9636cf` → `589b195`）
- **指令註冊表**：`lib/slash-commands.ts` 前端 + `backend/slash_commands.py` 後端，22 指令 × 6 分類
- **Autocomplete UI**：InvokeCore + OrchestratorAI 雙入口，輸入 / 觸發下拉選單（分類 badge + 名稱 + 說���）
- **鍵盤導航**：↑↓ 選擇、Tab 確認、Esc 關閉
- **後端攔截**：chat.py `_try_slash_command()` 在 LLM pipeline 前處理 /status、/debug、/logs 等系統查詢
- **12 個後端 handler**：status/info/debug/logs/devices/agents/tasks/provider/budget/npi/sdks/help
- **開發指令**：/build、/test、/simulate、/review 透過 LLM pipeline 處理

### Phase 28 — SoC SDK/EVK 整合開發自動化（commits `537d01e` → `77e4edb`）
- **Platform YAML vendor 擴展**：vendor_id, sdk_version, sysroot_path, cmake_toolchain_file, deploy_method（向後相容）
- **vendor-example.yaml**：完整 vendor profile 範本（含 NPU、deploy、supported_boards）
- **hardware_manifest vendor section**：soc_model, platform_profile, npu_enabled
- **Container SDK mount**：讀 .omnisight/platform → 載入 YAML → 條件 :ro mount sysroot + toolchain
- **Workspace platform hint**：provision() 自動從 manifest 寫入 .omnisight/platform
- **simulate.sh cmake 支援**：--toolchain-file 參數 + 自動讀 platform YAML cmake_toolchain_file + SYSROOT
- **get_platform_config tool**：Agent 查詢 ARCH/CROSS_COMPILE/SYSROOT/CMAKE_TOOLCHAIN_FILE
- **BSP skill 參數化**：從硬編碼 arm64 改為 get_platform_config 動態取值 + vendor SDK 規範
- **GET /system/vendor/sdks**：列出所有 platform profiles 及 SDK mount 狀態

### Phase 27 — Agent Handoff 視覺化 + NPI 甘特圖（commits `ebff46c` → `19fd117`）
- **Handoff Chain API**：GET /tasks/{id}/handoffs + GET /tasks/handoffs/recent
- **HandoffTimeline 組件**：agent-to-agent 接力視覺化（色彩對應 agent type + 時間戳 + arrow connectors）
- **NPIGantt 組件**：橫向 phase bar chart（completed 綠 + in_progress 橙 pulse + blocked 紅 indicator）
- **NPI Timeline 雙模式**：header toggle 按鈕切換 Timeline/Gantt 視圖（BarChart3/List icon）
- **Orchestrator 整合**：HANDOFF CHAIN 收合區塊，展開時自動載入最近 handoffs

### Phase 26 — External → Internal Webhook 雙向同步（commits `d52744c` → `7463118`）
- **GitHub Webhook**：POST /webhooks/github + HMAC-SHA256 signature 驗證 + issue state → task status
- **GitLab Webhook**：POST /webhooks/gitlab + X-Gitlab-Token 驗證 + issue state → task status
- **Jira Webhook**：POST /webhooks/jira + Bearer token 驗證 + changelog status mapping
- **Sync Debounce**：5s 防迴圈（last_external_sync_at timestamp）
- **CI/CD Trigger**：change-merged → GitHub Actions (gh CLI) / Jenkins (curl) / GitLab CI (REST API)
- **Task 追蹤欄位**：external_issue_platform + last_external_sync_at + DB migration
- **Config**：github/gitlab/jira_webhook_secret + ci_github_actions/jenkins/gitlab_enabled

### Phase 25 — Provider Fallback Chain UI（commits `e1c2fa4` → `6beb72d`）
- **GET /providers/health**：回傳 chain 順序 + 每個 provider 狀態（active/cooldown/available/unconfigured）+ cooldown 倒數秒
- **PUT /providers/fallback-chain**：runtime 更新 fallback chain 順序 + 驗證 provider ID + 清除 LLM cache
- **前端 FAILOVER CHAIN**：Orchestrator 面板新區塊，numbered list + color-coded status dots + cooldown timer + 上下箭頭排序
- **Health polling**：每 10 秒自動刷新 provider 健康狀態

### Phase 24 — Agent 團隊協作（commits `8f6a7f3` → `2b444bc`）
- **CODEOWNERS**：`configs/CODEOWNERS` 設定檔 + `backend/codeowners.py` 解析器（soft/hard enforcement, directory prefix + filename matching）
- **write_file 權限檢查**：hard-block → [BLOCKED]、soft-own → warning log、unowned → 允許
- **Pre-merge conflict 偵測**：finalize() 在 commit 後 test-merge 到 base branch，偵測 CONFLICT 檔案
- **Agent.file_scope**：從 CODEOWNERS 解析的 glob patterns
- **修復**：fnmatch → 自製 _match_codeowner_pattern、base branch 存在性檢查

### Phase 23 — 調度強化（commits `f97eda0` → `68ee69e`）
- **Pre-fetch 檢索子智能體**：`_prefetch_codebase_context()` 從任務標題提取關鍵字 → asyncio.to_thread 搜索 workspace → 注入 handoff_context
- **任務依賴圖**：Task.depends_on 欄位 + `_plan_actions()` 依賴檢查（缺失依賴 = 阻塞，安全預設）
- **動態重分配**：Watchdog 偵測 blocked task + idle agent → 重置 task 為 backlog → INVOKE 重新分派
- **修復**：sync I/O → asyncio.to_thread、stop words 移至 module level、rglob 去 sorted、None 依賴阻塞

### Phase 22 — 生成-驗證閉環（commits `4850440` → `38ebdd6`）
- **Verification Loop**：error_check_node 偵測 [FAIL] prefix → 與 tool error 分離的獨立迴圈
- **GraphState**：verification_loop_iteration + max_verification_iterations(=2) + last_verification_failure
- **Specialist Prompt 注入**：verification failure 優先於 tool error（互斥 elif）
- **_should_retry 擴展**：3 路徑判斷（loop breaker → tool retry → verification retry → summarizer）
- **Gerrit -1 自動修復**：_on_comment_added 偵測 -1 → 建立 high-priority fix task + 提取 reviewer feedback
- **修復**：tool error 優先於 [FAIL]、off-by-one（<= → <）、prompt 互斥、state 清理

### Phase 21 — 消息總線強化（commits `4eab250` → `0fba6a2`）
- **通知 DLQ**：dispatch_status/send_attempts/last_error 欄位 + _send_with_retry() exponential backoff（3 次）+ 失敗列表 API
- **事件持久化**：event_log DB 表 + EventBus publish() 自動持久化白名單事件（6 類）+ cleanup_old_events（7 天）
- **事件重播 API**：`GET /events/replay?since=&types=&limit=` 查詢 event_log + JSON 回傳
- **Queue 強化**：maxsize=1000（防記憶體洩漏）+ slow subscriber 自動踢除
- **外部 dispatch 異常化**：Slack/Jira/PagerDuty 失敗改 raise RuntimeError（而非靜默 log）

### Phase 20 — Debug Blackboard + 迴圈斷路器（commit `17b7ee8`）
- **debug_findings DB 表**：task_id, agent_id, finding_type, severity, content, context, status
- **語義迴圈偵測**：error_history 追蹤跨 retry 的錯誤鍵值，same_error_count 計數連續相同錯誤，loop_breaker_triggered 強制跳出
- **_extract_error_key()**：從 error summary 提取 tool name 做比對
- **_should_retry() 強化**：loop_breaker → 直接到 summarizer（不再浪費 retry）
- **emit_debug_finding**：SSE 事件廣播除錯發現
- **GET /system/debug**：聚合 agent errors + blocked tasks + findings by type
- **對話注入**：_build_state_summary() 加入 open debug findings
- **GraphState.task_id**：修復 pre-existing latent bug（tool_executor 引用不存在的欄位）

### 審計修復（累計 4 輪，~70 個問題）
- Phase 1-12 審計：shell injection、token freeze propagation、SSE reconnect、deadlock、slider UX、z-index、壓縮防禦
- Phase 13-14 審計（8 issues）：NPI auto-compute、grid overflow、mobile nav、PATCH validation、report task_id、error handling
- Phase 15 審計（21 issues）：API route 404、input sanitization、SQL injection、JSON escape、Valgrind tag、stderr capture、tests_failed
- Phase 16 審計（8 issues）：POST retry、asyncio.Lock、watchdog cancel、memory leak、container quoting、import、indices、timestamps

---

## 2. 修改的檔案清單（精確路徑）

### Backend 核心
```
backend/main.py
backend/config.py
backend/models.py
backend/events.py
backend/db.py
backend/workspace.py
backend/container.py
backend/requirements.txt
backend/docker/Dockerfile.agent
backend/pytest.ini
```

### Agent 系統
```
backend/agents/__init__.py
backend/agents/graph.py
backend/agents/nodes.py
backend/agents/llm.py
backend/agents/tools.py
backend/agents/state.py
```

### API Routers
```
backend/routers/__init__.py
backend/routers/health.py
backend/routers/agents.py
backend/routers/tasks.py
backend/routers/chat.py
backend/routers/invoke.py
backend/routers/tools.py
backend/routers/providers.py
backend/routers/events.py
backend/routers/workspaces.py
backend/routers/system.py
backend/routers/webhooks.py
```

### 新增模組
```
backend/git_auth.py
backend/git_platform.py
backend/gerrit.py
backend/handoff.py
backend/prompt_loader.py
backend/notifications.py
backend/issue_tracker.py
backend/output_compressor.py
```

### 測試（15 個檔案）
```
backend/tests/__init__.py
backend/tests/conftest.py
backend/tests/test_graph.py
backend/tests/test_nodes.py
backend/tests/test_tools.py
backend/tests/test_git_auth.py
backend/tests/test_git_platform.py
backend/tests/test_gerrit.py
backend/tests/test_handoff.py
backend/tests/test_prompt_loader.py
backend/tests/test_webhooks.py
backend/tests/test_dispatch.py
backend/tests/test_token_budget.py
backend/tests/test_issue_tracking.py
backend/tests/test_output_compressor.py
```

### Config 檔案（21 個）
```
configs/hardware_manifest.yaml
configs/client_spec.json
configs/models/_default.md
configs/models/claude-opus.md
configs/models/claude-sonnet.md
configs/models/claude-mythos.md
configs/models/gpt.md
configs/models/gemini.md
configs/models/grok.md
configs/roles/firmware/bsp.skill.md
configs/roles/firmware/isp.skill.md
configs/roles/firmware/hal.skill.md
configs/roles/software/algorithm.skill.md
configs/roles/software/ai-deploy.skill.md
configs/roles/software/middleware.skill.md
configs/roles/validator/sdet.skill.md
configs/roles/validator/security.skill.md
configs/roles/reporter/compliance.skill.md
configs/roles/reporter/documentation.skill.md
configs/roles/reviewer/code-review.skill.md
configs/roles/devops/cicd.skill.md
```

### 前端
```
app/page.tsx
app/api/chat/route.ts
components/omnisight/agent-matrix-wall.tsx
components/omnisight/orchestrator-ai.tsx
components/omnisight/global-status-header.tsx
components/omnisight/token-usage-stats.tsx
components/omnisight/task-backlog.tsx
components/omnisight/notification-center.tsx
hooks/use-engine.ts
lib/api.ts
lib/providers.ts
.env.example
.gitignore
```

### 設計文件
```
HANDOFF.md
README.md
code-review-git-repo.md
organization_role_map.md
tiered-notification-routing-system.md
issue_tracking_system.md
rust_token_killer.md
```

---

## 3. 編譯與測試狀態

### Frontend Build
```
Status: PASS
Route (app)
  ○ /              (Static)
  ○ /_not-found    (Static)
  ƒ /api/chat      (Dynamic)
```
- `npm run build` 通過，零錯誤

### Backend
```
Status: PASS
FastAPI: ~60 routes loaded
Tests: 177 passed, 0 failed
Tools: 25 sandboxed tools
Agent Types: 8
Graph Nodes: 11
```
- `backend/.venv/bin/python -m uvicorn backend.main:app` 正常啟動
- LangGraph pipeline 測試通過（routing + tool execution + error_check + summarize）
- Workspace provision/finalize/cleanup 測試通過
- State machine transition 驗證測試通過
- Output compressor 測試通過（12 tests）
- Issue tracking 測試通過（20 tests）

### 已知限制
1. TypeScript 有若干非阻塞型別警告（`ignoreBuildErrors: true`）
2. RTK binary 在 Docker 容器內尚未實測（Dockerfile 已寫入 install 腳本）
3. Token usage tracking 需要有 LLM API key 才能產生真實數據
4. 外部工單同步需要配置對應的 API token（GitHub/GitLab/Jira）

---

## 4. 下一個對話接手後，立刻要執行的前十個步驟

### Step 1: 啟動開發環境並驗證

```bash
# Terminal 1: Backend
cd /home/user/work/sora/OmniSight-Productizer
backend/.venv/bin/python -m uvicorn backend.main:app --reload --port 8000

# Terminal 2: Frontend
npm run dev

# Terminal 3: Verify
curl http://localhost:3000/api/v1/health
# Expected: {"status":"online","engine":"OmniSight Engine","version":"0.1.0","phase":"3.2"}
```

打開瀏覽器 `http://localhost:3000`，確認：
- GlobalStatusHeader 顯示真實系統資訊 + 通知鈴鐺
- HostDevicePanel 顯示真實 CPU/RAM
- REPORTER VORTEX 有彩色標籤日誌
- Agent Matrix Wall 顯示 4 個預設 agent

### Step 2: 設定 LLM API Key（啟用智慧代理）

```bash
cp .env.example .env
# Edit .env, add at minimum:
echo 'OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-your-key-here' >> .env
```

重啟 backend 後驗證：
```bash
curl http://localhost:8000/api/v1/providers/test
# Expected: {"status":"ok","provider":"anthropic","model":"claude-sonnet-4-20250514","response":"OMNISIGHT_OK"}
```

### Step 3: 執行完整測試套件

```bash
backend/.venv/bin/python -m pytest backend/tests/ -v
# Expected: 177 passed

npx next build
# Expected: ✓ Compiled successfully
```

### Step 4: 深度審計確認系統完整性

執行深度分析確認所有功能正常運作，特別關注：
- RTK 壓縮引擎是否正確攔截所有 tool 輸出
- Token budget freeze 是否正確傳播到 `get_llm()`（module ref 而非 value copy）
- 外部工單同步是否在 task status 變更時觸發
- 通知鈴鐺和通知中心是否正常顯示
- State machine 是否阻擋非法狀態轉換

### Step 5: 閱讀設計文件

```
code-review-git-repo.md              # Gerrit 架構（單一審查閘道 + 單向 Replication）
organization_role_map.md             # 組織角色定義（5 層 34 個角色）
tiered-notification-routing-system.md  # 4 級通知路由（L1-L4）
issue_tracking_system.md             # 工單系統整合（AI 工作流 + 狀態機 + 幻覺防護）
rust_token_killer.md                 # RTK 壓縮（Docker 掛載 + Prompt 規範 + fallback）
```

### Step 6: 配置 Gerrit Server（如有）

```bash
# .env 加入:
OMNISIGHT_GERRIT_ENABLED=true
OMNISIGHT_GERRIT_SSH_HOST=gerrit.your-domain.com
OMNISIGHT_GERRIT_SSH_PORT=29418
OMNISIGHT_GERRIT_PROJECT=project/omnisight-core
OMNISIGHT_GERRIT_REPLICATION_TARGETS=github,gitlab
```

### Step 7: 配置通知管道（如需要）

```bash
# .env 加入:
OMNISIGHT_NOTIFICATION_SLACK_WEBHOOK=https://hooks.slack.com/services/...
OMNISIGHT_NOTIFICATION_SLACK_MENTION=U1234567  # Slack user ID for L3 @mention
OMNISIGHT_NOTIFICATION_JIRA_URL=https://jira.company.com
OMNISIGHT_NOTIFICATION_JIRA_TOKEN=...
OMNISIGHT_NOTIFICATION_JIRA_PROJECT=OMNI
OMNISIGHT_NOTIFICATION_PAGERDUTY_KEY=...
```

### Step 8: 設定 Token Budget

在前端 Orchestrator 面板 → TOKEN USAGE → ▼ SETTINGS：
- 選擇日預算（如 $10）
- 調整 Warn / Degrade 閾值
- 或透過 API：
```bash
curl -X PUT "http://localhost:8000/api/v1/system/token-budget?budget=10"
```

### Step 9: 測試 INVOKE 全流程

在前端按下 INVOKE ⚡ 按鈕或：
```bash
curl -X POST http://localhost:8000/api/v1/invoke
```

觀察：
- Task 自動分派到對應 Agent（按 sub_type 評分匹配）
- 複合 task 自動拆解（"write driver and run tests" → 2 個子 task）
- REPORTER VORTEX 即時顯示所有 [AGENT] [WORKSPACE] [TASK] 日誌
- 壓縮統計面板顯示 tokens saved

### Step 10: 規劃下一階段開發

依優先順序：
1. **Artifact 生成管線**（Reporter Agent + Jinja2 → PDF）
2. **真實攝影機串流**（GStreamer/FFmpeg + WebRTC/MJPEG）
3. **RTK binary 實機驗證**（Docker container 內測試）
4. **多專案管理**（project selector + 獨立 SSOT）
5. **External → Internal webhook**（外部工單 → 內部 Task 同步）

---

## 附錄：關鍵檔案快速參考

| 需求 | 檔案 |
|------|------|
| 加新的 API endpoint | `backend/routers/` 下新增 .py，在 `backend/main.py` 掛載 |
| 加新的 Agent tool | `backend/agents/tools.py` 加 `@tool` 函數，更新 TOOL_MAP 和 AGENT_TOOLS |
| 加新的 LLM provider | `backend/agents/llm.py` 的 `_create_llm()` + `lib/providers.ts` |
| 加新的 Agent role | `configs/roles/{category}/{role}.skill.md` + 前端 ROLE_OPTIONS |
| 加新的 Model rule | `configs/models/{model}.md`（fuzzy match 自動辨識） |
| 改 Agent 路由邏輯 | `backend/agents/nodes.py` 的 `_ROUTE_KEYWORDS` 或 orchestrator_node |
| 改 LangGraph 拓樸 | `backend/agents/graph.py` 的 `build_graph()` |
| 改前端狀態管理 | `hooks/use-engine.ts` |
| 改前端 API 呼叫 | `lib/api.ts` |
| 改 SSOT 規格 | `configs/hardware_manifest.yaml` |
| 改 INVOKE 行為 | `backend/routers/invoke.py` 的 `_plan_actions()` 和 `_score_agent_for_task()` |
| 改 Task 拆解邏輯 | `backend/routers/invoke.py` 的 `_maybe_decompose_task()` |
| 改 State Machine | `backend/models.py` 的 `TASK_TRANSITIONS` |
| 改通知路由 | `backend/notifications.py` 的 `_dispatch_external()` |
| 改外部工單同步 | `backend/issue_tracker.py` |
| 改 RTK 壓縮策略 | `backend/output_compressor.py` |
| 改 REPORTER VORTEX 色彩 | `components/omnisight/vitals-artifacts-panel.tsx` 搜尋 `tagColor` |
| 改 Docker 編譯環境 | `backend/docker/Dockerfile.agent` |
| 改 workspace 隔離邏輯 | `backend/workspace.py` |
| 改 Gerrit 整合 | `backend/gerrit.py` + `backend/routers/webhooks.py` |
| 改 Token Budget 閾值 | `backend/config.py` + `backend/routers/system.py` |
