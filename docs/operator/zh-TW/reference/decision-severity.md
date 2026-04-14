# Decision Severity — info / routine / risky / destructive

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — 給 PM 看

AI 丟出的每個決策都會帶一個風險標籤。標籤決定圖示、顏色、倒數條，
以及 MODE 是否自動執行它。**最該留意的是 destructive。**

| Severity | 圖示 | 顏色 | 可還原？ | 典型例子 |
|---|---|---|---|---|
| **info** | 資訊圓圈 | 中性 | 可 | 「我讀了 12 個檔案來回答」 |
| **routine** | 資訊圓圈 | 中性 | 可 | 為此 task 選用哪個模型 |
| **risky** | 警告三角 | 琥珀 | 可救回 | 中途切換 agent 的 LLM provider |
| **destructive** | 警告八角 | 紅 | **不可** | push production、刪工作區、ship release |

## AI 如何挑嚴重度

在決策被提出的當下決定。兩個來源：

1. **硬編預設** — engine 知道例如 `deploy/*` 為 `destructive`、
   `switch_model` 為 `risky`。
2. **Decision Rules** — 操作員自訂覆寫。可以宣告「本隊 `deploy/staging`
   只算 `risky`」或「FULL AUTO 下 `git_push/experimental/*` 自動執行」。
   在 Decision Rules panel 設定。

## UI 提示

在 **Decision Queue** panel 與右上 **Toast** 中：

- **Destructive** — 紅色 AlertOctagon 圖示、紅邊框、紅色倒數條；
  點 APPROVE / REJECT 時會跳瀏覽器 `confirm()` 對話框（B10 雙重確認）。
- **Risky** — 琥珀 AlertTriangle、琥珀邊框、有倒數但無 confirm。
- **Routine / info** — 藍色 Info 圖示，除非設了 `timeout_s` 否則無倒數。

pending 決策剩餘時間 < 10 秒時，倒數條在 panel 與 toast 兩處都會
**變紅並脈動**，讓您在遠距也能注意。

## 逾時行為

pending 決策逾時未處理時：

- 自動解析為 `default_option_id`（通常是安全選項）
- `resolver` 欄位記為 `"timeout"`
- 發出 `decision_resolved` SSE 並移至 history
- 30 秒 sweep 迴圈處理；您也可在 Decision Queue header 手動按
  **SWEEP** 觸發

sweep 間隔可由 `OMNISIGHT_DECISION_SWEEP_INTERVAL_S` 覆寫（預設 10）。

## Destructive 雙重確認 — B10 保護

審計項 B10 新增。對 destructive 決策按 APPROVE 或 REJECT 會跳出
瀏覽器 confirm 對話框，顯示標題與所選選項。意義：

- 不能因誤觸鍵盤 `A` 就放行「push prod」。
- Reject 也要確認，因為拒絕 destructive deploy 可能留下半合併分支。

想繞過（如 E2E 腳本），請直接呼叫 backend API 而非走 UI。

## 速率限制

Decision mutator 端點（`/approve`、`/reject`、`/undo`、`/sweep`、
`/operation-mode`、`/budget-strategy`）有滑動視窗速率限制 — 預設
每個客戶端 IP 每 10 秒 30 次。用 `OMNISIGHT_DECISION_RL_WINDOW_S`
與 `OMNISIGHT_DECISION_RL_MAX` 調整。

## 內部實作

- Enum：`backend/decision_engine.py · DecisionSeverity`
- 自動執行矩陣：`should_auto_execute(severity, mode)`
- Destructive confirm：`components/omnisight/decision-dashboard.tsx ·
  doApprove / doReject`
- 速率限制：`backend/routers/decisions.py · _rate_limit()`

## 延伸閱讀

- [Operation Modes](operation-modes.md) — severity × mode 如何決定
  自動 vs 排隊
- [Panels Overview](panels-overview.md) — 去哪看 pending / history
