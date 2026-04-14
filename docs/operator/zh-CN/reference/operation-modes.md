# Operation Modes — 画面最上方的 MODE pill

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — 给 PM 看

MODE 决定 **AI 在不问您的情况下可以做到哪一步**。四个等级，从
"所有事都要问我"到"全部自己做，错了我再喊停"。图标颜色对应风险。

| Mode | 图标颜色 | 一句话含义 |
|---|---|---|
| **MANUAL** (MAN) | 青 (cyan) | 每一步都要您批准 |
| **SUPERVISED** (SUP) | 蓝 (blue) | 常规工作自动运行，有风险的停下等您 — **默认** |
| **FULL AUTO** (AUT) | 琥珀 (amber) | 只有破坏性工作停下等您 |
| **TURBO** (TRB) | 红 (red) | 全部自动含破坏性，您有 60 秒撤销窗口 |

切换后实时同步到每个已连接的浏览器（桌面、手机、平板都同步）。

## 与 Decision Severity 的交互

AI 想做的每件事都会被标上四种严重度之一（详见
[Decision Severity](decision-severity.md)）。MODE 的工作就是从下表
挑一行：

| Severity ↓ / Mode → | MANUAL | SUPERVISED | FULL AUTO | TURBO |
|---|---|---|---|---|
| `info`（纯读取 / 日志） | 排队 | 自动 | 自动 | 自动 |
| `routine`（常规写入） | 排队 | 自动 | 自动 | 自动 |
| `risky`（可还原写入） | 排队 | 排队 | 自动 | 自动 |
| `destructive`（ship / deploy / 删除） | 排队 | 排队 | 排队 | 自动（60 秒倒计时） |

"排队"表示该决策进入 **Decision Queue** panel，必须您批准后 AI
才会继续。

## 并行度预算

MODE 同时控制系统并行执行 agent 的数量。pill 旁会显示
`in_flight / cap`。

| Mode | 并行上限 |
|---|---|
| MANUAL | 1 |
| SUPERVISED | 2 |
| FULL AUTO | 4 |
| TURBO | 8 |

并行度越高吞吐越快但 token 消耗也越多。token 紧张时先调
**Budget Strategy** 再考虑升 MODE。

## 常见场景

- **下班离开 / 过夜** — 切 MANUAL，确保无意外决策。未决事项会累积，
  第二天一并处理。
- **日常开发** — SUPERVISED 最实用。AI 能推进常规工作（读文件、
  调用工具、分析），但任何不可逆动作前会停下。
- **Demo 冲刺** — FULL AUTO，只在破坏性 push 时停下问您。
- **周末批量重构** — TURBO 配合手机 toast 监控 60 秒倒计时；
  看到不对立即 Emergency Stop。

## 谁能改 MODE

后端 `.env` 若设了 `OMNISIGHT_DECISION_BEARER`，只有 API 调用端
带该 token 的才能切换 MODE（UI 从 localStorage 读 token）。未设时
此控制对所有能连到后端的网络地址开放 — 单人本地部署 OK，
多人共用不建议。

## 内部实现

- 前端：`components/omnisight/mode-selector.tsx` — 分段 pill + SSE
  订阅者让所有 tab 保持同步
- 后端：`backend/decision_engine.py` · `set_mode()` / `get_mode()` ·
  `should_auto_execute(severity)` 即上方对照表
- 事件：切换会在 SSE bus 发布 `mode_changed`；schema 可由
  `GET /api/v1/system/sse-schema` 获取
- 持久化：**目前不跨重启保留** — 重启后会回到 SUPERVISED 默认。
  将于未来 phase 处理。

## 延伸阅读

- [Decision Severity](decision-severity.md) — `risky` 和
  `destructive` 到底差在哪
- [Budget Strategies](budget-strategies.md) — MODE 旁边的 token
  成本调节器
- [Panels Overview](panels-overview.md) — 排队决策出现后要去哪看
