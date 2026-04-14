# Glossary 名詞解釋

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

UI 與 log 使用的專有名詞。依英文字母序。

**Agent** — 專職 LLM worker。預設八種類型（firmware、software、
validator、reporter、reviewer、general、custom、devops），各有
`sub_type` 對應 `configs/roles/*.yaml` 的角色檔。每個 agent 有
獨立 git 工作區。

**Artifact** — pipeline 產出值得保留的任何檔案：編譯後 firmware
映像、模擬報告、release bundle。置於 `.artifacts/`，在
Vitals & Artifacts panel 呈現。

**Budget Strategy** — 五個 tuning knob 的具名組合（model tier、
max retries、downgrade threshold、freeze threshold、prefer parallel），
用來控制每次 agent 呼叫的成本。預設四策略：`quality`、`balanced`、
`cost_saver`、`sprint`。

**Decision** — AI 停下來決定要自行動作、問您、或 timeout fallback
的任何時點。帶 severity（`info` / `routine` / `risky` / `destructive`）
與選項列表。

**Decision Queue** — Pending 決策列表（panel 名稱與記憶體 list 同名）。
最新在上。

**Decision Rule** — 操作員自訂覆寫規則，比對 `kind` glob（如
`deploy/staging/*`）並指定 severity、預設選項、或自動執行模式。
規則持久化至 SQLite（Phase 50-Fix A1）。

**Emergency Stop** — 停止所有執行中 agent 與 pending invocation。
釋放 concurrency slot，發 `pipeline_halted`。按 Resume 恢復。

**Invoke** — 「全域同步」動作，要 orchestrator 盤點現狀並決定下一步。
也可帶自由指令（`/invoke fix the build`）。

**LangGraph** — 底層 agent graph 框架。日常用不到，但 log 中的
「graph state」、「reducer」即 LangGraph 語意。

**L1 / L2 / L3 memory** — 分層 agent 記憶。L1 = `CLAUDE.md` 不變
核心規則。L2 = 各 agent 角色 + 近期對話。L3 = episodic（可搜過往
事件，透過 FTS5）。

**MODE** — 全域自治等級，詳見 [operation-modes.md](operation-modes.md)。

**NPI** — New Product Introduction，硬體出貨週期：
Concept → Sample → Pilot → Mass Production。每階段有自己的 pipeline。

**Operation Mode** — MODE 的正式名。四值：manual、supervised、
full_auto、turbo。

**Pipeline** — 將 task 從「idea」推到「shipped」的有序步驟。
步驟組成 phase。Pipeline Timeline panel 視覺化當前執行。

**REPORTER VORTEX** — 左側捲動 log 顯示系統每個動作。每個
`emit_*()` 事件都寫到這裡。

**SSE** (Server-Sent Events) — 後端單向推送即時更新到所有瀏覽器
的通道。端點 `/api/v1/events`。Schema 在 `/api/v1/system/sse-schema`。

**Singularity Sync** — Invoke 的行銷名稱，同義詞。

**Slash command** — Orchestrator AI panel 內以 `/` 開頭的指令。
內建 `/invoke`、`/halt`、`/resume`、`/commit`、`/review-pr`，
加上 skill 系統定義的。

**Stuck detector** — 監測 agent 反覆同錯時提案補救決策（switch
model、spawn alternate、escalate）的 watchdog。每 60 秒執行。

**Sweep** — 週期性（預設 10 秒）將 deadline 已過的 pending 決策
逾時處理。可在 Decision Queue header 手動觸發。

**Task** — 工作單元。有指派 agent、優先級、狀態、父子樹、以及
可選的外部 issue 連結（GitHub、GitLab、Gerrit）。

**Token warning** — 每日 LLM token 預算達 80 % / 90 % / 100 % 時
發出 SSE 事件。90 % 觸發自動降級到更便宜模型。

**Workspace** — 各 agent 工作的隔離 git clone。置於
`OMNISIGHT_WORKSPACE`（預設暫存目錄）。狀態：`none | active |
finalized | cleaned`。

## 相關

- [Operation Modes](operation-modes.md)
- [Panels Overview](panels-overview.md)
- `backend/models.py` — 權威 enum 定義
