# Tutorial · 處理一個決策（8 分鐘）

> **source_en:** 2026-04-14 · authoritative

[第一次 Invoke](first-invoke.md) 的延伸。這裡看一個決策的完整流程：
哪裡會出現、如何決定、按錯了怎麼還原。

## 1 · 強制產生一個決策

開 **Orchestrator AI** 輸入：

```
/invoke 把 workspace 變更 push 到 origin/main
```

SUPERVISED mode 下會提案 **destructive** 決策（push 到 `main` 不可逆，
除非 force-push + 碰運氣）。

## 2 · 您會看到

三個同步介面呈現同一個決策：

- **Toast**（右上）— 紅邊框、AlertOctagon 圖示，剩餘 < 10 秒時倒數閃紅。
- **Decision Queue** panel — 項目出現在最上。Pending count 徽章 +1，
  每列有倒數欄。
- **SSE log**（REPORTER VORTEX）— 一行 `[DECISION] dec-… kind=push
  severity=destructive`。

預設 timeout 60 秒。可在 propose 時調，由 sweep loop 監控
（見 `OMNISIGHT_DECISION_SWEEP_INTERVAL_S`）。

## 3 · 決定

三種路徑：

### Approve
點 APPROVE。因 severity 是 `destructive`，會跳 `window.confirm()`
對話框（「Approve DESTRUCTIVE decision?」）。這是 B10 保護 — 不能
靠鍵盤 `A` 誤觸就放行 prod push。

確認 → agent 繼續，決策移至 HISTORY，toast 消失。

### Reject
點 REJECT。destructive 同樣會跳 confirm。確認 → agent 停手。決策
以 `resolver=user, chosen_option_id=__rejected__` 進 HISTORY。

### Timeout
什麼都不做。倒數到 0 時 sweep loop 自動解析為 `default_option_id`
（destructive 通常是安全選項）。紀錄 `resolver=timeout`。

## 4 · Undo

開 Decision Queue，切到 **HISTORY** tab（點 HISTORY 或從 PENDING
按 → 方向鍵）。找到剛剛的決策。點 **UNDO**。

undo **不會做的事**：不會反轉真實世界效應（git push 已經打出去了）。
它只是把決策狀態翻為 `undone` 並發 `decision_undone` SSE，讓您的
紀錄系統知道操作員改了主意。

把 `undone` 當成「審計 log：操作員後悔了」，而非「系統幫我 revert」。
真正 revert 需要您手動做補償動作（例如用先前 commit `git push -f`）。

## 5 · 觀察 SSE round-trip

同一個 dashboard 另開一個瀏覽器分頁。所有事件都即時同步 — Decision
Queue、toast、mode pill — 全經 SSE `/api/v1/events`。

關掉一個分頁。另一個照跑。這是 Phase 48-Fix 加入的共享 SSE manager：
每個瀏覽器一個 EventSource，所有 panel 共用。

## 6 · 定一條 Rule 下次免問

若您「永遠」想自動批准對某特定 branch pattern 的 push，開 **Decision
Rules** panel：

```
kind_pattern: push/experimental/**
auto_in_modes: [supervised, full_auto, turbo]
severity: risky          # 從 destructive 降級
default_option_id: go
```

儲存。下次匹配的決策會在列出的 mode 自動執行。規則持久化至 SQLite
（Phase 50-Fix A1），重啟後仍在。

## 相關

- [Decision Severity](../reference/decision-severity.md) — 為何
  destructive 會跳 confirm 而 risky 不會。
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  自動執行矩陣。
- [Troubleshooting](../troubleshooting.md) — `[AUTH]` /
  `[RATE LIMITED]` banner 與「按鈕好像沒反應」類問題。
