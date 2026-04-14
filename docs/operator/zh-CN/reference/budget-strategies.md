# Budget Strategies — Budget Strategy panel 的 4 张卡片

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — 给 PM 看

Budget Strategy 决定 **每次 agent 调用允许多贵**。四种预设对应不同场景；
暂不支持自定义（如有需求请开 issue）。

| 策略 | 什么时候用 | 一句话的成本 / 质量权衡 |
|---|---|---|
| **QUALITY** | 关键 release、安全认证固件 | 顶级模型、3 次重试、不自动降级 — 最准确、最花钱 |
| **BALANCED** | 日常开发默认 | 默认阶模型、2 次重试、90 % 日用量时降级 |
| **COST_SAVER** | 探索性工作、side project、实验 | 低价阶模型、1 次重试、70 % 即降级 |
| **SPRINT** | Demo 冲刺、死线推进 | 默认阶、2 次重试、偏好并行执行 |

切换即时，通过 `budget_strategy_changed` SSE 同步到每个已连接浏览器。

## 5 个 tuning knob

每个策略都是 5 个 knob 的冻结组合。Budget Strategy panel 底部条可实时读出。

| Knob | 范围 | 作用 |
|---|---|---|
| **TIER** | `premium` / `default` / `budget` | provider 链默认用哪一阶模型。`premium` = provider 最强；`budget` = 最便宜。provider 配置对应各阶具体模型。 |
| **RETRIES** | 0 – 5 | 遇暂时性 LLM 错误（rate limit / 5xx）后本次尝试放弃前重试几次。 |
| **DOWNGRADE** | 0 – 100 % | 当日 token 预算用到多少 % 时自动降阶到便宜模型。 |
| **FREEZE** | 0 – 100 % | 达到此阈值所有非关键 LLM 调用冻结，后续 agent 工作需操作员明确批准。 |
| **PARALLEL** | YES / NO | orchestrator 是否积极并行独立 agent（SPRINT 为 YES）。 |

`DOWNGRADE < FREEZE` — FREEZE 为更严格停机。两者都 100 % 时都不触发。

## 4 种策略详细

### QUALITY
- TIER=premium · RETRIES=3 · DOWNGRADE=100 % · FREEZE=100 % · PARALLEL=NO
- **适合**：出货给付费客户、安全审查、最终固件 build。
- **不适合**：快速迭代 — 单 task 成本最高且 premium 模型通常较慢。

### BALANCED（默认）
- TIER=default · RETRIES=2 · DOWNGRADE=90 % · FREEZE=100 % · PARALLEL=NO
- **适合**：日常工作。质量与成本的最佳平衡点；烧到 90 % 日预算后
  会悄悄掉到 budget 阶撑到当日结束。
- **不适合**：release 关键期不希望掉入降级区造成质量回退时。

### COST_SAVER
- TIER=budget · RETRIES=1 · DOWNGRADE=70 % · FREEZE=95 % · PARALLEL=NO
- **适合**：探索性 coding、side project、手动 QA 脚本。
- **不适合**：任何面向客户的工作。budget 阶模型漏掉 premium 能抓的
  边界情况，且只有 1 次重试意味着暂时性失败会以硬错误直接浮出。

### SPRINT
- TIER=default · RETRIES=2 · DOWNGRADE=95 % · FREEZE=100 % · PARALLEL=YES
- **适合**：死线冲刺、demo 准备、并行重构批量。`prefer_parallel=YES`
  让调度器饱和 MODE 并行上限（FULL AUTO = 4 个并行 agent、TURBO = 8 个）。
- **不适合**：低并行度且需严格排序的 task — 调度器可能在 parent
  task 未声明依赖时先跑 child。

## 与 MODE 的交互

Budget Strategy 与 Operation Mode 是正交关系：

- MODE 决定 **谁批准**（您 vs AI）
- Budget Strategy 决定 AI 决策 **多贵**

常见组合：

| MODE × 策略 | 什么时候合理 |
|---|---|
| SUPERVISED × BALANCED | 日常默认 — AI 跑常规、您批 risky、默认模型 |
| TURBO × SPRINT | 周末批量重构 — 最大并行度、最大自主 |
| MANUAL × QUALITY | 最终 release 审查 — 人参与每个 loop、premium 模型 |
| FULL AUTO × COST_SAVER | 探索性 prototype — AI 推进、便宜模型 |

## Token 预算交互

DOWNGRADE 与 FREEZE 阈值对应每日 LLM token 预算
（由 `OMNISIGHT_LLM_TOKEN_BUDGET_DAILY` 设定）。`token_warning` SSE
于 80 / 90 / 100 % 触发；Budget Strategy tuning 决定是否触发自动降级。

## 谁能切换策略

与 mode 相同，PUT `/api/v1/budget-strategy` 若 `OMNISIGHT_DECISION_BEARER`
有设则需 bearer token；速率限制为每客户端 IP 每 10 秒 30 次。

## 内部实现

- 后端：`backend/budget_strategy.py` · `_TUNINGS` 即上表 4 行冻结 dict。
  `set_strategy()` 发 `budget_strategy_changed`。
- 前端：`components/omnisight/budget-strategy-panel.tsx` · 4 张卡片 +
  5 个 knob cell（TuningCell）+ SSE 同步。
- 事件：`SSEBudgetStrategyChanged` 于 `backend/sse_schemas.py`。

## 延伸阅读

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md) — severity 标签与 budget 无关
- [Troubleshooting](../troubleshooting.md) — panel 显示红色 error banner 时
