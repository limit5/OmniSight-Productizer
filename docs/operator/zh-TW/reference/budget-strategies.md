# Budget Strategies — Budget Strategy panel 的 4 張卡片

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — 給 PM 看

Budget Strategy 決定 **每次 agent 呼叫允許多貴**。四種預設對應不同情境；
暫不支援自訂（若有需求請開 issue）。

| 策略 | 什麼時候用 | 一句話的成本 / 品質取捨 |
|---|---|---|
| **QUALITY** | 關鍵 release、安全認證韌體 | 頂級模型、3 次重試、不自動降級 — 最準確、最花錢 |
| **BALANCED** | 日常開發預設 | 預設階模型、2 次重試、90 % 日用量時降級 |
| **COST_SAVER** | 探索性工作、side project、實驗 | 低價階模型、1 次重試、70 % 即降級 |
| **SPRINT** | Demo 衝刺、死線推進 | 預設階、2 次重試、偏好平行執行 |

切換即時，經由 `budget_strategy_changed` SSE 同步到每個已連線瀏覽器。

## 5 個 tuning knob

每個策略都是 5 個 knob 的凍結組合。Budget Strategy panel 底部條可即時讀出。

| Knob | 範圍 | 作用 |
|---|---|---|
| **TIER** | `premium` / `default` / `budget` | provider 鏈預設用哪一階模型。`premium` = provider 最強；`budget` = 最便宜。provider 設定檔對應各階具體模型。 |
| **RETRIES** | 0 – 5 | 遇到暫時性 LLM 錯誤（rate limit / 5xx）後本次嘗試放棄前重試幾次。 |
| **DOWNGRADE** | 0 – 100 % | 當天 token 預算用到多少 % 時自動降階到便宜模型。 |
| **FREEZE** | 0 – 100 % | 達到此門檻所有非關鍵 LLM 呼叫凍結，後續 agent 工作需操作員明確批准。 |
| **PARALLEL** | YES / NO | orchestrator 是否積極平行化獨立 agent（SPRINT 為 YES）。 |

`DOWNGRADE < FREEZE` — FREEZE 為更嚴格停機。兩者都 100 % 時都不觸發。

## 4 種策略詳細

### QUALITY
- TIER=premium · RETRIES=3 · DOWNGRADE=100 % · FREEZE=100 % · PARALLEL=NO
- **適合**：出貨給付費客戶、安全審查、最終韌體 build。
- **不適合**：快速疊代 — 單 task 成本最高且 premium 模型通常較慢。

### BALANCED（預設）
- TIER=default · RETRIES=2 · DOWNGRADE=90 % · FREEZE=100 % · PARALLEL=NO
- **適合**：日常工作。品質與成本的最佳平衡點；燒到 90 % 日預算後
  會悄悄掉到 budget 階撐到當日結束。
- **不適合**：release 關鍵期不希望掉入降級區造成品質回退時。

### COST_SAVER
- TIER=budget · RETRIES=1 · DOWNGRADE=70 % · FREEZE=95 % · PARALLEL=NO
- **適合**：探索性 coding、side project、手動 QA 腳本。
- **不適合**：任何面向客戶的工作。budget 階模型漏掉 premium 能抓的
  邊界情況，且只有 1 次重試代表暫時性失敗會以硬錯誤直接浮出。

### SPRINT
- TIER=default · RETRIES=2 · DOWNGRADE=95 % · FREEZE=100 % · PARALLEL=YES
- **適合**：死線衝刺、demo 準備、平行重構批次。`prefer_parallel=YES`
  讓排程器飽和 MODE 平行上限（FULL AUTO = 4 個並行 agent、TURBO = 8 個）。
- **不適合**：低平行度且需嚴格排序的 task — 排程器可能在 parent
  task 未宣告依賴時先跑 child。

## 與 MODE 的互動

Budget Strategy 與 Operation Mode 是正交關係：

- MODE 決定 **誰批准**（您 vs AI）
- Budget Strategy 決定 AI 決策 **多貴**

常見組合：

| MODE × 策略 | 什麼時候合理 |
|---|---|
| SUPERVISED × BALANCED | 日常預設 — AI 跑常規、您批 risky、預設模型 |
| TURBO × SPRINT | 週末批次重構 — 最大平行度、最大自主 |
| MANUAL × QUALITY | 最終 release 審查 — 人參與每個 loop、premium 模型 |
| FULL AUTO × COST_SAVER | 探索性 prototype — AI 推進、便宜模型 |

## Token 預算互動

DOWNGRADE 與 FREEZE 門檻對應每日 LLM token 預算
（由 `OMNISIGHT_LLM_TOKEN_BUDGET_DAILY` 設定）。`token_warning` SSE
於 80 / 90 / 100 % 觸發；Budget Strategy tuning 決定是否觸發自動降級。

## 誰能切換策略

與 mode 相同，PUT `/api/v1/budget-strategy` 若 `OMNISIGHT_DECISION_BEARER`
有設則需 bearer token；速率限制為每客戶端 IP 每 10 秒 30 次。

## 內部實作

- 後端：`backend/budget_strategy.py` · `_TUNINGS` 即上表 4 列凍結 dict。
  `set_strategy()` 發 `budget_strategy_changed`。
- 前端：`components/omnisight/budget-strategy-panel.tsx` · 4 張卡片 +
  5 個 knob cell（TuningCell）+ SSE 同步。
- 事件：`SSEBudgetStrategyChanged` 於 `backend/sse_schemas.py`。

## 延伸閱讀

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md) — severity 標籤與 budget 無關
- [Troubleshooting](../troubleshooting.md) — panel 顯示紅色 error banner 時
