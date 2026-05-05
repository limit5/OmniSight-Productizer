---
audience: operator
status: accepted + complete for repository DoD
date: 2026-05-06
priority: AB Definition of Done — Operator API migration runbook
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

> **Completion note（2026-05-06）**：本 runbook 現在覆蓋 AB DoD 需要的
> operator migration SOP：API key provisioning、wizard 切換、R76-R80 guard、
> API-mode smoke、第一個 >=100 task batch、七類 batch velocity ledger、30 天
> fallback grace、`OMNISIGHT_AB_API_MODE_ENABLED=true` finalize lock、rollback
> 與 production evidence handoff。它不是「已在 production 啟用」的宣告；實際
> 啟用仍由第 10 節 evidence ledger 和第 11 節 production gate 記錄。

## 0. 前置確認（5 分鐘）

- [ ] 你有 Anthropic 帳號（console.anthropic.com 已登入）
- [ ] 帳號已升 Tier 4（檢查：console → Plans & Billing 顯示 RPM ≥ 4000）
- [ ] 確認本月 LLM 預算上限（建議：dev workspace ≤ $50 / month）
- [ ] OmniSight backend 已部署 AB.1-AB.10（`python -m backend.agents.tool_schemas --list` 跑得起來）
- [ ] OmniSight 既有 Claude 訂閱版仍能用（驗 `claude --version` 成功）
- [ ] R76-R80 mitigation evidence 已讀：
      [`ab_r76_r80_mitigation_evidence.md`](ab_r76_r80_mitigation_evidence.md)
- [ ] 七類 batch velocity evidence template 已讀：
      [`ab_batch_velocity_evidence.md`](ab_batch_velocity_evidence.md)

⚠️ **這條 runbook 改 OmniSight 自身開發 workflow、不影響對外提供給客戶的
LLM 整合**。客戶端走的 `backend/llm_adapter.py` multi-provider facade 不變。

### 0.1 這條 runbook 不做的事

- 不替客戶 LLM provider 切換 API key、不改 tenant provider settings。
- 不手寫或修改 API key 明文；key 只進 AS Token Vault / KS.1 envelope path。
- 不在 30 天 grace 前 disable 訂閱版 fallback。
- 不把 batch lane 用在 chat UI、incident response、rollback、human-in-loop prompt
  等 realtime-required path；這些 path 仍走 realtime lane。

### 0.2 Live gate matrix

| Gate | 必須保存的證據 | 完成條件 |
|---|---|---|
| API-mode smoke | wizard smoke response、Anthropic usage row、cost guard spend row | auth/tool/cost/rate-limit 全綠 |
| 第一個 >=100 task batch | batch id、100 個 custom_id mapping、results export、estimate vs actual ledger | 100% task accounted、actual cost vs estimate 偏差 < 10% |
| 50% batch discount | realtime equivalent estimate、batch actual、Anthropic usage export | observed discount 約 50%（cache input 另列） |
| 七類 velocity | `ab_batch_velocity_evidence.md` ledger row | API+Batch tasks/day >= 訂閱版 baseline 2x |
| 30 天 grace | daily/weekly spend、latency、error/DLQ、rollback 未觸發 | clean observation 才能 finalize |
| Finalize lock | 每個 backend replica 的 env snapshot | `OMNISIGHT_AB_API_MODE_ENABLED=true` 全部 replica 可見 |

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

先提交 10 個 routine task 走 batch，確認 lane / DLQ / cost path 工作；再提交
第一個 AB DoD 要求的 >=100 task batch。10-task 是 smoke，>=100 task 才是 DoD
evidence。

```bash
curl -X POST http://localhost:8000/api/v1/batch/submit \
  -d '{"tasks": [...10 tasks...], "lane": "batch"}'
```

batch 進度跑完 ~10 分鐘後檢查實際 cost vs estimate（差距應 < 10%）。

第一個 >=100 task batch 執行時，把下列欄位貼進第 10 節 evidence ticket：

```text
batch_run_id:
submitted_count:
succeeded_count:
errored_count:
estimated_cost_usd:
actual_cost_usd:
variance_pct:
realtime_equivalent_estimate_usd:
batch_discount_observed_pct:
anthropic_usage_export_path:
batch_results_export_path:
dlq_entries:
operator_initials:
```

### 3.4 七類 batch velocity 量測（AB DoD）

第一週 dogfood 後，把 HD.1 / HD.4 / HD.5.13 / HD.18.6 / L4.1 / L4.3 /
TODO routine 七類任務各自提交到 batch lane，並依
[`ab_batch_velocity_evidence.md`](ab_batch_velocity_evidence.md) 記錄：

- `batch_tasks_submitted` / `batch_tasks_succeeded`
- `subscription_baseline_tasks_per_day` / `api_batch_tasks_per_day`
- `wall_clock_hours_saved`
- `estimated_cost_usd` / `actual_cost_usd`
- `batch_discount_observed_pct`
- `p95_batch_completion_hours`
- `dlq_rate_pct`

完成條件：七類 routing 全覆蓋、第一個完整樣本 >= 100 tasks、API+Batch
tasks/day >= 訂閱版 baseline 2x、actual cost vs estimate 偏差 < 10%、P95
completion <= 24h、DLQ rate < 2%。

### 3.5 Realtime lane guard

API mode 啟用後，operator 必須保留至少一個 realtime-required smoke，確認 batch
eligibility 不會誤把互動 path 丟進 24h batch SLA：

```text
task_kind: chat_ui
force_lane: batch
expected: vetoed to realtime
evidence: backend/tests/test_ab_e2e_smoke.py::test_e2e_realtime_required_cannot_be_batched
```

如果 UI/runner 看到 chat、rollback、incident task 進 batch queue，立即 rollback
到 subscription mode 或停用 batch lane，因為這是 R77/R78 class regression。

---

## 4. 30 天觀察期（每週 5 分鐘）

每週至少跑一次：

- [ ] 看 monthly spend 趨勢、確認不超預算
- [ ] 檢查 DLQ entries：`curl /api/v1/dlq/list` → 預期 0 或極少
- [ ] 比對訂閱版 vs API mode 的 dev velocity（task 完成速度 / 數量）
- [ ] 抽查 Anthropic console Usage Limits 仍低於預算上限（R76 external hard cap）
- [ ] 抽查 batch completion SLA：P95 <= 24h、DLQ rate < 2%
- [ ] 抽查 realtime lane：human-interactive task 沒有被 batch queue 接走
- [ ] 若有任何疑慮 → 走第 5 節 rollback、不要硬撐

每週紀錄格式：

```text
week:
observation_start:
observation_end:
api_mode_enabled_replicas:
monthly_spend_usd:
daily_peak_spend_usd:
latency_p50_ms:
latency_p95_ms:
error_rate_pct:
dlq_rate_pct:
batch_p95_completion_hours:
api_batch_tasks_per_day:
subscription_baseline_tasks_per_day:
rollback_triggered: yes/no
operator_decision: continue/rollback/finalize-ready
```

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

先把 API mode single knob 寫進所有 backend worker 共同使用的 env，並重啟每個
replica：

```bash
sed -i.bak 's/^OMNISIGHT_AB_API_MODE_ENABLED=.*/OMNISIGHT_AB_API_MODE_ENABLED=true/' /opt/omnisight/.env
grep -q '^OMNISIGHT_AB_API_MODE_ENABLED=' /opt/omnisight/.env \
  || echo 'OMNISIGHT_AB_API_MODE_ENABLED=true' >> /opt/omnisight/.env

docker compose up -d --force-recreate backend-a backend-b
docker compose exec -T backend-a env | grep '^OMNISIGHT_AB_API_MODE_ENABLED=true$'
docker compose exec -T backend-b env | grep '^OMNISIGHT_AB_API_MODE_ENABLED=true$'
```

`finalize_disable_subscription()` 會拒絕在這個 lock 缺失或為 false 時執行；
避免 30 天觀察期後一邊刪掉訂閱 fallback、一邊仍有 worker 因 env 漏設而走回
subscription path。

```bash
curl -X POST http://localhost:8000/api/v1/anthropic-mode/finalize
```

只在以下條件全滿足時執行：
- [ ] `state.current_step == "confirmed"` 已超過 30 天
- [ ] `OMNISIGHT_AB_API_MODE_ENABLED=true` 已套用到所有 backend replica
- [ ] 過去 30 天 monthly spend 在預算內
- [ ] DLQ entries < 10（沒有結構性 retry 問題）
- [ ] dev velocity 比訂閱版時代有明顯提升（非主觀感受）
- [ ] 確認 Claude 訂閱不退費（Anthropic Pro / Max 月費）
- [ ] 第 10 節 evidence ticket 已附 API-mode smoke、>=100 task batch、
      50% discount、30-day observation、env lock snapshot

Finalize 後做最後一次不可回復確認：

```bash
curl http://localhost:8000/api/v1/anthropic-mode/state
claude --version
```

預期：
- `fallback_subscription_kept=false`
- `mode="api"`
- `rollback` endpoint 回傳「fallback disabled」類錯誤
- `claude --version` 可成功或失敗都不影響 OmniSight API mode，但若仍保留 CLI，
  operator 應在 evidence ticket 註明它只作 manual emergency tool，不是 runtime fallback。

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

### 7.7 Batch 結果超過 24h 未回來

→ 先視為 R77 lane/SLA incident，不要重送同一批 100 task 造成重複 cost。
   檢查：
   - Anthropic batch status 是否仍 processing
   - OmniSight `batch_runs` / `batch_results` 是否有 partial result
   - callback 是否已為 failed result fan-out
   - DLQ 是否有 submit / stream_results failure

若 Anthropic 端仍 processing，等到 24h SLA 結束；若 OmniSight callback 已 failure，
把該 batch id、failed custom_id list、DLQ entry id 貼進第 10 節 evidence ticket。

### 7.8 Cost actual vs estimate 偏差 >= 10%

→ 不要 finalize。先判斷偏差來源：
   - pricing table 是否過期（看 `backend/tests/test_ab_cost_regression.py`）
   - prompt caching 是否讓 actual 明顯低於 estimate（可接受，但要另列）
   - retry / duplicate submit 是否把 actual 墊高
   - realtime equivalent 是否誤把 batch discount 重複折扣

修正前，第一個 100-task batch DoD 不算完成。

### 7.9 API-mode worker env 不一致

→ 若 `backend-a` 有 `OMNISIGHT_AB_API_MODE_ENABLED=true` 但 `backend-b` 沒有，
   停止 finalize。修 `.env` / compose env 後重啟所有 replica，再重跑第 6 節
   env snapshot。這個 gate 是 cross-worker consistency guard：每個 worker 從同一
   deployed env 推導 API-mode lock；沒有 Redis/PG 協調，env 必須一致。

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

## 10. Evidence ticket template（每次實際切換必填）

把以下 template 貼到 deploy / dogfood ticket。不要貼 API key、OAuth token、
完整 request payload、或任何 customer secret。

```markdown
## Anthropic API migration evidence

- Operator:
- Environment: dev/staging/production/dogfood
- Git ref:
- Backend image:
- Wizard start:
- Wizard confirmed:
- Rollback grace until:
- Finalize executed: yes/no

### External hard caps
- Anthropic workspace:
- Console monthly usage cap:
- OmniSight daily cap:
- OmniSight monthly cap:
- Evidence screenshot/export:

### API-mode smoke
- smoke timestamp:
- model:
- tool path exercised:
- cost_estimate_id:
- cost_actual_usd:
- rate-limit usage row:
- DLQ entries:

### First >=100 task batch
- batch_run_id:
- submitted_count:
- succeeded_count:
- errored_count:
- estimated_cost_usd:
- actual_cost_usd:
- variance_pct:
- realtime_equivalent_estimate_usd:
- batch_discount_observed_pct:
- p95_completion_hours:
- batch_results_export_path:
- anthropic_usage_export_path:
- DLQ entries:

### Seven-family velocity
- evidence doc updated:
- HD.1:
- HD.4:
- HD.5.13:
- HD.18.6:
- L4.1:
- L4.3:
- TODO routine:
- API+Batch tasks/day:
- subscription baseline tasks/day:
- uplift:

### 30-day observation
- observation window:
- monthly spend max:
- daily spend max:
- latency p50/p95:
- error rate:
- DLQ rate:
- rollback triggered: yes/no
- incidents:

### Finalize lock
- backend-a env snapshot:
- backend-b env snapshot:
- fallback_subscription_kept after finalize:
- rollback endpoint after finalize:
```

## 11. Production status gate

Repository status for this runbook row is **dev-only**: the SOP is complete
and linked by ADR / evidence tests, but production is not considered active
until an operator runs the ticket template above in the target environment.

Promotion states:

| Status | Flip when |
|---|---|
| `dev-only` | runbook / tests / docs merged only |
| `deployed-inactive` | backend image rebuilt, env/API key caps prepared, API mode still off |
| `deployed-active` | wizard confirmed, API-mode smoke green, first >=100 task batch recorded |
| `deployed-observed` | 30-day grace clean, finalize lock applied, subscription fallback disabled |

**Production status:** dev-only
**Next gate:** deployed-active — operator executes this runbook in the
dogfood/production-equivalent environment, attaches the API-mode smoke,
first >=100 task batch, observed batch discount, cost variance, DLQ/latency
metrics, and starts the 30-day grace observation before finalize.

---

## 12. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-05-06
- **Status**: repository DoD complete；production activation pending operator evidence
- **Next review**: 第一次完整跑過 wizard、第一個 >=100 task batch 完成、以及
  第一個 30-day grace 結束時更新故障排除節
