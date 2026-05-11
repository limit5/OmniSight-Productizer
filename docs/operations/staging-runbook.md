# Staging Deploy Runbook (OP-878)

This runbook covers the D6 staging mirror at
`https://staging.sora.services`. The staging host runs two color
projects from `deploy/staging/docker-compose.yml` and the existing host
Caddy ingress imports `deploy/caddy/staging.caddy`.

## Provision

Install the Caddy site block into the host Caddyfile:

```caddyfile
import /home/user/work/sora/OmniSight-Productizer/deploy/caddy/staging.caddy
```

Seed the active-upstream file and reload Caddy:

```bash
mkdir -p /var/lib/omnisight/staging
printf 'reverse_proxy 127.0.0.1:18080\n' \
  | sudo tee /var/lib/omnisight/staging/active-upstream.caddy
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

Create the staging env file on the host:

```bash
cp deploy/staging/.env.example deploy/staging/.env
${EDITOR:-nano} deploy/staging/.env
```

Minimum values:

```bash
POSTGRES_PASSWORD=...
OMNISIGHT_GHCR_NAMESPACE=<ghcr-owner>
OMNISIGHT_AUTH_MODE=strict
OMNISIGHT_ENV=staging
```

## Auto-Deploy Trigger

`scripts/staging_deploy.sh` consumes a Gerrit `change-merged` payload.
Cron should invoke it against the latest webhook spool file:

```cron
* * * * * cd /home/user/work/sora/OmniSight-Productizer && scripts/staging_deploy.sh --event-file /var/spool/omnisight/gerrit-last.json
```

The event is ignored unless it is `change-merged` for `main` or
`refs/heads/main`. The image tag is read from `image_tag`, `imageTag`,
`newRev`, `revision`, `commit`, `submitRevision`, or matching nested
Gerrit fields.

## Blue-Green Flow

The active color is stored in `/var/lib/omnisight/staging/active_color`.
If absent, blue is treated as active and green is the first standby.

1. Pull the D2 backend/frontend images with `OMNISIGHT_IMAGE_TAG` set to
   the merged-main revision.
2. Start the standby Compose project:
   `omnisight-staging-blue` or `omnisight-staging-green`.
3. Poll every 30 seconds until `/health` is HTTP 200 through Caddy and
   both backend host ports; the frontend root is also checked because
   the frontend image does not expose a `/health` route.
4. Atomically replace `/var/lib/omnisight/staging/active-upstream.caddy`.
5. Run `caddy validate` and `caddy reload`.
6. Poll `https://staging.sora.services/health`; if it flatlines for
   3 minutes, switch ingress back to the prior color and stop the failed
   standby.
7. Stop the old color with a 60-second drain window.

Default color ports:

| Color | Caddy | Backend A | Backend B | Frontend | Postgres |
| --- | ---: | ---: | ---: | ---: | ---: |
| blue | 18080 | 18010 | 18011 | 13010 | 55432 |
| green | 28080 | 28010 | 28011 | 23010 | 55433 |

## Health Failure

`StagingHealthFlatlined` means the standby color failed to return 200
for more than 3 minutes. The script leaves the old color active, stops
the failed standby, and posts an optional webhook alert through
`OMNISIGHT_STAGING_ALERT_WEBHOOK`.

Recovery:

```bash
docker compose -p omnisight-staging-green -f deploy/staging/docker-compose.yml logs --tail=200
docker compose -p omnisight-staging-green -f deploy/staging/docker-compose.yml stop
```

Replace `green` with the failed standby color shown in the alert.

## Image Pull Failure

`StagingImagePullFailed` means GHCR did not return the D2 image tag for
one or more staging services. No ingress change occurs.

Recovery:

```bash
OMNISIGHT_IMAGE_TAG=<tag> docker compose -p omnisight-staging-green \
  -f deploy/staging/docker-compose.yml pull
```

Confirm the D2 image exists, then rerun `scripts/staging_deploy.sh`.

## Ingress Switch Failure

`StagingIngressSwitchFailed` means Caddy validation or reload failed
after the active-upstream file was changed. The script rewrites the
prior upstream and retries reload, but this remains a manual recovery
event.

Manual recovery:

```bash
cat /var/lib/omnisight/staging/active_color
printf 'reverse_proxy 127.0.0.1:18080\n' \
  | sudo tee /var/lib/omnisight/staging/active-upstream.caddy
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
curl -fsS https://staging.sora.services/health
```

Use `18080` for blue or `28080` for green, matching the active color.

## Operator Verification

After a successful deploy:

```bash
curl -fsS https://staging.sora.services/health
curl -fsS https://staging.sora.services/readyz
cat /var/lib/omnisight/staging/active_color
cat /var/lib/omnisight/staging/active_tag
```

The ticket DoD still requires an operator browser visit to
`https://staging.sora.services` after the stack is reachable.
