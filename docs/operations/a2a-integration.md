# A2A Integration - Operator Guide

> BP.A2A.13. Audience: operators registering external A2A agents,
> exposing OmniSight specialists to partners, and validating OAuth-style
> scopes for Agent-to-Agent traffic.

## Overview

```
[1 Publish] OmniSight exposes /.well-known/agent.json AgentCard
              |
[2 Invoke ] External callers POST /a2a/invoke/{agent_name}
              |
[3 Register] Operators add remote peers under /admin/external-agents
              |
[4 Call   ] OmniSight outbound nodes use A2AClient, AgentCard cache,
            retry, circuit breaker, and optional SSE streaming
```

BP.A2A is the cross-process and cross-vendor agent edge. Internal
specialist routing still flows through the existing graph state; A2A is
used when OmniSight calls a remote agent, or when a partner calls an
OmniSight specialist through the public protocol surface.

## Prerequisites

| Requirement | Production default | Notes |
|---|---|---|
| Auth mode | `OMNISIGHT_AUTH_MODE=strict` | Do not expose A2A discovery or invoke from `open` mode |
| Public origin | Reverse proxy sets forwarded headers | `x-forwarded-proto` and `x-forwarded-host` are used in AgentCard URLs |
| API prefix | `/api/v1` | External-agent registry APIs are prefixed; A2A discovery/invoke endpoints are not |
| External registry store | Inject durable `external_agent_registry` | The default in-memory store is dev/test only and per worker |
| Rate limit backend | Redis-backed `backend.rate_limit` | In-memory fallback is intentionally per worker for local dev/tests |
| Audit backend | Existing audit chain | A2A discovery and invoke events write audit metadata when audit is available |

## Expose OmniSight Agents

Publish the public AgentCard at the root well-known path:

```bash
curl -sS https://api.omnisight.example.com/.well-known/agent.json \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" | jq .
```

Expected fields:

- `protocol` is `a2a`.
- `protocol_version` is `0.3.0`.
- `auth.scopes` includes `a2a:discover:*` and `a2a:invoke:*`.
- `endpoints.invoke_url_template` ends with `/a2a/invoke/{agent_name}`.
- `streaming.transport` is `sse`.
- `capabilities[]` includes OmniSight guilds such as `hal`, `bsp`,
  `backend`, `frontend`, `sre`, plus runtime specialists such as
  `orchestrator` and `hd`.

If OmniSight is behind Cloudflare, Caddy, or another reverse proxy, verify
the AgentCard uses the external hostname, not the private backend URL:

```bash
curl -sS http://127.0.0.1:8000/.well-known/agent.json \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" \
  -H "x-forwarded-proto: https" \
  -H "x-forwarded-host: api.omnisight.example.com" \
  | jq -r '.url, .endpoints.invoke_url_template'
```

## Invoke OmniSight Agents

Synchronous JSON invocation:

```bash
curl -sS https://api.omnisight.example.com/a2a/invoke/hal \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Inspect this HAL bring-up failure and return next checks."}' \
  | jq .
```

Streaming invocation uses the same endpoint with `stream=true`:

```bash
curl -N https://api.omnisight.example.com/a2a/invoke/bsp?stream=true \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Review this device-tree overlay for boot blockers."}'
```

SSE events follow the AgentCard streaming contract:

| Event | Meaning |
|---|---|
| `task_submitted` | Request accepted and invocation id assigned |
| `task_working` | PEP decision created and graph execution started |
| `artifact_delta` | Partial answer or artifact chunk |
| `task_completed` | Final payload for a successful graph run |
| `task_failed` | Final payload for a failed graph run |

The invoke payload may supply `command`, `message`, `task`, string
`input`, or object `input` with `command`, `message`, `task`, `prompt`, or
`text`. The response includes `invocation_id`, `agent_name`, `status`,
`routed_to`, `answer`, `last_error`, `pep_decision_id`, `pep_action`,
`tool_results`, and `actions`.

## Provider-Scoped AgentCards

OmniSight also exposes provider-scoped AgentCards so the orchestrator can
route through A2A endpoint metadata instead of SDK classes. For manual
operator smoke, use a browser session or an admin key with broad scope until
the API-key endpoint-to-scope mapper covers provider-scoped A2A paths.

```bash
for provider in anthropic openai google xai groq deepseek together openrouter ollama; do
  curl -sS "https://api.omnisight.example.com/.well-known/a2a/providers/${provider}/agent.json" \
    -H "Authorization: Bearer $OMNISIGHT_ADMIN_TOKEN" \
    | jq -r '.provider + " " + (.capabilities | length | tostring)'
done
```

Provider-scoped invocation uses:

```bash
curl -sS https://api.omnisight.example.com/a2a/providers/openrouter/invoke/reviewer \
  -H "Authorization: Bearer $OMNISIGHT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Review this patch for behavioral regressions."}' \
  | jq .
```

Each provider capability includes `provider_id` and `model_spec`. Model
specs are generated from `configs/model_mapping.yaml`; changing that file
is reflected on the next AgentCard discovery after the per-worker mtime
cache reloads.

## OAuth Setup

OmniSight's A2A surface uses bearer authentication plus OAuth-style scopes.
For machine callers, create a scoped API key as an admin:

```bash
curl -sS https://api.omnisight.example.com/api/v1/api-keys \
  -H "Content-Type: application/json" \
  --cookie "omnisight_session=$SESSION_COOKIE; omnisight_csrf=$CSRF_COOKIE" \
  -H "X-CSRF-Token: $CSRF_TOKEN" \
  -d '{
    "name": "partner-a2a",
    "scopes": ["a2a:discover:*", "a2a:invoke:*"]
  }' | jq -r '.secret'
```

Use narrower scopes when a partner should only reach one specialist:

| Scope | Allows |
|---|---|
| `a2a:discover:*` | `/.well-known/agent.json` discovery |
| `a2a:discover:agent-card` | Root AgentCard discovery only |
| `a2a:invoke:*` | Any `/a2a/invoke/{agent_name}` call |
| `a2a:invoke:hal` | Only `/a2a/invoke/hal` |
| `a2a:invoke:bsp` | Only `/a2a/invoke/bsp` |

Provider-scoped A2A routes currently require session auth or a broader
admin key because the API-key scope mapper has not yet assigned
`/.well-known/a2a/providers/{provider_id}/agent.json` and
`/a2a/providers/{provider_id}/invoke/{agent_name}` to OAuth-style scope
strings.

Rotate and revoke A2A API keys through the existing API-key endpoints:

```bash
curl -X POST https://api.omnisight.example.com/api/v1/api-keys/$KEY_ID/rotate ...
curl -X POST https://api.omnisight.example.com/api/v1/api-keys/$KEY_ID/revoke ...
```

Do not store partner bearer tokens or OAuth refresh tokens in source code.
For outbound external agents, put the secret in the same production secret
path used for other runtime tokens, then register only the `token_ref`.

## Register External Agents

Use the Operations Console:

1. Open `/admin/external-agents`.
2. Select `Register`.
3. Fill `agent id`, `display name`, `base URL`, `remote agent name`,
   `auth mode`, optional `token ref`, tags, and capabilities.
4. Save, then verify the row shows the normalized base URL and
   `agent_card_url`.
5. Use the row action toggle as the operator kill-switch.

The same operation is available through the API:

```bash
curl -sS https://api.omnisight.example.com/api/v1/external-agents \
  -H "Content-Type: application/json" \
  --cookie "omnisight_session=$SESSION_COOKIE; omnisight_csrf=$CSRF_COOKIE" \
  -H "X-CSRF-Token: $CSRF_TOKEN" \
  -d '{
    "agent_id": "threat-intel",
    "display_name": "Threat Intel",
    "base_url": "https://partner.example.com",
    "agent_name": "intel",
    "description": "Partner A2A endpoint for CVE and IOC enrichment.",
    "auth_mode": "bearer",
    "token_ref": "secret:a2a-threat-intel",
    "enabled": true,
    "tags": ["secops", "intel"],
    "capabilities": ["cve_triage", "ioc_enrichment"],
    "config": {"timeout_s": 30}
  }' | jq .
```

Field rules:

| Field | Rule |
|---|---|
| `agent_id` | Lowercase slug; digits, dash, and underscore are allowed |
| `base_url` | Absolute `http` or `https` URL; trailing slash is removed |
| `agent_name` | Remote A2A agent slug invoked at `/a2a/invoke/{agent_name}` |
| `auth_mode` | `none`, `bearer`, or `oauth2` |
| `token_ref` | Required for `bearer` or `oauth2`; empty for `none` |
| `enabled` | Operator kill-switch; disabled endpoints cannot be invoked |

List and toggle endpoints:

```bash
curl -sS https://api.omnisight.example.com/api/v1/external-agents \
  --cookie "omnisight_session=$SESSION_COOKIE; omnisight_csrf=$CSRF_COOKIE" | jq .

curl -sS -X PATCH https://api.omnisight.example.com/api/v1/external-agents/threat-intel \
  -H "Content-Type: application/json" \
  --cookie "omnisight_session=$SESSION_COOKIE; omnisight_csrf=$CSRF_COOKIE" \
  -H "X-CSRF-Token: $CSRF_TOKEN" \
  -d '{"enabled": false}' | jq .
```

Before using a new remote peer in a workflow, fetch its AgentCard:

```bash
curl -sS https://partner.example.com/.well-known/agent.json \
  -H "Authorization: Bearer $PARTNER_A2A_TOKEN" | jq .
```

The outbound client caches AgentCards for one hour, retries transient 429
and 5xx responses, uses a per-tenant circuit breaker, sends
`X-Omnisight-Tenant-Id`, and supports SSE parsing for streaming calls.

## AgentCard Schema Reference

Root object:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | Currently `1.0.0` |
| `protocol` | string | Always `a2a` |
| `protocol_version` | string | Currently `0.3.0` |
| `name` | string | Public card name |
| `description` | string | Human-readable service description |
| `version` | string | Service version |
| `url` | string | Discovery URL |
| `provider` | string | `OmniSight` or provider display name |
| `endpoints` | object | Discovery, JSON invoke, and SSE invoke templates |
| `auth` | object | Bearer/OAuth-style auth contract |
| `streaming` | object | SSE support and event names |
| `protocol_capabilities` | object | Protocol feature flags |
| `capabilities` | array | Callable agent descriptors |
| `default_input_modes` | array | Defaults to `text/plain`, `application/json` |
| `default_output_modes` | array | Includes `text/event-stream` |

`endpoints`:

| Field | Example |
|---|---|
| `discovery_url` | `https://api.omnisight.example.com/.well-known/agent.json` |
| `invoke_url_template` | `https://api.omnisight.example.com/a2a/invoke/{agent_name}` |
| `stream_url_template` | `https://api.omnisight.example.com/a2a/invoke/{agent_name}?stream=true` |

`auth`:

| Field | Value |
|---|---|
| `scheme` | `oauth2` or `bearer` |
| `description` | PEP gateway OAuth bearer token required |
| `scopes` | `a2a:discover:*`, `a2a:invoke:*` |

`streaming`:

| Field | Value |
|---|---|
| `supported` | `true` |
| `transport` | `sse` |
| `content_type` | `text/event-stream` |
| `events` | `task_submitted`, `task_working`, `artifact_delta`, `task_completed`, `task_failed` |

`protocol_capabilities`:

| Field | Value |
|---|---|
| `streaming` | `true` |
| `push_notifications` | `false` |
| `state_transition_history` | `true` |

`capabilities[]`:

| Field | Type | Notes |
|---|---|---|
| `agent_name` | string | Lowercase slug used in invoke URL |
| `display_name` | string | UI/display label |
| `description` | string | Specialist capability summary |
| `source` | string | `guild`, `runtime_specialist`, `domain_specialist`, or `provider_specialist` |
| `endpoint_url` | string | JSON invoke URL |
| `stream_endpoint_url` | string | SSE invoke URL |
| `admitted_tiers` | array | Sandbox tiers admitted for that specialist |
| `input_modes` | array | Accepted MIME modes |
| `output_modes` | array | Returned MIME modes |
| `tags` | array | Discovery and routing tags |
| `provider_id` | string/null | Present for provider-scoped cards |
| `model_spec` | string/null | Provider/model routing metadata |

## Smoke Test

Run this after deployment or after changing `configs/model_mapping.yaml`:

```bash
set -euo pipefail
BASE=https://api.omnisight.example.com

curl -sS "$BASE/.well-known/agent.json" \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" \
  | jq -e '.protocol == "a2a" and (.capabilities | length > 0)'

curl -sS "$BASE/a2a/invoke/hal" \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"smoke test; return a short acknowledgement"}' \
  | jq -e '.invocation_id and .agent_name == "hal" and .pep_action'

curl -sS "$BASE/api/v1/external-agents" \
  --cookie "omnisight_session=$SESSION_COOKIE; omnisight_csrf=$CSRF_COOKIE" \
  | jq -e '.external_agents'
```

For streaming, confirm the first two event names are stable:

```bash
curl -N "$BASE/a2a/invoke/hal?stream=true" \
  -H "Authorization: Bearer $OMNISIGHT_A2A_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"streaming smoke test"}' | sed -n '1,8p'
```

Expected first events are `task_submitted` then `task_working`.

## Troubleshooting

| Symptom | Check |
|---|---|
| AgentCard shows `http://127.0.0.1` URLs | Proxy is not setting `x-forwarded-proto` and `x-forwarded-host` |
| `403 pep_denied` on invoke | PEP gateway denied the A2A operation; inspect `pep_rule` and `pep_reason` in the response |
| `429 a2a_rate_limited` | Tenant exceeded the per-AgentCard bucket; respect `Retry-After` |
| `422` on invoke | Payload did not include `command`, `message`, `task`, or text `input` |
| External registration rejects token | `token_ref` must be present for `bearer`/`oauth2` and empty for `none` |
| External row disappears across workers/restart | Production has not injected a durable external-agent registry store |
| Remote calls fail after repeated 429/5xx | Per-tenant A2A circuit is open; wait for breaker recovery or disable the endpoint |
| Provider AgentCard has unexpected model | Check `configs/model_mapping.yaml`, then re-fetch after the mtime cache reloads |

## Related

- `backend/a2a/agent_card.py` - AgentCard schema, capability descriptors, and provider cards
- `backend/routers/a2a_inbound.py` - discovery and inbound invoke routes
- `backend/a2a/client.py` - outbound HTTP/SSE client
- `backend/agents/external_agent_registry.py` - external A2A endpoint registry
- `backend/routers/external_agents.py` - operator API for external agent registration
- `app/admin/external-agents/page.tsx` - Operations Console registration UI
- `backend/tests/test_a2a_inbound.py` - inbound A2A route contract tests
- `backend/tests/test_a2a_outbound.py` - outbound client and registry contract tests
