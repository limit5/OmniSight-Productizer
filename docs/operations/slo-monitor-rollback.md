# OP-772 SLO Monitor + Auto-Rollback

Production deploys use `config/slos.yaml` as the SLO source of truth:

- error rate `< 0.5%`
- p95 latency `< 500 ms`
- success rate `> 99.5%`
- monthly error budget `1 h`

`deploy/systemd/omnisight-slo-monitor.service` runs `python -m backend.slo_monitor`
as a persistent worker. The worker samples Prometheus every 30 seconds and
keeps running through the deploy and for the post-deploy hour. A breach is
three consecutive 30-second windows above threshold for the same route metric.

When a breach fires, the monitor posts a medium operator notification, checks
that the previous backend and frontend image tags exist in GHCR, and then runs:

```bash
OMNISIGHT_IMAGE_TAG=<previous-tag> \
docker compose -f docker-compose.prod.yml up -d --no-deps backend-a backend-b frontend
```

Rollback is bounded by `rollback_max_seconds: 300`. If either previous image is
missing from the registry, the monitor posts a high-severity alert and halts
without changing the running services; the operator must intervene manually.
