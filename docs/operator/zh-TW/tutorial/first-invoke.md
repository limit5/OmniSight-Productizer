# Tutorial · 第一次 Invoke（10 分鐘）

> **source_en:** 2026-04-14 · authoritative

這份教學帶您從剛啟動的 dashboard 走到第一次 **Singularity Sync / Invoke** —
那是請 orchestrator「盤點系統、決定下一步、執行」的全域動作。走完您會認得
AI 點亮的每個元素，也知道要介入時該點哪裡。

## 開始前

- 後端在 `http://localhost:8000`（或 `BACKEND_URL` 指向的位址）。
  用 `curl http://localhost:8000/api/v1/health` 確認。
- 前端在 `http://localhost:3000`。
- `.env` 至少一個 LLM provider key，或沒 key（rule-based fallback 仍能跑，
  agent 只是回樣板回應）。

## 1 · 認識環境

打開 `http://localhost:3000`。新瀏覽器會自動啟動 **5 步首次導覽**（每張卡
底部有 Skip / Next）。導覽結束後 dashboard 就是您的。看一眼頂列：

- **MODE** pill — 預設 SUPERVISED。表示常規 AI 動作自動執行，有風險的會等您。
  [→ 詳情](../reference/operation-modes.md)
- **`?` 說明圖示**（MODE 旁邊）— 忘記什麼按鈕做什麼時隨時點。
- **Decision Queue**（右側 tile）— 目前是空的。AI 無法自動執行的決策會落在這。

## 2 · 挑最簡單的 task

開 **Orchestrator AI** panel（桌機版在中央，手機 swipe 過去）。輸入框輸入：

```
/invoke 列出目前連接的硬體裝置
```

按 Enter。

## 3 · 看 pipeline 點亮

一連串事情會接續發生，這是正常的：

1. 左側 **REPORTER VORTEX** log 串印出 `[INVOKE] singularity_sync: ...`。
2. **Agent Matrix** panel 有一個 agent 轉 `active`。thought-chain 一行一行更新。
3. 一到多個 **Tool progress** 事件顯示檔案讀取 / shell 呼叫。
4. 在 SUPERVISED mode 下，若 agent 提出 `risky` 或 `destructive` 的東西，
   右上會彈 **Toast**，且該項也會進入 **Decision Queue**。

此次的「只讀列表」invocation 應該不會產生決策 — AI 直接在對話中回答。

## 4 · 看答案

Orchestrator 在 panel 中回覆一則訊息，您應會看到連接裝置列表（若開發筆電
沒接相機，列表可能是空的 — 正常）。

## 5 · 試個較有風險的 invoke

```
/invoke 在當前 workspace 建立名為 tutorial-sandbox 的 git branch
```

這次在 SUPERVISED mode 下，您應該會看到 **Decision Queue** 出現 severity
`risky` 的項目。Toast 顯示 A / R / Esc 鍵盤提示與倒數。

- 按 **A**（或點 APPROVE）— AI 建立 branch。
- 按 **R** — AI 停手。
- 讓倒數跑完 — 解析為預設安全選項（通常是「停手」）。

若沒看到決策，可能是 agent 因規則或您把 MODE 切到 FULL_AUTO / TURBO
而自動執行了。查 Decision Queue panel 內的 `?` 看 severity 矩陣。

## 6 · 試 MANUAL mode

點 MODE pill → MANUAL。重跑建 branch 的 invoke。現在 *每一個* 步驟都進
Decision Queue，包括常規讀取。這是「我想先看 AI 要做什麼才讓它動」的
正確 mode。

探索完切回 SUPERVISED。

## 下一步

- [處理一個決策](handling-a-decision.md) — risky/destructive 決策的完整
  生命週期，含 undo。
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  矩陣細節。
- [Budget Strategies](../reference/budget-strategies.md) — 本教學期間
  token 花費令您擔心時。
- [Troubleshooting](../troubleshooting.md) — 某些元素沒照文字點亮時。
