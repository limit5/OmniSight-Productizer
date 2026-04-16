# Prometheus integration (O9 #272)

`/api/v1/metrics` is the unified Prometheus exporter for the OmniSight
backend. It exposes every counter / gauge / histogram registered in
`backend/metrics.py`, including the O1 (dist-lock), O2 (queue-backend),
O3 (worker-pool), and O6 (merger-agent) families.

## Scrape config

```yaml
scrape_configs:
  - job_name: omnisight
    metrics_path: /api/v1/metrics
    static_configs:
      - targets: ["omnisight-backend:8080"]
```

## Alert rules

Load `orchestration_alerts.rules.yml` from this directory:

```yaml
rule_files:
  - /etc/prometheus/rules/orchestration_alerts.rules.yml
```

Routing labels used by the rules (wire these to Alertmanager):

| Subsystem                   | severity     | Page? |
| --------------------------- | ------------ | ----- |
| `orchestration_queue`       | warning      | No    |
| `orchestration_queue` (P0)  | critical     | Yes   |
| `orchestration_locks`       | warning      | No    |
| `orchestration_locks` (DL)  | critical     | Yes   |
| `orchestration_merger`      | warning      | No    |
| `orchestration_dual_sign`   | warning/info | No    |

`runbook_url` annotations point at internal docs; rewrite for your
deployment.
