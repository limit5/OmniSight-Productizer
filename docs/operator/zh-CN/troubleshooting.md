# Troubleshooting — dashboard 告诉您出问题时

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

按操作员实际看到的现象整理。本页没涵盖的情况请看 Orchestrator AI
panel 的 log 流（REPORTER VORTEX）与后端 stderr。

## Panel 出现红色 banner

### `[AUTH] ...`
后端以 401 / 403 拒绝。

- **原因**：后端设了 `OMNISIGHT_DECISION_BEARER` 但前端 token 错误或缺失。
- **处理**：开 Settings → provider 标签重新输入 bearer；或单机部署时
  于 `.env` 取消 `OMNISIGHT_DECISION_BEARER`。

### `[RATE LIMITED] ...`
滑动窗口节流触发（默认每客户端 IP 每 10 秒 30 次）。

- **原因**：脚本轮询或 UI 失控重试。
- **处理**：等 banner 自动消失（10 秒），或用 `OMNISIGHT_DECISION_RL_MAX`
  / `_WINDOW_S` 放宽上限，详见 `.env.example`。

### `[NOT FOUND] ...`
端点返回 404。

- **原因**：前端调用后端已移除或改名的端点。通常是部分部署后版本不一致。
- **处理**：硬刷新页面。若持续，前后端版本不同 — 两边都重启。

### `[BACKEND DOWN] ...`
后端返回 5xx。

- **原因**：uvicorn 没跑、或 router 中未处理的异常。检查
  `/tmp/omni-backend.log`（dev）或服务 log（prod）。
- **处理**：重启后端。若启动即挂，前台跑 `python3 -m uvicorn backend.main:app`
  看 stack。

### `[NETWORK] ...`
fetch 在抵达后端前就失败。

- **原因**：后端进程死了、port 错、或 proxy / VPN 断连。
- **处理**：`curl http://127.0.0.1:8000/api/v1/health`。若有回应，前端
  `NEXT_PUBLIC_API_URL` 或 rewrite 配置错误。若无回应，启动后端。

## Decision Queue 看起来卡住

### Pending 决策按下 approve / reject 后没消失
- **原因 1**：后端返回 409 — 该决策已被其他标签解决。UI 会在下个 SSE
  事件对齐；按 panel header 的 **RETRY** 强制。
- **原因 2**：destructive severity 的 `window.confirm()` 对话框仍开在
  隐藏标签。检查所有 dashboard 标签。

### 决策每次都还没按就 timeout
- propose 时默认 `timeout_s` 为 60。若 producer 设了更短的 deadline
  而您来不及反应，sweep loop 会解析为 default 安全选项。这是预期行为。
- 若要更多时间：切到 MANUAL mode（不设 deadline，决策会无限期保留 —
  确认方式是查看 decision payload 的 `deadline_at`）。

### SWEEP 按了没反应
- 只会解析 deadline **已过** 的决策。若全部都还在时间窗口内，0 条被解析
  并出临时消息告知。

## Toast 问题

### "+N MORE PENDING" 徽章不消失
- 关掉所有可见 toast（逐个按 Esc 或点 ✕）。overflow 计数只在堆栈归 0 时重置。
- 若仍持续，后端发 `decision_pending` 的速度比您处理快。调低 MODE
  （SUPERVISED 或 MANUAL）避免常规决策自动执行后产生新的 risky/destructive
  后续。

### 倒计时卡在 100 %
- 后端与浏览器时钟偏移。两边 `date -u` 比对。
- 后端时钟比浏览器早时，进度条会满值停留直到真实 deadline 过后瞬跳 0。

### 倒计时显示 NaN 或奇怪数值
- 后端发了格式错误的 `deadline_at`。审计项 B2 新增的验证应已强制类型，
  若仍看到：硬刷新（js 缓存）；持续则开 issue 附上原始 SSE payload。

## Agent 问题

### Agent 卡在 "working" 超过 30 分钟
- watchdog 30 分钟后触发，会提案 stuck 补救决策（switch model /
  spawn alternate / escalate）。查 Decision Queue。
- 60 秒内都没东西出现代表 watchdog 认为该 agent 有活跃心跳。用
  **Emergency Stop** → Resume 强制重置。

### Agent 反复卡同一个错
- 每个 agent 的 error ring buffer（10 条）由 node graph 喂。
  窗口内第 3 次相同错时 stuck detector 于 FULL AUTO / TURBO 自动提案
  `switch_model` 补救；较低 mode 则排入队列等批准。
- 若从未走到这步，该错可能未以 tool error 形式浮出 — 查 REPORTER VORTEX。

### Provider health 显示红但我的 key 没问题
- Provider health = 最近 3 次 probe ping。额度用尽算健康失败。查看
  provider dashboard。
- key 有效的话 keyring 可能加载了旧版。Settings → Provider Keys → 重新保存。

## 手机 / 平板问题

### 手机上有些 panel 点不到
- 底部 nav 点列对应 12 个 panel。若看到少于 12 个，表示跑的是 Phase 50D
  前的 build。硬刷新。
- swipe prev/next 按钮按顺序循环。

### 深链开到错的 panel
- `?panel=` 优先于 `?decision=`。拿掉 `?panel=` 组件，或确保深链决策 id
  时设为 `?panel=decisions`。

## 真的卡住了

- `curl http://localhost:8000/api/v1/system/sse-schema | jq` — 确认后端
  有响应且发送前端预期的事件类型。
- `pytest backend/tests/test_decision_engine.py` — 决策引擎的 27 个
  测试 < 1 秒完成，可抓到大部分后端回归。
- 开 issue 附上：后端 commit hash（`git rev-parse HEAD`）、红色
  banner 文字、REPORTER VORTEX 最后 50 行。

## 相关

- [Operation Modes](reference/operation-modes.md)
- [Decision Severity](reference/decision-severity.md)
- [Glossary](reference/glossary.md)
