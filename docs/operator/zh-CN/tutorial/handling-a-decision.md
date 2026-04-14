# Tutorial · 处理一个决策（8 分钟）

> **source_en:** 2026-04-14 · authoritative

[第一次 Invoke](first-invoke.md) 的延续。这里看一个决策的完整流程：
哪里会出现、如何决定、按错了怎么还原。

## 1 · 强制产生一个决策

开 **Orchestrator AI** 输入：

```
/invoke 把 workspace 变更 push 到 origin/main
```

SUPERVISED mode 下会提案 **destructive** 决策（push 到 `main` 不可逆，
除非 force-push + 碰运气）。

## 2 · 您会看到

三个同步界面呈现同一个决策：

- **Toast**（右上）— 红边框、AlertOctagon 图标，剩余 < 10 秒时倒计时闪红。
- **Decision Queue** panel — 项目出现在最上。Pending count 徽章 +1，
  每行有倒计时列。
- **SSE log**（REPORTER VORTEX）— 一行 `[DECISION] dec-… kind=push
  severity=destructive`。

默认 timeout 60 秒。可在 propose 时调，由 sweep loop 监控
（见 `OMNISIGHT_DECISION_SWEEP_INTERVAL_S`）。

## 3 · 决定

三种路径：

### Approve
点 APPROVE。因 severity 是 `destructive`，会弹 `window.confirm()`
对话框（"Approve DESTRUCTIVE decision?"）。这是 B10 保护 — 不能
靠键盘 `A` 误触就放行 prod push。

确认 → agent 继续，决策移至 HISTORY，toast 消失。

### Reject
点 REJECT。destructive 同样会弹 confirm。确认 → agent 停手。决策
以 `resolver=user, chosen_option_id=__rejected__` 进 HISTORY。

### Timeout
什么都不做。倒计时到 0 时 sweep loop 自动解析为 `default_option_id`
（destructive 通常是安全选项）。记录 `resolver=timeout`。

## 4 · Undo

开 Decision Queue，切到 **HISTORY** tab（点 HISTORY 或从 PENDING
按 → 方向键）。找到刚刚的决策。点 **UNDO**。

undo **不会做的事**：不会反转真实世界效应（git push 已经推出去了）。
它只是把决策状态翻为 `undone` 并发 `decision_undone` SSE，让您的
记录系统知道操作员改了主意。

把 `undone` 当成"审计 log：操作员后悔了"，而非"系统帮我 revert"。
真正 revert 需要您手动做补偿动作（例如用先前 commit `git push -f`）。

## 5 · 观察 SSE round-trip

同一个 dashboard 另开一个浏览器标签。所有事件都实时同步 — Decision
Queue、toast、mode pill — 全经 SSE `/api/v1/events`。

关掉一个标签。另一个照跑。这是 Phase 48-Fix 加入的共享 SSE manager：
每个浏览器一个 EventSource，所有 panel 共用。

## 6 · 定一条 Rule 下次免问

若您"永远"想自动批准对某特定 branch pattern 的 push，开 **Decision
Rules** panel：

```
kind_pattern: push/experimental/**
auto_in_modes: [supervised, full_auto, turbo]
severity: risky          # 从 destructive 降级
default_option_id: go
```

保存。下次匹配的决策会在列出的 mode 自动执行。规则持久化至 SQLite
（Phase 50-Fix A1），重启后仍在。

## 相关

- [Decision Severity](../reference/decision-severity.md) — 为何
  destructive 会弹 confirm 而 risky 不会。
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  自动执行矩阵。
- [Troubleshooting](../troubleshooting.md) — `[AUTH]` /
  `[RATE LIMITED]` banner 与"按钮好像没反应"类问题。
