# Panels Overview — 畫面上每塊 tile 的職責

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

Dashboard 共 12 個 panel。桌機版鋪平展開；手機 / 平板版透過底部
nav bar 橫向切換。以下是各 panel 的一句話職責，需要深入處附連結。

## 頂部列（永遠可見）

| 元素 | 職責 | 深入文件 |
|---|---|---|
| **MODE** pill | 全域自治等級 — AI 不問您能做多少 | [operation-modes.md](operation-modes.md) |
| **Sync count** | 本次工作階段全域 Singularity Sync 觸發次數 | — |
| **Provider health** | 目前能連到的 LLM provider | — |
| **Emergency Stop** | 立即停止所有 agent 與 pending invocation | — |
| **Notifications** 鈴鐺 | 未讀 L1-L4 通知（Slack / Jira / PagerDuty / 站內） | — |
| **Settings** 齒輪 | Provider 金鑰、整合、各 agent 模型覆寫 | — |
| **Language** 地球 | 切 UI 語言（文件連結也會同步切） | — |

## 主要 panel

| Panel | URL 參數 | 對象 | 一句話職責 |
|---|---|---|---|
| **Host & Device** | `?panel=host` | 工程師 | 目前驅動的 WSL2/Linux 主機與外接相機 / 開發板 |
| **Spec** | `?panel=spec` | PM + 工程師 | agent 建構依據的 `hardware_manifest.yaml` |
| **Agent Matrix** | `?panel=agents` | 兩者 | 8 個 agent 的即時狀態 / thought chain / 進度 |
| **Orchestrator AI** | `?panel=orchestrator` | 兩者 | 與 supervisor agent 對話；slash 指令在此 |
| **Task Backlog** | `?panel=tasks` | PM | 類 sprint 的 task 列表，拖曳重指派，依優先級排序 |
| **Source Control** | `?panel=source` | 工程師 | 各 agent 隔離的 workspace、branch、commit 數、repo URL |
| **NPI Lifecycle** | `?panel=npi` | PM | Concept → Sample → Pilot → MP 各階段與日期 |
| **Vitals & Artifacts** | `?panel=vitals` | 兩者 | Build log、模擬結果、可下載的 firmware artifact |
| **Decision Queue** | `?panel=decisions` | 兩者 | Pending 決策等您 approve/reject + history | ⭐ |
| **Budget Strategy** | `?panel=budget` | PM | 4 策略卡片 × 5 tuning knob 做 token / 成本控制 |
| **Pipeline Timeline** | `?panel=timeline` | 兩者 | 各 phase 的水平 timeline、目前進度標記、ETA |
| **Decision Rules** | `?panel=rules` | 兩者 | 操作員自訂規則覆寫 severity/mode 預設 |

## 深鏈快速參考

URL 參數可跨重整分享給同事。

```
/?panel=decisions                     ← 開 Decision Queue
/?decision=dec-abc123                 ← 開 Queue 並滾到該筆決策
/?panel=timeline&decision=dec-abc123  ← Timeline 可見，決策仍排隊
```

`?panel=` 值非法時會落回 Orchestrator panel（不會崩潰）。

## 手機 / 平板導航

螢幕寬度 < `lg` breakpoint（1024 px）時：

- 12 panel 收為單欄捲動
- **底部 nav bar** 顯示：← 上一個 panel、中間 pill（點開完整選單）、
  → 下一個 panel、加一排對應各 panel 的點
- 點的觸控目標為 44 × 44 px（視覺仍是 8 px），符合 WCAG 2.5.5

## 鍵盤快速鍵（Decision Queue / Toast 內）

- **A** — 批准目前 focus / 最新決策採用預設選項
- **R** — 拒絕 focus 的決策
- **Esc** — 關掉當前 toast 不動作
- **← / →** 或 **Home / End** — 於 PENDING / HISTORY tab 間切換

## 內部實作

- Panel 註冊：`app/page.tsx · VALID_PANELS` 與 `readPanelFromUrl()`
- URL 同步：`app/page.tsx` 的 `useEffect` 透過 `history.replaceState`
  把 `activePanel` 綁到 `?panel=`
- 手機導航：`components/omnisight/mobile-nav.tsx`

## 延伸閱讀

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md)
- [Glossary](glossary.md) — 不確定 NPI / Singularity Sync 意思？
