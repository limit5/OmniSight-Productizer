# Panels Overview — 画面上每块 tile 的职责

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

Dashboard 共 12 个 panel。桌面版平铺展开；手机 / 平板版通过底部
nav bar 横向切换。以下是各 panel 的一句话职责，需深入时附链接。

## 顶部栏（始终可见）

| 元素 | 职责 | 深入文档 |
|---|---|---|
| **MODE** pill | 全局自治等级 — AI 不问您能做多少 | [operation-modes.md](operation-modes.md) |
| **Sync count** | 本次会话全局 Singularity Sync 触发次数 | — |
| **Provider health** | 当前能连到的 LLM provider | — |
| **Emergency Stop** | 立即停止所有 agent 与 pending invocation | — |
| **Notifications** 铃铛 | 未读 L1-L4 通知（Slack / Jira / PagerDuty / 站内） | — |
| **Settings** 齿轮 | Provider 密钥、集成、各 agent 模型覆盖 | — |
| **Language** 地球 | 切 UI 语言（文档链接也会同步切） | — |

## 主要 panel

| Panel | URL 参数 | 对象 | 一句话职责 |
|---|---|---|---|
| **Host & Device** | `?panel=host` | 工程师 | 当前驱动的 WSL2/Linux 主机与外接相机 / 开发板 |
| **Spec** | `?panel=spec` | PM + 工程师 | agent 构建依据的 `hardware_manifest.yaml` |
| **Agent Matrix** | `?panel=agents` | 两者 | 8 个 agent 的实时状态 / thought chain / 进度 |
| **Orchestrator AI** | `?panel=orchestrator` | 两者 | 与 supervisor agent 对话；slash 指令在此 |
| **Task Backlog** | `?panel=tasks` | PM | 类 sprint 的 task 列表，拖拽重分配，按优先级排序 |
| **Source Control** | `?panel=source` | 工程师 | 各 agent 隔离的 workspace、branch、commit 数、repo URL |
| **NPI Lifecycle** | `?panel=npi` | PM | Concept → Sample → Pilot → MP 各阶段与日期 |
| **Vitals & Artifacts** | `?panel=vitals` | 两者 | Build log、模拟结果、可下载的 firmware artifact |
| **Decision Queue** | `?panel=decisions` | 两者 | Pending 决策等您 approve/reject + history | ⭐ |
| **Budget Strategy** | `?panel=budget` | PM | 4 策略卡片 × 5 tuning knob 做 token / 成本控制 |
| **Pipeline Timeline** | `?panel=timeline` | 两者 | 各 phase 的水平 timeline、当前进度标记、ETA |
| **Decision Rules** | `?panel=rules` | 两者 | 操作员自定义规则覆盖 severity/mode 默认值 |

## 深链快速参考

URL 参数可跨刷新分享给同事。

```
/?panel=decisions                     ← 开 Decision Queue
/?decision=dec-abc123                 ← 开 Queue 并滚到该决策
/?panel=timeline&decision=dec-abc123  ← Timeline 可见，决策仍排队
```

`?panel=` 值非法时会落回 Orchestrator panel（不会崩溃）。

## 手机 / 平板导航

屏幕宽度 < `lg` breakpoint（1024 px）时：

- 12 panel 收为单列滚动
- **底部 nav bar** 显示：← 上一个 panel、中间 pill（点开完整菜单）、
  → 下一个 panel、加一排对应各 panel 的点
- 点的触控目标为 44 × 44 px（视觉仍是 8 px），符合 WCAG 2.5.5

## 键盘快捷键（Decision Queue / Toast 内）

- **A** — 批准当前 focus / 最新决策采用默认选项
- **R** — 拒绝 focus 的决策
- **Esc** — 关掉当前 toast 不执行
- **← / →** 或 **Home / End** — 在 PENDING / HISTORY tab 间切换

## 内部实现

- Panel 注册：`app/page.tsx · VALID_PANELS` 与 `readPanelFromUrl()`
- URL 同步：`app/page.tsx` 的 `useEffect` 通过 `history.replaceState`
  把 `activePanel` 绑到 `?panel=`
- 手机导航：`components/omnisight/mobile-nav.tsx`

## 延伸阅读

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md)
- [Glossary](glossary.md) — 不确定 NPI / Singularity Sync 意思？
