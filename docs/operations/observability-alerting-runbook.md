# Observability alerting runbook — Alertmanager (OP-2563)

Alertmanager (`prom/alertmanager:v0.27.0`, compose service `alertmanager`,
`observability` profile, port `:9093`) receives the Phase S anti-hollow
alerts (`ProjectStateStructuralHollow` / `ProjectStateHalfHollow` /
`ProjectStateNoTraffic` / `ProjectStateLatencyHigh`, from
`deploy/observability/prometheus/project_state_health.yml`) via the
`alerting:` block in `configs/prometheus.yml`.

Routing lives in `configs/alertmanager.yml`: alerts group by
`["alertname", "severity"]`; `severity=critical` routes to the `critical`
receiver, everything else to `default`. **Both receivers ship as name-only
blackholes** — routing works, delivery goes nowhere until you activate a
real channel.

## Activate a real paging channel

1. Edit `configs/alertmanager.yml`: under the receiver you want
   (`critical` for paging, `default` for the rest), uncomment ONE of the
   template blocks (`webhook_configs:` / `slack_configs:` /
   `email_configs:`) and fill in the placeholders.
2. Supply the secret (webhook URL, Slack URL, SMTP password) via your
   operator secret mechanism at deploy time. **Never commit the secret
   or real URL** (CLAUDE.md L1) — the config with a live channel stays
   host-local, only the commented templates are committed.
3. Restart / start the service:

   ```bash
   docker compose -f docker-compose.prod.yml --profile observability up -d alertmanager
   ```

## Verify routing

```bash
# Alertmanager is healthy
curl -s http://localhost:9093/-/healthy

# Prometheus sees it as an active alertmanager
curl -s http://localhost:9090/api/v1/alertmanagers | jq '.data.activeAlertmanagers'

# Alerts currently held by Alertmanager (routing proof)
curl -s http://localhost:9093/api/v2/alerts | jq '.[].labels'

# Or with amtool
amtool alert query --alertmanager.url=http://localhost:9093
```

To exercise the path end-to-end without waiting for a real hollow event,
hand-inject a test alert:

```bash
amtool alert add TestAlert severity=critical --alertmanager.url=http://localhost:9093
curl -s http://localhost:9093/api/v2/alerts | jq '.[] | select(.labels.alertname=="TestAlert")'
```

Config validation before restart: `amtool check-config configs/alertmanager.yml`.
