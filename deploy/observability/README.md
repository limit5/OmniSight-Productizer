# OmniSight HA-07 Observability Bundle (G7 #4 / row 1389)

This directory ships the deploy-side half of the HA-07
observability bundle:

* [`grafana/ha.json`](grafana/ha.json) — Grafana 10 dashboard
  (uid `omnisight-ha-07`) covering the four HA signal families.
  Owner: G7 #2 / [`TODO.md`](../../TODO.md) row 1387.
* [`prometheus/alerts.yml`](prometheus/alerts.yml) — Prometheus
  alert rules group `omnisight_ha_07` with three rules
  (`OmniSightReplicaLagHigh` / `OmniSightRollingDeploy5xxRateHigh`
  / `OmniSightBackendInstanceDown`). Owner: G7 #3 / row 1388.

The operator-facing runbook for these artefacts is
[`docs/ops/observability_runbook.md`](../../docs/ops/observability_runbook.md)
(G7 #4 / row 1389). **If you are on-call and the page just fired,
go there first; this README is for SREs deploying or re-importing
the bundle.**

---

## 1. What gets scraped

Prometheus must scrape the OmniSight backend `/metrics` endpoint.
The four metric families exposed by G7 #1
([`backend/metrics.py`](../../backend/metrics.py) +
[`backend/ha_observability.py`](../../backend/ha_observability.py))
are:

| Metric | Type | Labels | Source |
|---|---|---|---|
| `omnisight_backend_instance_up` | Gauge | `instance_id` | `mark_instance_up()` / `mark_instance_down()` on FastAPI lifespan. `1` = serving, `0` = draining/down. |
| `omnisight_rolling_deploy_responses_total` | Counter | `status_class` ∈ `2xx`/`3xx`/`4xx`/`5xx` | HTTP middleware `record_http_response(status_code)`. Source of truth for PromQL `rate()` queries. |
| `omnisight_rolling_deploy_5xx_rate` | Gauge | — | In-process 60-second ring-buffer scalar (0..1). Convenience for alert rules that would rather not write rate-of-rate PromQL. |
| `omnisight_replica_lag_seconds` | Gauge | `replica` | `update_replica_lag(application_name, seconds)` from the pg_ha sampler. Negative values are clamped to 0. |
| `omnisight_readyz_latency_seconds` | Histogram | `outcome` ∈ `ready`/`not_ready`/`draining` | `/readyz` probe wall-clock. Buckets `0.005..5 s`. |

Minimal `prometheus.yml` snippet:

```yaml
scrape_configs:
  - job_name: omnisight-backend
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets:
          - backend-a:8000
          - backend-b:8000
```

The alert rule `for: 2m` duration is calibrated for a 15 s scrape
interval (4 scrape intervals = 1 minute of noise-filtering, with
headroom). A longer scrape interval (> 30 s) would require
re-tuning the `for:` durations — see
[`docs/ops/observability_runbook.md`](../../docs/ops/observability_runbook.md)
§2 "Contract pins".

---

## 2. Loading the alert rules into Prometheus

Add the rules file to the top-level `rule_files:` list in
`prometheus.yml`. The file ships one group (`omnisight_ha_07`)
with three alert rules and no recording rules:

```yaml
rule_files:
  - /etc/prometheus/omnisight/alerts.yml   # = deploy/observability/prometheus/alerts.yml
```

**Validate syntax before reloading Prometheus:**

```bash
promtool check rules deploy/observability/prometheus/alerts.yml
# Checking deploy/observability/prometheus/alerts.yml
#   SUCCESS: 3 rules found
```

**Hot reload** (signal-based; or re-deploy the Prometheus pod):

```bash
curl -X POST http://prometheus:9090/-/reload
```

Alertmanager routes on the `severity` label; the three rules ship
`severity: warning` (replica lag) and `severity: critical` (5xx
rate, instance down). Route those severities to the on-call
receiver — the G7 #3 test
[`backend/tests/test_ha_alert_rules_g7_3.py`](../../backend/tests/test_ha_alert_rules_g7_3.py)
(`TestRuleHygiene::test_rule_has_severity_label`) pins the
whitelist `{info, warning, critical}` so a silent edit to
`emergency` would fail CI before it reaches Alertmanager.

---

## 3. Importing the Grafana dashboard

The dashboard is committed as an **as-code** JSON with a stable
uid (`omnisight-ha-07`) so re-imports replace the same object
instead of forking a new copy each time.

### 3.1 Grafana UI (Dashboards → Import)

1. Navigate to **Dashboards → Import**.
2. Upload `deploy/observability/grafana/ha.json` (or paste its
   contents).
3. When prompted, pick your Prometheus datasource as the value for
   the `datasource` template variable. Every panel references
   `${datasource}` — hard-coding a datasource uid is explicitly
   prevented by the G7 #2 contract test
   (`TestEveryPanelUsesDatasourceVariable`).
4. Save. The dashboard will appear at `d/omnisight-ha-07/`.

### 3.2 Grafana API (CI / CD)

```bash
curl -X POST \
  -H "Authorization: Bearer $GRAFANA_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"dashboard\": $(cat deploy/observability/grafana/ha.json), \"overwrite\": true}" \
  https://grafana.internal/api/dashboards/db
```

The `overwrite: true` flag is safe because the dashboard has a
stable uid — the API matches on uid first, then on title.

### 3.3 Template variables to set per environment

| Variable | Query | Typical value |
|---|---|---|
| `datasource` | Prometheus datasource picker | The Prometheus instance for this environment. |
| `instance_id` | `label_values(omnisight_backend_instance_up, instance_id)` | `All` (multi-value, shows every replica). |
| `replica` | `label_values(omnisight_replica_lag_seconds, replica)` | `All` (multi-value, shows every replication stream). |

The `instance_id` and `replica` variables are multi-value with an
`All` default — a fresh import immediately shows every replica.

---

## 4. Deployment smoke test

After loading both files, confirm the bundle is wired end-to-end:

```bash
# 1. Prometheus is scraping the backend
curl -s http://prometheus:9090/api/v1/query?query=omnisight_backend_instance_up \
  | jq '.data.result | length'
# Expect: number of backend replicas (>= 1).

# 2. Alert rules are loaded
curl -s http://prometheus:9090/api/v1/rules \
  | jq '.data.groups[] | select(.name == "omnisight_ha_07") | .rules | length'
# Expect: 3.

# 3. Dashboard resolves the datasource variable
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  https://grafana.internal/api/dashboards/uid/omnisight-ha-07 \
  | jq '.dashboard.templating.list | map(.name)'
# Expect: ["datasource", "instance_id", "replica"]
```

If any of the three checks fails, see
[`docs/ops/observability_runbook.md`](../../docs/ops/observability_runbook.md)
§5 "Common failure modes".

---

## 5. Contract surface (why this bundle is small)

The G7 bucket ships **exactly two artefacts on the deploy side**:
one dashboard JSON, one alert rules YAML. Anything else lives in
the runbook at `docs/ops/observability_runbook.md` or in the
metric source at `backend/metrics.py` / `backend/ha_observability.py`.

Specifically, this directory does **not** contain:

* **Alert routing / Alertmanager config** — lives in your
  Alertmanager deployment, not here. The alert rules only declare
  `severity` + `subsystem` labels; where `severity=critical` goes
  is a deploy-environment decision.
* **Dashboard provisioning manifests** — Grafana's
  `provisioning/dashboards/*.yaml` is a deploy-level concern. This
  README documents both UI-import and API-import so either path
  works; the as-code JSON is the only artefact we commit.
* **Recording rules** — forbidden by the G7 #3 contract test
  (`TestScopeDisciplineSiblingRows::test_alert_file_does_not_redefine_metrics`).
  `backend/metrics.py` is the single source of truth for metric
  shape; a recording rule here would split that truth.
* **Per-metric SLO / error-budget definitions** — not in scope for
  HA-07. Add them in a follow-up bucket with its own row.

Keeping this surface small is the whole point of the bundle:
every artefact here has exactly one owner, one contract test, and
one canonical path. Drift between them is detected on CI, not in
production at 3 am.
