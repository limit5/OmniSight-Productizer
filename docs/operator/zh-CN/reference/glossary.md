# Glossary 术语表

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

UI 与 log 使用的专有名词。按英文字母序。

**Agent** — 专职 LLM worker。默认八种类型（firmware、software、
validator、reporter、reviewer、general、custom、devops），各有
`sub_type` 对应 `configs/roles/*.yaml` 的角色文件。每个 agent 有
独立 git 工作区。

**Artifact** — pipeline 产出值得保留的任何文件：编译后 firmware
映像、模拟报告、release bundle。置于 `.artifacts/`，在
Vitals & Artifacts panel 呈现。

**Budget Strategy** — 五个 tuning knob 的具名组合（model tier、
max retries、downgrade threshold、freeze threshold、prefer parallel），
用来控制每次 agent 调用的成本。默认四策略：`quality`、`balanced`、
`cost_saver`、`sprint`。

**Decision** — AI 停下来决定要自行动作、问您、或 timeout fallback
的任何时刻。带 severity（`info` / `routine` / `risky` / `destructive`）
与选项列表。

**Decision Queue** — Pending 决策列表（panel 名称与内存 list 同名）。
最新在上。

**Decision Rule** — 操作员自定义覆盖规则，匹配 `kind` glob（如
`deploy/staging/*`）并指定 severity、默认选项、或自动执行模式。
规则持久化至 SQLite（Phase 50-Fix A1）。

**Emergency Stop** — 停止所有执行中 agent 与 pending invocation。
释放 concurrency slot，发 `pipeline_halted`。按 Resume 恢复。

**Invoke** — "全局同步"动作，让 orchestrator 盘点现状并决定下一步。
也可带自由指令（`/invoke fix the build`）。

**LangGraph** — 底层 agent graph 框架。日常用不到，但 log 中的
"graph state"、"reducer"即 LangGraph 语义。

**L1 / L2 / L3 memory** — 分层 agent 记忆。L1 = `CLAUDE.md` 不变
核心规则。L2 = 各 agent 角色 + 近期对话。L3 = episodic（可搜过往
事件，通过 FTS5）。

**MODE** — 全局自治等级，详见 [operation-modes.md](operation-modes.md)。

**NPI** — New Product Introduction，硬件出货周期：
Concept → Sample → Pilot → Mass Production。每阶段有自己的 pipeline。

**Operation Mode** — MODE 的正式名。四值：manual、supervised、
full_auto、turbo。

**Pipeline** — 将 task 从"idea"推到"shipped"的有序步骤。
步骤组成 phase。Pipeline Timeline panel 可视化当前执行。

**REPORTER VORTEX** — 左侧滚动 log 显示系统每个动作。每个
`emit_*()` 事件都写到这里。

**SSE** (Server-Sent Events) — 后端单向推送实时更新到所有浏览器
的通道。端点 `/api/v1/events`。Schema 在 `/api/v1/system/sse-schema`。

**Singularity Sync** — Invoke 的营销名，同义词。

**Slash command** — Orchestrator AI panel 内以 `/` 开头的指令。
内置 `/invoke`、`/halt`、`/resume`、`/commit`、`/review-pr`，
加上 skill 系统定义的。

**Stuck detector** — 监测 agent 反复同错时提案补救决策（switch
model、spawn alternate、escalate）的 watchdog。每 60 秒执行。

**Sweep** — 周期性（默认 10 秒）将 deadline 已过的 pending 决策
超时处理。可在 Decision Queue header 手动触发。

**Task** — 工作单元。有指派 agent、优先级、状态、父子树、以及
可选的外部 issue 链接（GitHub、GitLab、Gerrit）。

**Token warning** — 每日 LLM token 预算达 80 % / 90 % / 100 % 时
发出 SSE 事件。90 % 触发自动降级到更便宜模型。

**Workspace** — 各 agent 工作的隔离 git clone。置于
`OMNISIGHT_WORKSPACE`（默认临时目录）。状态：`none | active |
finalized | cleaned`。

## 相关

- [Operation Modes](operation-modes.md)
- [Panels Overview](panels-overview.md)
- `backend/models.py` — 权威 enum 定义
