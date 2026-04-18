# SLO / SLI — Service-Level Objectives (H4, 2026-04-19)

Per pre-prod audit H4: OmniSight had RTO/RPO defined (`dr_rto_rpo.md`)
but no request-level SLOs. Without them the on-call has no numeric
basis to decide "is 2% error rate actionable?" or "should I rollback
now?". This file pins the first committed set.

Thresholds here are the **target** operators page on. They are tuned
to current production headroom + what the stack can actually deliver
given Cloudflare Tunnel + Caddy + one uvicorn replica per worker
pod. Loosen only with a post-mortem; tighten as the observed p95
improves.

---

## 1. HTTP request latency (p50 / p95 / p99)

**SLI**: end-to-end wall-clock time of user-facing HTTP requests,
measured from uvicorn's request-start event to final response bytes
written (the `http_request_duration_seconds` histogram when mounted —
today a proxy via Caddy's access log on the Grafana HA dashboard).

**Target**:

| Percentile | Target | Alert threshold |
|------------|--------|-----------------|
| p50        | ≤ 100 ms | warn when > 200 ms for 10 min |
| p95        | ≤ 500 ms | warn when > 1 s for 5 min |
| p99        | ≤ 2 s   | page when > 5 s for 3 min |

**Exclusions**: `/readyz`, `/livez`, `/healthz`, `/metrics`, and
`/bootstrap/*` before finalization. These are probe paths whose
latency reflects Caddy's polling cadence, not user-facing UX.

**Measurement window**: rolling 5 min on Grafana HA dashboard.

**Rationale for numbers**:
* p50 100 ms = "the dashboard feels fast" threshold. Anthropic's API
  average round-trip for a single-turn agent call is ~800 ms; most
  OmniSight UI requests don't exit the backend (DB read, config
  fetch) so sub-100-ms is achievable.
* p95 500 ms = covers LLM-free dashboard flows + the common cross-DB
  read patterns. A 500-ms p95 is the ceiling before a user flow
  starts feeling sticky.
* p99 2 s = the outlier bound for LLM-dependent paths. The next
  layer (15 s — agent task completion) lives in the task queue, not
  in synchronous HTTP.

---

## 2. Error rate (5xx / total)

**SLI**: `rate(omnisight_rolling_deploy_responses_total{status_class="5xx"}[5m])
/ rate(omnisight_rolling_deploy_responses_total[5m])` — the exact
gauge the `OmniSightRollingDeploy5xxRateHigh` alert already uses.

**Target**: < 0.5 % over a rolling 5 min window.

**Alert thresholds**:
* ≥ 0.5 % for 5 min → warn (investigate).
* ≥ 1 % for 2 min → **page** (the existing
  `OmniSightRollingDeploy5xxRateHigh` alert).
* ≥ 5 % for 1 min → page oncall + auto-initiate `scripts/deploy.sh
  --rollback` evaluation.

**Exclusions**: deliberate 5xx during drain (drain middleware returns
503 with `Connection: close`; Caddy ejects the replica from the pool
and routes to the peer, so these shouldn't appear in aggregate
counts past 2 min of a rolling deploy).

---

## 3. Availability

**SLI**: proportion of successful `/readyz` probes over a rolling
window, polled every 30 s by the uptime checker (Caddy's active
health probe at `health_uri /readyz`, plus an external synthetic).

**Target**: **99.5 %** monthly → ≤ 3.6 h downtime/month, or 21.6 min
for a single incident's error budget.

**Sub-targets**:
| Measurement window | Target  | Budget spend |
|-------------------|---------|--------------|
| 30 d rolling       | ≥ 99.5 % | 3.6 h        |
| 7 d rolling        | ≥ 99.7 % | 30 min       |
| 1 h rolling        | ≥ 99 %   | 36 s         |

**Alert thresholds**:
* 30-d budget > 75 % burned → warn; slow deployment pace, review
  post-mortems.
* 30-d budget > 100 % burned → freeze non-critical changes until
  budget recovers.
* 1 h availability < 99 % → page.

---

## 4. Task queue health

**SLI 1 — queue depth** (`omnisight_queue_depth`, pending tasks):
* Target: < 100 at the P1/P2 level; < 10 at P0.
* Alert: > 500 for 5 min at any priority → warn (capacity shortage
  or worker stuck).

**SLI 2 — task completion time** (`omnisight_task_completion_seconds`,
not-yet-exposed — follow-up):
* Target: p95 < 2 min for trivial agent turns (classify / ping),
  < 15 min for compound tasks.

**SLI 3 — worker liveness** (`omnisight_workers_active`):
* Target: ≥ 1 active worker per enabled `omnisight-worker@N`.
* Alert: 0 active workers for 2 min → page
  (`OmniSightBackendInstanceDown` already covers the backend side;
  this is the worker-pool equivalent — follow-up row to add).

---

## 5. LLM provider dependency

**SLI**: fallback invocation rate
(`omnisight_llm_provider_fallback_total`, not-yet-exposed — audit M
follow-up).

**Target**: < 5 % of LLM calls hit the fallback chain.

**Alert threshold**: > 25 % for 10 min → warn (primary provider is
flaky; consider switching `OMNISIGHT_LLM_PROVIDER` to the more-stable
secondary).

**Hard dependency note**: LLM availability is NOT part of the 99.5 %
availability SLO. `/readyz` shallow check just verifies a key is
set; `OMNISIGHT_READYZ_DEEP_CHECK=1` (C3 audit) adds real-probe
status but is opt-in. Customer-facing LLM failure surfaces as a
task-level error, not a request-level 5xx — users see the error in
their task history, not as a page reload.

---

## 6. How these map to the existing alerts file

| SLO       | Prometheus alert                          | Severity |
|-----------|-------------------------------------------|----------|
| 5xx rate  | `OmniSightRollingDeploy5xxRateHigh`       | critical |
| Replica lag | `OmniSightReplicaLagHigh`               | warning  |
| Instance up | `OmniSightBackendInstanceDown`          | critical |
| Migrations | `OmniSightMigrationMismatch` (H2)        | critical |
| Latency p99 | *not yet wired* — follow-up           | —        |
| Availability 1h | *external synthetic check* — follow-up | —    |

The latency + availability gaps are tracked follow-ups; the current
alert set covers the immediately-critical incident surface.

---

## 7. Incident severity mapping

| Severity | Trigger | Response time |
|----------|---------|---------------|
| SEV-1    | p99 > 10 s for 5 min, OR 5xx > 5 % for 2 min, OR 1 h availability < 98 % | page, 15 min ack |
| SEV-2    | Alert fires, service degraded | page, 1 h ack |
| SEV-3    | Non-blocking alert (lag warning, budget > 75 %) | ticket, next business day |

**Error budget exhaustion policy**: if the 30-day budget is spent,
freeze non-critical deploys for the remainder of the window. Only
security and data-integrity fixes land. This is the primary SLO
enforcement lever.

---

## 8. Follow-up work

- [ ] Wire `http_request_duration_seconds` histogram with `method` +
  `status_class` labels (audit Medium).
- [ ] External synthetic check (StatusCake / HetrixTools) that polls
  a canary path every 30 s and feeds a 30-day availability gauge.
- [ ] Grafana panel pinning these thresholds so "current vs target"
  is visible on the HA dashboard.
- [ ] Error-budget burn rate alert (fast + slow burn, per the SRE
  workbook).
