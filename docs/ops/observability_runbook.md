# OmniSight HA-07 Observability Runbook (G7 #4 / HA-07 bundle closure)

> **G7 #4 (TODO row 1389).** Fourth and final deliverable of the G7
> HA-07 bucket — the single-entry-point **aggregator** that points
> the on-call operator at every other G7 artefact in the order it
> needs to be read during an incident. Lives alongside
> [`deploy/observability/README.md`](../../deploy/observability/README.md),
> the deploy-side quickstart that tells an SRE how to load the
> dashboard + alert rules into a freshly-provisioned
> Grafana/Prometheus pair.
>
> If you are the on-call operator paged at 3am, **start at §1**.
> Every other section is reference.

---

## 0. What this runbook is (and is not)

This runbook is the **bundle closure** for the G7 bucket. It does
**not** restate the metric definitions, the dashboard panel layout,
or the alert rule YAML — those have canonical homes and drifting
them across two docs is the single most common source of stale
runbooks. What this doc does is:

1. Give the operator a **decision tree** that maps a firing alert
   to the panel to open and the remediation path (§1).
2. Declare the **contract** each sibling artefact is pinned to (§2)
   so the operator knows which doc is authoritative for which
   piece of the observability contract.
3. Document the **alert → dashboard mapping** (§3) so the operator
   paged at 3am can land on the correct panel in one click.
4. Record the **relationships** between the G7 artefacts (§4) so a
   new on-call can read this doc end-to-end and know where every
   part of the HA-07 promise is implemented.

What this doc is **NOT**:

* **Not** a replacement for any sibling artefact. It links; it
  does not duplicate.
* **Not** the metric definitions — those are
  [`backend/metrics.py`](../../backend/metrics.py) +
  [`backend/ha_observability.py`](../../backend/ha_observability.py)
  (G7 #1).
* **Not** the Grafana dashboard JSON — that is
  [`deploy/observability/grafana/ha.json`](../../deploy/observability/grafana/ha.json)
  (G7 #2).
* **Not** the Prometheus alert rule YAML — that is
  [`deploy/observability/prometheus/alerts.yml`](../../deploy/observability/prometheus/alerts.yml)
  (G7 #3).
* **Not** the deploy/import quickstart — that is
  [`deploy/observability/README.md`](../../deploy/observability/README.md)
  (G7 #4 companion).

---

## 1. Incident decision tree (read this first)

Paged. Which alert fired? Follow the branch, open the dashboard
panel, come back here when the remediation path is clear.

```
Alert fired — which one?
│
├── OmniSightReplicaLagHigh (severity=warning, subsystem=ha_replication)
│   → Dashboard Row 3 "Replica lag — omnisight_replica_lag_seconds"
│   → Threshold 10 s aligned with alert expr `> 10`.
│     Budget: 5-min RPO per docs/ops/dr_rto_rpo.md §3 —
│     10 s = 3.3% of budget, so this is an early-warning page,
│     not a breach. You have time to diagnose before failover
│     becomes lossy.
│   → Investigate: replica I/O, network, long-running
│     transactions on the primary. If lag climbs past 30 s,
│     treat as pending RPO breach and follow
│     docs/ops/dr_manual_failover.md §2 (DB primary failover).
│
├── OmniSightRollingDeploy5xxRateHigh (severity=critical,
│   subsystem=ha_rolling_deploy)
│   → Dashboard Row 2 "Rolling deploy 5xx" — two panels side by
│     side: the in-process gauge (fast read) and the
│     status_class breakdown (is it 500s or 502s dominating?).
│   → Threshold 0.01 (1%) aligned with alert expr `> 0.01`
│     against the G7 #1 60-second in-process ring-buffer gauge.
│   → Remediation: pause the rolling deploy. If 500s dominate,
│     the new revision is regressing — roll back via
│     `scripts/deploy.sh --strategy rolling --rollback`.
│     If 502s dominate, an upstream dependency is unhealthy —
│     check the reverse-proxy pool (docs/ops/dr_manual_failover.md
│     §3) before declaring a code regression.
│
├── OmniSightBackendInstanceDown (severity=critical,
│   subsystem=ha_availability)
│   → Dashboard Row 1 "Availability — omnisight_backend_instance_up"
│     — the stat panel shows count of serving replicas, and the
│     timeseries shows which replica flipped to 0.
│   → During an active rolling deploy a single transient 0 is
│     normal (`for: 2m` is calibrated to ride out graceful
│     drain). Persisting past 2 min means the replica is stuck
│     or crashed. Evict the pod / restart the systemd unit and
│     confirm the replica re-registers as `instance_up == 1`.
│
└── Something else (dashboard blank, metrics missing)
    → See §3 below (alert → dashboard panel mapping) + §5
      (common failure modes: metrics disappear, panel empty,
      alert doesn't fire).
```

The decision tree above is the **only** mandatory read before
acting. Every branch terminates at a dashboard panel that surfaces
the live state and a sibling doc that owns the step-by-step.

---

## 2. Contract pins — which artefact owns which piece of the promise

The G7 bucket ships a single observability promise:

> **OmniSight backend HA state (replica availability, replication
> lag, rolling-deploy error share) is exposed as Prometheus
> metrics, visualised on a single Grafana dashboard, and paged on
> breaching three thresholds that the operator sees mirrored on
> that same dashboard.**

That sentence is implemented by four artefacts. Each artefact owns
exactly one contract line — if you are unsure where to look, this
table is the index.

| Contract | Owning artefact | Row | CI contract test |
|---|---|---|---|
| Four Prometheus metric families expose HA state | [`backend/metrics.py`](../../backend/metrics.py) + [`backend/ha_observability.py`](../../backend/ha_observability.py) | G7 #1 / row 1386 | [`backend/tests/test_ha_observability_g7_1.py`](../../backend/tests/test_ha_observability_g7_1.py) |
| Grafana dashboard renders those metrics in 4 rows × 2 views each | [`deploy/observability/grafana/ha.json`](../../deploy/observability/grafana/ha.json) | G7 #2 / row 1387 | [`backend/tests/test_ha_grafana_dashboard_g7_2.py`](../../backend/tests/test_ha_grafana_dashboard_g7_2.py) |
| Prometheus alert rules fire on the three thresholds pinned in the TODO headline | [`deploy/observability/prometheus/alerts.yml`](../../deploy/observability/prometheus/alerts.yml) | G7 #3 / row 1388 | [`backend/tests/test_ha_alert_rules_g7_3.py`](../../backend/tests/test_ha_alert_rules_g7_3.py) |
| Bundle aggregator + deploy-side quickstart | **this doc** + [`deploy/observability/README.md`](../../deploy/observability/README.md) | G7 #4 / row 1389 | [`backend/tests/test_ha_bundle_closure_g7_4.py`](../../backend/tests/test_ha_bundle_closure_g7_4.py) |

If a reader is ever uncertain "is THIS doc the authoritative source
for X?" — the answer is in the table above, not in section
headers.

The pins themselves:

* **Replica-lag threshold `10 s`** is pinned in
  [`deploy/observability/prometheus/alerts.yml`](../../deploy/observability/prometheus/alerts.yml)
  (`OmniSightReplicaLagHigh` expr `> 10`) and mirrored on the
  Grafana panel threshold step at value 10. This runbook **cites**
  both; it does not redefine either.
* **5xx-rate threshold `0.01` (1%)** is pinned in
  [`deploy/observability/prometheus/alerts.yml`](../../deploy/observability/prometheus/alerts.yml)
  (`OmniSightRollingDeploy5xxRateHigh` expr `> 0.01`) and mirrored
  on the Grafana panel threshold step at value 0.01.
* **Every alert uses `for: 2m`** — four Prometheus scrape intervals
  (15 s scrape) is enough to ride out single-scrape noise and
  rolling-deploy graceful-drain flaps. Any edit to this value
  must land in the YAML, not here.
* **The four metric names** — `omnisight_backend_instance_up`,
  `omnisight_rolling_deploy_5xx_rate`,
  `omnisight_replica_lag_seconds`,
  `omnisight_readyz_latency_seconds` — are owned by
  [`backend/metrics.py`](../../backend/metrics.py). Rename lands
  there first; dashboard + alerts + this doc follow in the same
  commit.

---

## 3. Alert → dashboard panel mapping (fast lookup)

On-call opens this table first after the page arrives. It is the
one lookup that saves the 3am operator from re-deriving which
panel to open from the alert name.

| Alert name | Panel row on `deploy/observability/grafana/ha.json` | Metric rendered | Threshold mirror |
|---|---|---|---|
| `OmniSightReplicaLagHigh` | Row 3 — `omnisight_replica_lag_seconds` | replica streaming lag (sec) per replica | `10` s threshold step |
| `OmniSightRollingDeploy5xxRateHigh` | Row 2 — `omnisight_rolling_deploy_5xx_rate` + `omnisight_rolling_deploy_responses_total` status_class breakdown | in-process 60 s gauge + rate-of-counter | `0.01` threshold step |
| `OmniSightBackendInstanceDown` | Row 1 — `omnisight_backend_instance_up` | per-replica 1=serving / 0=draining | `red < 1`, `green ≥ 2` stat step |

The dashboard uid `omnisight-ha-07` is stable across re-imports;
deep-link directly to a panel from a page by appending
`?viewPanel=<id>` to the dashboard URL. Panel IDs are pinned in
[`backend/tests/test_ha_grafana_dashboard_g7_2.py`](../../backend/tests/test_ha_grafana_dashboard_g7_2.py)
`TestPanelStructure::test_every_panel_has_unique_id`.

---

## 4. Artefact relationship map (end-to-end)

For a new on-call reading this doc end-to-end, the data flow
through the four G7 artefacts is:

```
  HTTP request
     │
     ▼
  [backend/main.py lifespan]
     │   mark_instance_up() → omnisight_backend_instance_up = 1
     │
     ├─► [backend/ha_observability.py middleware]
     │        record_http_response(status_code) → increments
     │        omnisight_rolling_deploy_responses_total{status_class}
     │        and recomputes the 60 s ring-buffer 5xx rate gauge.
     │
     └─► [backend/routers/health.py::_readyz_handler]
              observe_readyz_latency(outcome) → histogram bucket.

  [backend/pg_ha sampler]
     │   update_replica_lag(application_name, seconds)
     │        → omnisight_replica_lag_seconds

  /metrics endpoint (prometheus_client ASGI)
     │
     ▼
  Prometheus scrape (default 15 s)
     │
     ├─► alert rule evaluation  ── fires ──► Alertmanager
     │     (deploy/observability/prometheus/alerts.yml)
     │
     └─► Grafana dashboard query
           (deploy/observability/grafana/ha.json)
```

The invariant across all four artefacts is that **one metric name
appears in exactly one place** — `backend/metrics.py` owns the
`Gauge/Counter/Histogram` definition, `ha_observability.py` owns
the write path, the Grafana dashboard owns the read path, and the
alert YAML owns the threshold. A rename is a four-artefact patch.

---

## 5. Common failure modes (observability-of-observability)

Every observability stack develops meta-failures. These are the
three that have shown up before on similar OmniSight bucketsand
where to look first.

### 5.1 Dashboard loads but every panel says "No data"

Root cause is almost always the datasource variable. The dashboard
pins `${datasource}` as a template variable (contract:
[`backend/tests/test_ha_grafana_dashboard_g7_2.py`](../../backend/tests/test_ha_grafana_dashboard_g7_2.py)
`TestTemplatingVariables`). If the Grafana instance doesn't have a
Prometheus datasource configured, or the variable picker is empty,
no panel can query. Fix: configure a Prometheus datasource, refresh
the variable picker.

### 5.2 Alert fires but dashboard shows green

The G7 #2 panel threshold steps are pinned to the same literal
values as the alert rule expressions (10 / 0.01). A green panel
during a page means the threshold step was silently edited away
from the alert value. The cross-alignment is contract-tested in
[`backend/tests/test_ha_alert_rules_g7_3.py`](../../backend/tests/test_ha_alert_rules_g7_3.py)
`TestDashboardThresholdAlignment`. Run that test locally — the
failing assertion will name the mismatched number.

### 5.3 `omnisight_rolling_deploy_5xx_rate` is 0 but 5xx counter is climbing

The gauge is the in-process 60 s ring-buffer scalar; the counter
is the source-of-truth cumulative count. Dividing the counter's
1-minute rate should give a number close to the gauge. A large
drift means the `backend/ha_observability.py` ring-buffer
coalesce logic has regressed (contract:
[`backend/tests/test_ha_observability_g7_1.py`](../../backend/tests/test_ha_observability_g7_1.py)
section D "rolling 5xx tracker window boundary"). Re-run section
D; the failing assertion will point at the per-second bucket
arithmetic.

---

## 6. Scope fence — what belongs here vs sibling docs

* **RTO / RPO minutes** (15 min / 5 min) are cited in §1 decision
  tree for context, but are **owned** by
  [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) (G6 #2). Do not
  redefine them here.
* **`pg_ctl promote` step-by-step** for DB primary failover is
  **owned** by [`docs/ops/dr_manual_failover.md`](dr_manual_failover.md)
  §2 (G6 #3). This runbook names the trigger condition, not the
  command.
* **Alert rule PromQL expressions** are **owned** by
  [`deploy/observability/prometheus/alerts.yml`](../../deploy/observability/prometheus/alerts.yml)
  (G7 #3). This runbook references the alert name + threshold
  number but does not inline the full `expr:` block — drift-prone.
* **Dashboard panel JSON / templating variables** are **owned** by
  [`deploy/observability/grafana/ha.json`](../../deploy/observability/grafana/ha.json)
  (G7 #2). This runbook names the row + panel title but does not
  inline JSON.
* **Deploy / import commands** (`kubectl apply`, `grafonnet`,
  `promtool`) are **owned** by
  [`deploy/observability/README.md`](../../deploy/observability/README.md)
  (G7 #4 companion). This runbook is for the incident path;
  that README is for the deploy path.

---

## 7. What ships next

G7 is **closed** by this row (1389). The next HA-adjacent bucket
on the roadmap (see [`TODO.md`](../../TODO.md) §H "Host-aware
Coordinator") is unrelated to HA-07 observability and will not
extend this runbook. Any future HA signal (eg cache hit rate,
background job lag) that wants to page on-call lands in a new
G8+ row with its own metric definition, dashboard panel, alert
rule, and — if the signal has enough operator surface to justify
a third doc — its own runbook entry. This runbook aggregates **G7
only**.
