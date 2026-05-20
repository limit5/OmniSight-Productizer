# Docs-Site Hosting via Cloudflare Tunnel (ADR-0022)

Operator-facing runbook for the **live** public docs site at
**`https://docs.sora-dev.app/`**.

Decision rationale — see
[ADR-0022](../adr/ADR-0022-docs-site-hosting-cf-tunnel.md). This document is
the operational counterpart: how to bring the hosting up, verify it, what to
rotate, and what to do if it breaks. The previous GitHub Pages runbook
([docs-site-hosting.md](docs-site-hosting.md), ADR-0013) is retained as the
dormant-fallback reference.

## Architecture

```
                 (public client, browser)
                          │
                          ▼
        DNS:  docs.sora-dev.app  (Cloudflare zone, tunnel route)
                          │
                          ▼
                 Cloudflare edge
              (TLS: Cloudflare-managed
               cert for the sora-dev.app zone)
                          │
                          ▼ (outbound tunnel, plain HTTP)
              cloudflared  →  caddy:8081  (file_server)
                          │
                          ▼
              docs-site-dist/  (read-only bind mount /srv/docs)
                          ▲
                          │
              scripts/build_docs_site.sh
              (squidfunk/mkdocs-material image)
                          ▲
                          │
              omnisight-docs-build.timer (daily)
```

**Single host**: the prod WSL2 host's Caddy container. **Single TLS
authority**: Cloudflare edge (sora-dev.app zone). **Ingress**: one Cloudflare
Zero Trust Public Hostname. No NAS, no Synology cert, no GitHub in the live
path.

## Host choice

Cloudflare Tunnel + the already-running prod Caddy. The same connector
(`cloudflared`) that fronts the live app `ai.sora-dev.app` also fronts
`docs.sora-dev.app`; Caddy's `:8081` block serves the static MkDocs output.
Alternatives (GitHub Pages, Synology Web Station) are documented and rejected
in ADR-0022.

## DNS configuration

Created by the Cloudflare Tunnel route, not a manual registrar record:

| Type | Host | Value | Managed by |
|---|---|---|---|
| Tunnel route | `docs.sora-dev.app` | `<tunnel-uuid>.cfargotunnel.com` | Cloudflare Zero Trust dashboard |

The `sora-dev.app` zone is delegated to Cloudflare nameservers, so the tunnel
route + edge cert are issued automatically once the Public Hostname is added.

## TLS

| Property | Value |
|---|---|
| Authority | Cloudflare-managed edge cert (sora-dev.app zone) |
| Termination | Cloudflare edge; plain HTTP over the tunnel to `caddy:8081` |
| Rotation | Automatic at the Cloudflare edge. No operator action. |

## Access control

Public read-only. The site renders the curated `docs-site/docs/` markdown —
same authority model as the git history it renders from. No private docs are
in scope. Caddy serves `file_server` only (no write surface); the bind mount
is read-only (`/srv/docs:ro`).

## Rotation

| Item | Cadence | Mechanism | Operator action |
|---|---|---|---|
| TLS cert | Automatic | Cloudflare edge auto-renew | None |
| Docs content | Daily | `omnisight-docs-build.timer` rebuilds from the working tree | None |
| Tunnel credentials | On tunnel rotation | `OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN` in `.env` | Only if the tunnel is recreated |

## One-time operator setup checklist

1. Ensure `docs-site-dist/` is populated: run `bash scripts/build_docs_site.sh`.
2. Install + enable the build timer:
   ```
   ln -sf "$PWD/deploy/systemd/omnisight-docs-build.service" ~/.config/systemd/user/
   ln -sf "$PWD/deploy/systemd/omnisight-docs-build.timer"   ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now omnisight-docs-build.timer
   ```
3. Recreate the Caddy container so the new `:8081` block + `/srv/docs` mount
   take effect: `docker compose -f docker-compose.prod.yml -p omnisight-productizer up -d caddy`.
4. In the Cloudflare Zero Trust dashboard → Networks → Tunnels → the OmniSight
   tunnel → Public Hostnames → Add:
   - Subdomain `docs`, Domain `sora-dev.app`
   - Service Type `HTTP`, URL `caddy:8081`
5. Verify (below).

## Disaster recovery

| Failure | Recovery |
|---|---|
| Docs site 404 / blank | Check `docs-site-dist/` is populated (`ls docs-site-dist/index.html`); re-run `scripts/build_docs_site.sh`; confirm the Caddy container has the `/srv/docs` mount (`docker inspect`). |
| Caddy `:8081` not responding | `docker compose ... up -d caddy`; check `caddy validate` passes on the Caddyfile. |
| `docs.sora-dev.app` does not resolve | Re-add the Cloudflare Public Hostname (step 4); confirm `cloudflared` container is running. |
| Tunnel down | Restart `cloudflared`; the live app shares this tunnel, so this is the app's DR path too. |
| Build timer not firing | `systemctl --user status omnisight-docs-build.timer`; check `/home/user/work/sora/logs/docs-build/build.log`. |

Critical recovery runbooks are also readable directly from the git tree
(`docs/operations/`), so a docs-site outage never blocks recovery.

## Verification

```bash
# Build output present
ls docs-site-dist/index.html

# Caddy serves it internally (after container recreate)
docker exec omnisight-productizer-caddy-1 wget -qO- http://localhost:8081/ | head -5

# Public endpoint (after CF Public Hostname added)
curl -sI https://docs.sora-dev.app/ | head -3   # expect HTTP 200
```

## Related

- [ADR-0022 — Docs-site hosting: Cloudflare Tunnel](../adr/ADR-0022-docs-site-hosting-cf-tunnel.md)
- [ADR-0013 — Docs-site hosting: GitHub Pages (dormant fallback)](../adr/ADR-0013-docs-site-hosting-dns-tls.md)
- [ADR-0012 — Docs-site framework: MkDocs Material](../adr/ADR-0012-docs-site-framework.md)
- Build pipeline: `scripts/build_docs_site.sh` + `deploy/systemd/omnisight-docs-build.{service,timer}`
