# OmniSight 操作指南 — 繁體中文（台灣）

這份指南寫給實際使用 command center 的人，不是給打造它的人看的。
用白話回答「這個按鈕是幹嘛的？」「我的 task 怎麼停了？」之類的問題，
只在您想深入時才指向原始碼。

## 該從哪一段看起

| 如果您是…… | 從這裡開始 |
|---|---|
| 決定要不要 ship 的產品負責人 / release captain | [Operation Modes](reference/operation-modes.md) → [Decision Severity](reference/decision-severity.md) |
| 日常跑 pipeline 的嵌入式工程師 | [Panels Overview](reference/panels-overview.md) → [Glossary](reference/glossary.md) |
| 卡在某個畫面 | 點該 panel header 上的 `?` 圖示 |

## 快速地圖

- **Reference** (`reference/`) — 精確、完整、隨時更新
- **Glossary** — agent / task / pipeline / NPI 在本系統的具體定義
- **Troubleshooting**（即將推出） — 出紅色 banner 了怎麼辦
- **Tutorials**（即將推出） — 從零開始跑一次 Invoke 的手把手教學

## Agent 身分與升等系統（Priority RPG）

Agent 不再只是無狀態的 runner — 每個 agent 都有自己的角色卡
（Character Card），包含 Guild、level、XP、talent，以及每個 skill /
tool 的熟練度。日常操作 how-to（檢視 roster、選 Guild、看 XP）請見
[Agent RPG 系統操作指南](../../operations/agent-rpg-system.md)；
設計理由與背後的 schema lock-in 請見
[ADR-0008 — Agent RPG Class & Skill Leveling System](../../adr/0008-agent-rpg-class-skill-leveling.md)
（權威規格 — 本指南與 ADR 不一致時以 ADR 為準）。

## 多語版本

本文件同步維護英文（`en/`，權威源）、繁中（`zh-TW/`，本檔）、
簡中（`zh-CN/`）、日文（`ja/`）。英文為權威源，其餘為翻譯版。
翻譯若落後，每個檔案頂端的 `source_en:` 會標示對應的英文修訂日期。
