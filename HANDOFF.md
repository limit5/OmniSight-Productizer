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
- 47D：pending

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
