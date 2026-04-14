# Decision Severity — info / routine / risky / destructive

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — 给 PM 看

AI 抛出的每个决策都会带一个风险标签。标签决定图标、颜色、
倒计时条，以及 MODE 是否自动执行它。**最该留意的是 destructive。**

| Severity | 图标 | 颜色 | 可还原？ | 典型例子 |
|---|---|---|---|---|
| **info** | 信息圆圈 | 中性 | 可 | "我读了 12 个文件来回答" |
| **routine** | 信息圆圈 | 中性 | 可 | 为此 task 选用哪个模型 |
| **risky** | 警告三角 | 琥珀 | 可救回 | 中途切换 agent 的 LLM provider |
| **destructive** | 警告八角 | 红 | **不可** | push production、删工作区、ship release |

## AI 如何选严重度

在决策被提出的当下决定。两个来源：

1. **硬编默认** — engine 知道例如 `deploy/*` 为 `destructive`、
   `switch_model` 为 `risky`。
2. **Decision Rules** — 操作员自定义覆盖。可以声明"本队
   `deploy/staging` 只算 `risky`"或"FULL AUTO 下
   `git_push/experimental/*` 自动执行"。在 Decision Rules panel 设置。

## UI 提示

在 **Decision Queue** panel 与右上 **Toast** 中：

- **Destructive** — 红色 AlertOctagon 图标、红边框、红色倒计时条；
  点 APPROVE / REJECT 时会弹浏览器 `confirm()` 对话框（B10 双重确认）。
- **Risky** — 琥珀 AlertTriangle、琥珀边框、有倒计时但无 confirm。
- **Routine / info** — 蓝色 Info 图标，除非设了 `timeout_s` 否则无倒计时。

pending 决策剩余时间 < 10 秒时，倒计时条在 panel 与 toast 两处都会
**变红并脉动**，让您在远距也能注意。

## 超时行为

pending 决策超时未处理时：

- 自动解析为 `default_option_id`（通常是安全选项）
- `resolver` 字段记为 `"timeout"`
- 发出 `decision_resolved` SSE 并移至 history
- 30 秒 sweep 循环处理；您也可在 Decision Queue header 手动按
  **SWEEP** 触发

sweep 间隔可由 `OMNISIGHT_DECISION_SWEEP_INTERVAL_S` 覆盖（默认 10）。

## Destructive 双重确认 — B10 保护

审计项 B10 新增。对 destructive 决策按 APPROVE 或 REJECT 会弹出
浏览器 confirm 对话框，显示标题与所选选项。意义：

- 不会因误触键盘 `A` 就放行"push prod"。
- Reject 也要确认，因为拒绝 destructive deploy 可能留下半合并分支。

想绕过（如 E2E 脚本），请直接调用 backend API 而非走 UI。

## 速率限制

Decision mutator 端点（`/approve`、`/reject`、`/undo`、`/sweep`、
`/operation-mode`、`/budget-strategy`）有滑动窗口速率限制 — 默认
每个客户端 IP 每 10 秒 30 次。用 `OMNISIGHT_DECISION_RL_WINDOW_S`
与 `OMNISIGHT_DECISION_RL_MAX` 调整。

## 内部实现

- Enum：`backend/decision_engine.py · DecisionSeverity`
- 自动执行矩阵：`should_auto_execute(severity, mode)`
- Destructive confirm：`components/omnisight/decision-dashboard.tsx ·
  doApprove / doReject`
- 速率限制：`backend/routers/decisions.py · _rate_limit()`

## 延伸阅读

- [Operation Modes](operation-modes.md) — severity × mode 如何决定
  自动 vs 排队
- [Panels Overview](panels-overview.md) — 去哪看 pending / history
