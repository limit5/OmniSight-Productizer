# OP-883 SLO Monitor + Auto-Rollback

Production deploys use `config/slo_thresholds.yaml` as the SLO source of truth:

- error rate `< 1%`
- p95 latency `< 500 ms`
- sustained breach window `> 2 min`
- rollback cooldown `10 min`

`deploy/systemd/omnisight-slo-monitor.service` runs
`python -m backend.orchestrator.slo_monitor` from `/home/user/sora-bridge`
as a persistent worker. The worker samples Prometheus every 30 seconds and
keeps running continuously. A breach is sustained until the configured
`breach_sustain_seconds` window elapses.

When a breach fires, the monitor posts a critical operator notification and
triggers the OP-883 orchestrator rollback. Because progressive canary is HOLD'd
(OP-931/932/933), `canary_state.json` is absent in the current production
topology and the monitor defaults to the full production rollback path.

```bash
python -m backend.orchestrator.slo_monitor
```

`backend/slo_monitor.py` remains only as a deprecated compatibility shim that
forwards to `backend.orchestrator.slo_monitor`.
