# Graphiti MCP runbook (OP-901)

**Status**: Deployment wiring for Sprint F F3. Spec source:
`docs/audit/2026-05-11-sprint-c-readiness-for-sprint-f.md` §1-2 and
`docs/research/2026-05-11-3d-memory-cross-task-awareness-literature-scan.md`.

## 1. Components

| Component | Path | Contract |
|---|---|---|
| Graphiti service | `docker-compose.yml` `graphiti` profile | Runs `zepai/graphiti:latest` on container port 8000. |
| Public ingress | `deploy/caddy/mcp-graphiti.caddy` | `mcp-graphiti.sora.services` with Caddy-managed TLS. |
| JIRA ingestion | `backend/integrations/jira_to_graphiti_webhook.py` | JIRA webhook payload -> Graphiti `/ingest/jira`. |
| Gerrit ingestion | `scripts/bridge_gerrit_to_graphiti.py` | Gerrit stream-events -> Graphiti `/ingest/gerrit`. |
| Runner query | `backend/agents/mcp_integration.py` | Read-only `mcp_graphiti` MCP tools only. |

## 2. Initial bring-up

```bash
export OMNISIGHT_MCP_GRAPHITI_TOKEN='rotate-me-long-random'
export OPENAI_API_KEY='...'
docker compose --profile graphiti up -d graphiti
docker compose ps graphiti
```

Install the Caddy snippet on the host:

```bash
sudo cp deploy/caddy/mcp-graphiti.caddy /etc/caddy/conf.d/
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

DNS must point `mcp-graphiti.sora.services` at the Caddy host. Verify:

```bash
dig +short mcp-graphiti.sora.services
curl -fsS https://mcp-graphiti.sora.services/api/v1/health
```

The healthcheck is green only when `GET /api/v1/health` returns 200.

## 3. Auth and rotation

Graphiti ingestion and runner MCP calls use:

| Variable | Purpose |
|---|---|
| `OMNISIGHT_MCP_GRAPHITI_TOKEN` | Bearer token sent to Graphiti. |
| `OMNISIGHT_MCP_GRAPHITI_URL` | Optional override, defaults to `https://mcp-graphiti.sora.services`. |
| `OMNISIGHT_JIRA_WEBHOOK_SECRET` | Inbound JIRA webhook bearer secret. |

Rotation procedure:

1. Generate a new 32+ byte random token.
2. Update `OMNISIGHT_MCP_GRAPHITI_TOKEN` in the host secret store.
3. Restart the Graphiti container and the JIRA/Gerrit ingestion bridge.
4. Re-run the bad-token and good-token smoke checks.
5. Remove the previous token from the secret store.

Never commit token values. Use only environment files or the host secret
manager.

## 4. JIRA changelog ingestion

Configure JIRA Automation or Webhooks to POST issue changelog events to
the backend handler that calls
`backend.integrations.jira_to_graphiti_webhook.handle_jira_webhook`.
The adapter:

1. Verifies `Authorization: Bearer $OMNISIGHT_JIRA_WEBHOOK_SECRET`.
2. Normalizes the issue key, status, actor, and changelog items.
3. POSTs the normalized event to
   `$OMNISIGHT_MCP_GRAPHITI_URL/ingest/jira`.

Atlassian handles webhook retries when the OmniSight endpoint is
unreachable; OmniSight does not add an additional retry loop.

## 5. Gerrit stream-events ingestion

Run the bridge on the host that has Gerrit SSH credentials:

```bash
export OMNISIGHT_GERRIT_SSH_HOST=sora.services
export OMNISIGHT_GERRIT_SSH_PORT=29418
export OMNISIGHT_GERRIT_USER=codex-bot
export OMNISIGHT_GERRIT_KEY_PATH=$HOME/.config/omnisight/gerrit-codex-bot-ed25519
python scripts/bridge_gerrit_to_graphiti.py
```

The bridge forwards only:

* `patchset-created`
* `change-merged`
* `comment-added` events with `Code-Review +2`

Other stream events are intentionally ignored.

## 6. Read-only MCP contract

Runner-side MCP registration allows only query-shaped Graphiti tool
methods: `get*`, `find*`, `query*`, and `list*`. Write-shaped names such
as `create*`, `update*`, and `delete*` are rejected before MCP dispatch.

Expected audit meaning:

| Error | Meaning |
|---|---|
| `GraphitiWriteRefused` | Governance guard fired; correct behavior, audit only. |
| `GraphitiAuthRejected` | Token mismatch; refuse all MCP calls and alert. |
| `GraphitiQueryTimeout` | Return empty temporal context and log. |

## 7. Recovery and rollback

Graphiti owns its backing store. Until D15 backup automation is
available, recover corruption by replaying from source systems:

1. Stop the Graphiti container.
2. Snapshot or move aside Graphiti's service data.
3. Start a clean Graphiti container.
4. Re-send JIRA changelog events from JIRA export or automation replay.
5. Re-run `scripts/bridge_gerrit_to_graphiti.py --once-file <events.jsonl>`
   for saved Gerrit stream-events.

On `GraphitiContainerStartFailed`, alert, retry `docker compose --profile
graphiti up -d graphiti` once, and stop if it fails again.

## 8. Verification checklist

```bash
docker compose --profile graphiti up -d graphiti
docker compose ps graphiti
curl -fsS https://mcp-graphiti.sora.services/api/v1/health
python -m backend.tests.test_graphiti_deployment
backend/.venv/bin/pytest backend/tests/test_graphiti_deployment.py
```

Record the exact test output and healthcheck response in the JIRA AC
verification comment.
