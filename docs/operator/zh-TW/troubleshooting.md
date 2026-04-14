# Troubleshooting — dashboard 告訴您出狀況時

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

依照操作員實際看到的現象整理。本頁沒涵蓋的情況請看 Orchestrator AI
panel 的 log 串（REPORTER VORTEX）與後端 stderr。

## Panel 出現紅色 banner

### `[AUTH] ...`
後端以 401 / 403 拒絕。

- **原因**：後端設了 `OMNISIGHT_DECISION_BEARER` 但前端 token 錯誤或缺失。
- **處理**：開 Settings → provider 分頁重新輸入 bearer；或單機部署時
  於 `.env` 取消 `OMNISIGHT_DECISION_BEARER`。

### `[RATE LIMITED] ...`
滑動視窗節流觸發（預設每客戶端 IP 每 10 秒 30 次）。

- **原因**：腳本輪詢或 UI 失控重試。
- **處理**：等 banner 自動消失（10 秒），或用 `OMNISIGHT_DECISION_RL_MAX`
  / `_WINDOW_S` 放寬上限，詳見 `.env.example`。

### `[NOT FOUND] ...`
端點回 404。

- **原因**：前端呼叫後端已移除或更名的端點。通常是部分部署後版本不一致。
- **處理**：硬重整頁面。若持續，前後端版本不同 — 兩邊都重啟。

### `[BACKEND DOWN] ...`
後端回 5xx。

- **原因**：uvicorn 沒跑、或 router 中未處理的例外。檢查
  `/tmp/omni-backend.log`（dev）或服務 log（prod）。
- **處理**：重啟後端。若開機即掛，前景跑 `python3 -m uvicorn backend.main:app`
  看 stack。

### `[NETWORK] ...`
fetch 在抵達後端前就失敗。

- **原因**：後端程序死了、port 錯、或 proxy / VPN 斷線。
- **處理**：`curl http://127.0.0.1:8000/api/v1/health`。若有回應，前端
  `NEXT_PUBLIC_API_URL` 或 rewrite 設定錯誤。若無回應，啟動後端。

## Decision Queue 看起來卡住

### Pending 決策按下 approve / reject 後沒消失
- **原因 1**：後端回 409 — 該決策已被其他分頁解決。UI 會在下個 SSE
  事件對齊；按 panel header 的 **RETRY** 強制。
- **原因 2**：destructive severity 的 `window.confirm()` 對話框仍開在
  隱藏分頁。檢查所有 dashboard 分頁。

### 決策每次都還沒按就 timeout
- propose 時預設 `timeout_s` 為 60。若 producer 設了更短的 deadline
  而您來不及反應，sweep loop 會解析為 default 安全選項。這是預期行為。
- 若要更多時間：切到 MANUAL mode（不設 deadline，決策會無限期保留 —
  確認方式是查看 decision payload 的 `deadline_at`）。

### SWEEP 按了沒反應
- 只會解析 deadline **已過** 的決策。若全部都還在時間視窗內，0 筆被解析
  並出暫時訊息告知。

## Toast 問題

### 「+N MORE PENDING」徽章不消失
- 關掉所有可見 toast（逐個按 Esc 或點 ✕）。overflow 計數只在堆疊歸 0 時重置。
- 若仍持續，後端發 `decision_pending` 的速度比您處理快。調低 MODE
  （SUPERVISED 或 MANUAL）避免常規決策自動執行後產生新的 risky/destructive
  後續。

### 倒數卡在 100 %
- 後端與瀏覽器時鐘偏移。兩邊 `date -u` 比對。
- 後端時鐘比瀏覽器早時，進度條會滿值停留直到真實 deadline 過後瞬跳 0。

### 倒數顯示 NaN 或奇怪數值
- 後端送了格式錯誤的 `deadline_at`。審計項 B2 新增的驗證應已強制型別，
  若仍看到：硬重整（js 快取）；持續則開 issue 附上原始 SSE payload。

## Agent 問題

### Agent 卡在 "working" 超過 30 分鐘
- watchdog 30 分鐘後觸發，會提案 stuck 補救決策（switch model /
  spawn alternate / escalate）。查 Decision Queue。
- 60 秒內都沒東西出現代表 watchdog 認為該 agent 有活躍心跳。用
  **Emergency Stop** → Resume 強制重置。

### Agent 反覆卡同一個錯
- 每個 agent 的 error ring buffer（10 筆）由 node graph 餵。
  視窗內第 3 次相同錯時 stuck detector 於 FULL AUTO / TURBO 自動提案
  `switch_model` 補救；較低 mode 則排入佇列等批准。
- 若從未走到這步，該錯可能未以 tool error 形式浮出 — 查 REPORTER VORTEX。

### Provider health 顯示紅但我的 key 沒問題
- Provider health = 最近 3 次 probe ping。額度用盡算健康失敗。查看
  provider dashboard。
- key 有效的話 keyring 可能載入舊版。Settings → Provider Keys → 重新儲存。

## 手機 / 平板問題

### 手機上有些 panel 點不到
- 底部 nav 點列對應 12 個 panel。若看到少於 12 個，表示跑的是 Phase 50D
  前的 build。硬重整。
- swipe prev/next 按鈕照順序循環。

### 深鏈開到錯的 panel
- `?panel=` 優先於 `?decision=`。拿掉 `?panel=` 組件，或確保深鏈決策 id
  時設為 `?panel=decisions`。

## 真的卡住了

- `curl http://localhost:8000/api/v1/system/sse-schema | jq` — 確認後端
  有回應且發送前端預期的事件類型。
- `pytest backend/tests/test_decision_engine.py` — 決策引擎的 27 個
  測試 < 1 秒完成，可抓到大部分後端回歸。
- 開 issue 附上：後端 commit hash（`git rev-parse HEAD`）、紅色
  banner 文字、REPORTER VORTEX 最後 50 行。

## 相關

- [Operation Modes](reference/operation-modes.md)
- [Decision Severity](reference/decision-severity.md)
- [Glossary](reference/glossary.md)
