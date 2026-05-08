# OP-771 Canary Rollout Runbook

Progressive canary replaces the D9 all-at-once blue-green cutover for
production promotions that need an SLO gate between traffic ramps.

## Stages

| Stage | Canary weight | Observe window | Advance rule |
|-------|---------------|----------------|--------------|
| 1 | 5% | 10 min | D11 SLO snapshot passes |
| 2 | 25% | 15 min | D11 SLO snapshot passes |
| 3 | 100% | immediate completion | D11 SLO snapshot passes |

Any SLO breach aborts the rollout and rewrites
`deploy/blue-green/canary-weighted.caddy` back to the stable color only.

## Caddy snippet

The controller writes:

```caddyfile
(canary_weighted_upstream_rp) {
	reverse_proxy {$OMNISIGHT_UPSTREAM_A:backend-a:8000} {$OMNISIGHT_UPSTREAM_B:backend-b:8001} {
		lb_policy weighted_round_robin 95 5
		health_uri /readyz
	}
}
```

Import this snippet in the production Caddyfile when running a canary.
The weights change to `75 25` at the 25% stage when green is the
canary, and the 100% stage collapses to a single canary upstream.

## Tenant pinning

`backend.canary_rollout.stable_canary_assignment()` hashes
`<salt>:<tenant_id>` with SHA-256 and maps the tenant to a 0-99 bucket.
The bucket is compared with the current canary percentage, so a tenant
that is in the 5% cohort remains in the canary cohort at 25% and 100%.

The backend exposes `GET /api/v1/canary-rollout/assignment/{tenant_id}`
for dashboard/operator inspection.

## Operator controls

All control endpoints require admin auth:

```bash
POST /api/v1/canary-rollout/start
POST /api/v1/canary-rollout/evaluate
POST /api/v1/canary-rollout/control
```

`/control` accepts `pause`, `resume`, `advance`, and `abort`. This is
the backend contract the D17 dashboard can call without changing the
rollout state format.

## Auto-abort

The D11 bridge is `RollingDeploySloMonitor`, which reads
`backend.ha_observability.current_5xx_rate()`. The default thresholds
match `docs/ops/slo.md`: error rate >= 0.5%, p95 > 1000 ms, or
availability < 99% aborts the rollout.
