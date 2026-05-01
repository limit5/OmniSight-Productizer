---
audience: operator
status: accepted
date: 2026-05-02
priority: AB.8 — Subscription → API key migration runbook
related:
  - docs/operations/anthropic-api-migration-and-batch-mode.md (AB ADR)
  - backend/agents/anthropic_mode_manager.py (state machine)
  - backend/agents/cost_guard.py (AB.6 spend caps)
  - backend/agents/rate_limiter.py (AB.7 workspace partitioning)
---

# Anthropic API Migration Runbook

> **One-liner**：把 OmniSight 自身開發 workflow 從 Claude 訂閱版（Pro / Max
> via Claude Code CLI / OAuth）切到 **Anthropic API key + Batch mode**。
> 5 分鐘 step-by-step、附 30 天 rollback grace 安全網。

## 0. 前置確認（5 分鐘）

- [ ] 你有 Anthropic 帳號（console.anthropic.com 已登入）
- [ ] 帳號已升 Tier 4（檢查：console → Plans & Billing 顯示 RPM ≥ 4000）
- [ ] 確認本月 LLM 預算上限（建議：dev workspace ≤ $50 / month）
- [ ] OmniSight backend 已部署 AB.1-AB.7（`python -m backend.agents.tool_schemas --list` 跑得起來）
- [ ] OmniSight 既有 Claude 訂閱版仍能用（驗 `claude --version` 成功）

⚠️ **這條 runbook 改 OmniSight 自身開發 workflow、不影響對外提供給客戶的
LLM 整合**。客戶端走的 `backend/llm_adapter.py` multi-provider facade 不變。

---

## 1. 取得 API Key（~3 分鐘）

1. 進 https://console.anthropic.com/settings/keys
2. **Create Key** → 命名為 `omnisight-dev`（建議：dev / batch / production
   各別建一把、即 AB.7.5 三 workspace 切分）
3. 複製 key（只顯示一次！）
4. 在 console → **Billing → Usage Limits** 設 **monthly cap**（防 R76 燒
   爆帳單）：
   - dev: $50 / month
   - batch: $200 / month
   - production: 視業務量定

> ⚠️ 步驟 4 的上限是 Anthropic 端的硬閘（即便 OmniSight cost guard 失靈
> 也擋得住）。**不要省**。

---

## 2. 啟動 OmniSight 切換 Wizard（5 分鐘 / 5 步）

### Step 1 — 提交 API Key

API endpoint（背後接 `AnthropicModeManager.submit_api_key()`）:

```bash
# 起始 wizard
curl -X POST http://localhost:8000/api/v1/anthropic-mode/wizard/start \
  -d '{"target_workspace": "production"}'

# 提交 key（注意：key 走 AS Token Vault 加密、不會明文寫 log）
curl -X POST http://localhost:8000/api/v1/anthropic-mode/wizard/submit-api-key \
  -H "Content-Type: application/json" \
  -d '{"api_key": "sk-ant-...vour-key..."}'
```

回傳預期：
```json
{
  "current_step": "key_obtained",
  "api_key_configured": true,
  "api_key_fingerprint": "…NDEF5678",
  "mode": "subscription"  ← 還沒切，正常
}
```

### Step 2 — 設定 Spend Limits

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/wizard/spend-limits \
  -d '{"daily_usd": 30.0, "monthly_usd": 500.0}'
```

對齊原則：**OmniSight 端 cap 設 Anthropic console cap 的 50-70%**，留
buffer 給 cost guard 的 80% / 100% / 120% 三階 alert（AB.6.5）。

### Step 3 — 切換 Mode

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/wizard/switch-mode
```

✅ **這步開始 OmniSight default mode = API**。但訂閱版 fallback 仍保留
（`fallback_subscription_kept=true`）— 30 天 rollback grace 開始計時。

### Step 4 — Smoke Test

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/wizard/smoke-test
```

跑一個小的真實 API call、覆蓋：
- API key auth ✓
- Tool calling 路徑 ✓
- Token tracker 紀錄 ✓
- Cost guard 計費 ✓

若失敗、wizard **不會** 自動 rollback。看 `state.smoke_test.error_message`、
修問題後重跑 `smoke-test` endpoint（idempotent、再試一次即可）。
若無法修、走第 5 節 rollback。

### Step 5 — Confirm

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/wizard/confirm
```

回傳 `current_step: "confirmed"` + `rollback_grace_until: 2026-06-01...`。
30 天 grace 開始；其間隨時可走 `rollback`。

---

## 3. 切換後驗證（10 分鐘）

### 3.1 觀察 cost dashboard（AB.6 + Z）

```bash
# 看當前 daily spend
curl http://localhost:8000/api/v1/cost/usage?scope=workspace&key=production

# 看 alert 紀錄
curl http://localhost:8000/api/v1/cost/alerts?since=2026-05-02T00:00:00Z
```

預期：smoke test 應該觸發 ~$0.001 的 daily spend 紀錄。

### 3.2 觀察 rate limit tracker（AB.7）

```bash
curl http://localhost:8000/api/v1/rate-limit/usage?workspace=production&model=claude-sonnet-4-6
```

回傳 `{"requests": 1, "input_tokens": ..., "output_tokens": ...}`，反映
smoke test 那一發。

### 3.3 跑一個 batch（AB.3 + AB.4）

提交 10 個 routine task 走 batch、預期 50% 折扣：

```bash
curl -X POST http://localhost:8000/api/v1/batch/submit \
  -d '{"tasks": [...10 tasks...], "lane": "batch"}'
```

batch 進度跑完 ~10 分鐘後檢查實際 cost vs estimate（差距應 < 10%）。

---

## 4. 30 天觀察期（每週 5 分鐘）

每週至少跑一次：

- [ ] 看 monthly spend 趨勢、確認不超預算
- [ ] 檢查 DLQ entries：`curl /api/v1/dlq/list` → 預期 0 或極少
- [ ] 比對訂閱版 vs API mode 的 dev velocity（task 完成速度 / 數量）
- [ ] 若有任何疑慮 → 走第 5 節 rollback、不要硬撐

---

## 5. Rollback（30 天 grace 內隨時可走）

**情境**：API mode 出問題（cost 失控 / latency 退步 / 大量 DLQ entries / smoke test 跑不通），想切回訂閱版。

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/rollback
```

回傳 `mode: "subscription"`、`current_step: "not_started"`。

✅ Rollback 是 **lossless + idempotent**：
- API key 配置仍在（隨時可重 wizard）
- 既有 batch / cost / DLQ 資料不刪
- 訂閱版 OAuth credential 仍活、Claude Code CLI 立即可用

⚠️ **30 天 grace 過後執行 `finalize_disable_subscription()` 之後**：
- `fallback_subscription_kept=False`
- rollback **不再可用**
- 需手動 re-enroll 訂閱版（重新走 Anthropic 登入流程）

僅在 **API mode 在 production 跑滿 30 天無 incident** 時才 finalize。

---

## 6. Finalize Disable Subscription（grace 結束後執行）

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/finalize
```

只在以下條件全滿足時執行：
- [ ] `state.current_step == "confirmed"` 已超過 30 天
- [ ] 過去 30 天 monthly spend 在預算內
- [ ] DLQ entries < 10（沒有結構性 retry 問題）
- [ ] dev velocity 比訂閱版時代有明顯提升（非主觀感受）
- [ ] 確認 Claude 訂閱不退費（Anthropic Pro / Max 月費）

---

## 7. 故障排除

### 7.1 Smoke test 失敗（401 Unauthorized）

→ API key 拼錯 / 失效。回 console 重複 key、或建新 key、走
   `submit-api-key` 重提（idempotent、會覆寫前一把）。

### 7.2 Smoke test 失敗（429 Rate Limited）

→ Tier 不對。檢查 console → Plans & Billing。需先 upgrade tier 才能 submit。

### 7.3 Smoke test 失敗（網路 error）

→ 防火牆 / proxy 擋 api.anthropic.com。檢查 `curl https://api.anthropic.com/v1/messages`
   是否通。AB.7 retry policy 會自動重試 5 次後 DLQ；若每次都 fail，
   先解網路再重試。

### 7.4 Wizard step 拒絕（WizardOutOfOrderError）

→ Step 跳過。看 `state.current_step` 確認當前位置、依順序執行
   （submit-api-key → spend-limits → switch-mode → smoke-test → confirm）。

### 7.5 Cost spike 異常（80% / 100% / 120% alert 持續觸發）

→ 立即執行 rollback（第 5 節）。然後分析：
   - 看 `cost_estimates` 表 vs `cost_actuals` 是否估算偏差大
   - 看 `dlq` 是否有大量 retry 重複觸發 cost
   - 看 batch task 是否誤路由到 realtime lane

### 7.6 訂閱版 Claude Code CLI 在 wizard 後不能用

→ Wizard 不影響訂閱版 OAuth session。檢查：
   - `~/.claude/auth.json` 是否還在
   - `claude --version` 是否 timeout
   - 重新跑 `claude login` 即可

---

## 8. 監控 + alert 閾值建議

| 指標 | 來源 | Warn | Critical |
|------|------|------|----------|
| Monthly spend | AB.6 cost guard | 80% of cap | 100% of cap |
| Daily spend | AB.6 | 50% of daily cap by 12pm | 80% by 6pm |
| DLQ entries | AB.7 DLQ | > 5 / hour | > 20 / hour |
| Smoke test latency | wizard step 4 | > 5s | > 30s |
| Rate limit predictions | AB.7 tracker | RPM > 80% of Tier 4 | RPM > 95% |

---

## 9. 與 Z Provider Observability 整合

切換後 Z 既有的 LLM provider observability（balance / rate-limit /
real usage）會持續顯示 Anthropic 相關指標。**新增**：
- AB.6 cost alerts surface 在同一 dashboard card
- AB.7 DLQ 數量 surface 在 provider health 面板
- AB.8 wizard state（current_step / mode / fallback_kept）surface 在
  Settings → Provider Keys

---

## 10. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-05-02
- **Status**: 已 ship、與 AB.1-AB.7 backend 配套
- **Next review**: 第一次完整跑過 wizard 後（內部 dogfood 完成）+
  第一個 30-day grace 結束時更新故障排除節
