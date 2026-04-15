# Cloudflare Tunnel Wizard

## Overview

The Cloudflare Tunnel Wizard automates the 4-step manual process of setting up a Cloudflare Tunnel for exposing OmniSight to the internet securely. Instead of running `cloudflared tunnel login`, `create`, `route dns`, and editing `config.yml` manually, users complete a 5-step UI wizard.

## Prerequisites

1. A Cloudflare account with at least one active zone (domain)
2. A Cloudflare API Token with the following permissions:
   - **Account → Cloudflare Tunnel → Edit**
   - **Zone → DNS → Edit**
   - **Account → Account Settings → Read**

### Creating the API Token

1. Go to [Cloudflare Dashboard → API Tokens](https://dash.cloudflare.com/profile/api-tokens)
2. Click **Create Token**
3. Use the **Custom Token** template
4. Add the three permissions listed above
5. Restrict the token to the specific account and zone you'll use
6. Copy the token — it's shown only once

## Wizard Steps

### Step 1 — API Token
Enter your Cloudflare API Token. The system validates it against the CF API and checks that all required permissions are present.

### Step 2 — Account & Zone
Select the Cloudflare account and zone (domain) where the tunnel will be created. Zones are filtered to active ones only.

### Step 3 — Hostnames
Configure the hostnames that will point to your OmniSight instance:
- Default: `omnisight.<zone>` and `api.omnisight.<zone>`
- Custom hostnames can be added or removed
- Tunnel name defaults to `omnisight`

### Step 4 — Review
Review all settings before provisioning. Shows the ingress target (`http://localhost:8000`).

### Step 5 — Provision
The system automatically:
1. Creates (or reuses) a named Cloudflare Tunnel
2. Configures tunnel ingress rules
3. Retrieves the connector token
4. Creates DNS CNAME records for each hostname

Real-time progress is streamed via SSE events.

## Connector Mode

The wizard uses **token mode** (`cloudflared tunnel run --token <T>`) instead of credentials files. This avoids file management complexity and works in both systemd and container environments.

### systemd Mode
If `cloudflared` is installed and a systemd unit exists, the service is managed via `systemctl`. A sudoers rule is required:

```
# /etc/sudoers.d/omnisight-cloudflared
omnisight ALL=(root) NOPASSWD: /usr/bin/systemctl start cloudflared.service, /usr/bin/systemctl stop cloudflared.service, /usr/bin/systemctl restart cloudflared.service, /usr/bin/systemctl status cloudflared.service
```

### Container Mode
If `OMNISIGHT_CF_MODE=container` is set, or no systemd is available, `cloudflared` is spawned directly as a child process.

## Managing an Existing Tunnel

When a tunnel is already provisioned, the wizard shows:
- Current tunnel status (online/offline)
- Connected hostnames
- **Rotate Token** — replace the stored CF API token
- **Teardown** — delete the tunnel and all DNS records

## Security

- API tokens are encrypted at rest using Fernet symmetric encryption
- The UI only displays a token fingerprint (last 4 characters)
- Tokens never appear in:
  - Server logs
  - SSE event payloads
  - HTTP error messages
- All operations are logged to the audit trail (`cf_tunnel.provision`, `cf_tunnel.rotate`, `cf_tunnel.delete`)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/cloudflare/validate-token` | Validate token, return accounts |
| GET | `/api/v1/cloudflare/zones?account_id=` | List zones for account |
| POST | `/api/v1/cloudflare/provision` | Create tunnel + DNS |
| GET | `/api/v1/cloudflare/status` | Tunnel health status |
| POST | `/api/v1/cloudflare/rotate-token` | Replace stored token |
| DELETE | `/api/v1/cloudflare/tunnel` | Teardown tunnel + DNS |

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Invalid token | Token revoked or typo | Re-create token in CF dashboard |
| 403 Missing permissions | Token scope too narrow | Add missing permissions to token |
| 409 Conflict | Tunnel/DNS already exists | Wizard reuses existing resources automatically |
| 429 Rate limited | Too many API calls | Wait and retry (automatic backoff) |
| Connector offline | `cloudflared` not running | Check service status, restart if needed |
