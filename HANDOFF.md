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
