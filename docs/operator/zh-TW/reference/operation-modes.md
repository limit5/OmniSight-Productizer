# Operation Modes — 畫面最上方的 MODE pill

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — 給 PM 看

MODE 決定 **AI 在不問您的情況下可以做到哪一步**。四個等級，從
「所有事都要問我」到「全部自己做，錯了我再喊停」。圖示顏色對應風險。

| Mode | 圖示顏色 | 一句話意義 |
|---|---|---|
| **MANUAL** (MAN) | 青 (cyan) | 每一步都要您批准 |
| **SUPERVISED** (SUP) | 藍 (blue) | 常規工作自動跑，有風險的停下等您 — **預設** |
| **FULL AUTO** (AUT) | 琥珀 (amber) | 只有破壞性工作停下等您 |
| **TURBO** (TRB) | 紅 (red) | 全部自動含破壞性，您有 60 秒撤銷視窗 |

切換後即時同步到每個已連線的瀏覽器（桌機、手機、平板皆同步）。

## 與 Decision Severity 的互動

AI 想做的每件事都會被標上四種嚴重度之一（詳見
[Decision Severity](decision-severity.md)）。MODE 的工作就是從下表
挑一列：

| Severity ↓ / Mode → | MANUAL | SUPERVISED | FULL AUTO | TURBO |
|---|---|---|---|---|
| `info`（純讀取 / 記錄） | 排隊 | 自動 | 自動 | 自動 |
| `routine`（常規寫入） | 排隊 | 自動 | 自動 | 自動 |
| `risky`（可還原寫入） | 排隊 | 排隊 | 自動 | 自動 |
| `destructive`（ship / deploy / 刪除） | 排隊 | 排隊 | 排隊 | 自動（60 秒倒數） |

「排隊」表示該決策進入 **Decision Queue** panel，必須您批准後 AI 才會繼續。

## 平行度預算

MODE 同時控制系統平行執行 agent 的數量。pill 旁會顯示
`in_flight / cap`。

| Mode | 平行上限 |
|---|---|
| MANUAL | 1 |
| SUPERVISED | 2 |
| FULL AUTO | 4 |
| TURBO | 8 |

平行度越高吞吐越快但 token 消耗也越多。token 吃緊時先調
**Budget Strategy** 再考慮升 MODE。

## 常見場景

- **下班離開 / 過夜** — 切 MANUAL，確保無意外決策。未決事項會累積，
  早上回來一併處理。
- **日常開發** — SUPERVISED 最實用。AI 能推進常規工作（讀檔、
  呼叫工具、分析），但任何不可逆動作前會停下。
- **Demo 衝刺** — FULL AUTO，只在破壞性 push 時停下問您。
- **週末批次重構** — TURBO 配合手機 toast 監控 60 秒倒數；
  看到不對勁立即 Emergency Stop。

## 誰能改 MODE

後端 `.env` 若有設 `OMNISIGHT_DECISION_BEARER`，只有在 API 呼叫端
帶該 token 的才能切換 MODE（UI 從 localStorage 讀 token）。未設時
此控制對所有能連到後端的網路位址開放 — 單人本機部署 OK，
多人共用不建議。

## 內部實作

- 前端：`components/omnisight/mode-selector.tsx` — 分段 pill + SSE
  訂閱者讓所有 tab 保持同步
- 後端：`backend/decision_engine.py` · `set_mode()` / `get_mode()` ·
  `should_auto_execute(severity)` 即上方對照表
- 事件：切換會在 SSE bus 發布 `mode_changed`；schema 可由
  `GET /api/v1/system/sse-schema` 取得
- 持久化：**目前不跨重啟保留** — 重啟後會回到 SUPERVISED 預設。
  將於未來 phase 處理。

## 延伸閱讀

- [Decision Severity](decision-severity.md) — `risky` 和 `destructive`
  到底差在哪
- [Budget Strategies](budget-strategies.md) — MODE 旁邊的 token
  成本調節器
- [Panels Overview](panels-overview.md) — 排隊決策出現後要去哪看
