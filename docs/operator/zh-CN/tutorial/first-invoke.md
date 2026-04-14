# Tutorial · 第一次 Invoke（10 分钟）

> **source_en:** 2026-04-14 · authoritative

本教程带您从刚启动的 dashboard 走到第一次 **Singularity Sync / Invoke** —
那是让 orchestrator "盘点系统、决定下一步、执行"的全局动作。走完您会
认得 AI 点亮的每个元素，也知道要介入时该点哪里。

## 开始前

- 后端在 `http://localhost:8000`（或 `BACKEND_URL` 指向的地址）。
  用 `curl http://localhost:8000/api/v1/health` 确认。
- 前端在 `http://localhost:3000`。
- `.env` 至少一个 LLM provider key，或没 key（rule-based fallback 仍能跑，
  agent 只是回模板响应）。

## 1 · 熟悉环境

打开 `http://localhost:3000`。新浏览器会自动启动 **5 步首次导览**（每张卡
底部有 Skip / Next）。导览结束后 dashboard 就是您的。看一眼顶栏：

- **MODE** pill — 默认 SUPERVISED。表示常规 AI 动作自动执行，有风险的会等您。
  [→ 详情](../reference/operation-modes.md)
- **`?` 说明图标**（MODE 旁边）— 忘记什么按钮做什么时随时点。
- **Decision Queue**（右侧 tile）— 目前是空的。AI 无法自动执行的决策会落在这。

## 2 · 挑最简单的 task

开 **Orchestrator AI** panel（桌面版在中央，手机 swipe 过去）。输入框输入：

```
/invoke 列出当前连接的硬件设备
```

按 Enter。

## 3 · 看 pipeline 点亮

一连串事情会接续发生，这是正常的：

1. 左侧 **REPORTER VORTEX** log 流打印 `[INVOKE] singularity_sync: ...`。
2. **Agent Matrix** panel 有一个 agent 转 `active`。thought-chain 一行一行更新。
3. 一到多个 **Tool progress** 事件显示文件读取 / shell 调用。
4. 在 SUPERVISED mode 下，若 agent 提出 `risky` 或 `destructive` 的东西，
   右上会弹 **Toast**，且该项也会进入 **Decision Queue**。

本次"只读列表"调用应该不会产生决策 — AI 直接在对话中回答。

## 4 · 看答案

Orchestrator 在 panel 中回复一条消息，您应会看到连接设备列表（若开发笔记本
没接摄像头，列表可能是空的 — 正常）。

## 5 · 试个较有风险的 invoke

```
/invoke 在当前 workspace 建立名为 tutorial-sandbox 的 git branch
```

这次在 SUPERVISED mode 下，您应该会看到 **Decision Queue** 出现 severity
`risky` 的项目。Toast 显示 A / R / Esc 键盘提示与倒计时。

- 按 **A**（或点 APPROVE）— AI 建立 branch。
- 按 **R** — AI 停手。
- 让倒计时跑完 — 解析为默认安全选项（通常是"停手"）。

若没看到决策，可能是 agent 因规则或您把 MODE 切到 FULL_AUTO / TURBO
而自动执行了。查 Decision Queue panel 内的 `?` 看 severity 矩阵。

## 6 · 试 MANUAL mode

点 MODE pill → MANUAL。重跑建 branch 的 invoke。现在 *每一个* 步骤都进
Decision Queue，包括常规读取。这是"我想先看 AI 要做什么才让它动"的
正确 mode。

探索完切回 SUPERVISED。

## 下一步

- [处理一个决策](handling-a-decision.md) — risky/destructive 决策的完整
  生命周期，含 undo。
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  矩阵细节。
- [Budget Strategies](../reference/budget-strategies.md) — 本教程期间
  token 花费令您担心时。
- [Troubleshooting](../troubleshooting.md) — 某些元素没按文字点亮时。
