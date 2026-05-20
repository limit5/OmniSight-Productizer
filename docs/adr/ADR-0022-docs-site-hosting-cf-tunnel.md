---
id: ADR-0022
title: Docs-site hosting — Cloudflare Tunnel self-host at docs.sora-dev.app (supersedes ADR-0013 live path)
status: Accepted
date: 2026-05-20
supersedes: ADR-0013 (host decision only)
---

# ADR 0022 — Docs-site hosting: Cloudflare Tunnel self-host at `docs.sora-dev.app`

**Status**: Accepted (2026-05-20)

**Decider**: sora (operator) + AI fleet

**Supersedes**: [ADR-0013](ADR-0013-docs-site-hosting-dns-tls.md) — *host decision only*.
ADR-0013's framework choice (MkDocs Material, via ADR-0012) is unchanged.
The GitHub Pages publish path ADR-0013 specified is **retained as a dormant
fallback** (workflow + its contract tests preserved) but is no longer the
live serving path.

**Related**:
- [ADR-0013 — Docs-site hosting: GitHub Pages](ADR-0013-docs-site-hosting-dns-tls.md) — the superseded live-path decision; §A of that ADR pre-considered (and deferred) exactly this self-host option.
- [ADR-0012 — Docs-site framework: MkDocs Material](ADR-0012-docs-site-framework.md) — framework unchanged.
- [ADR-0006 — TLS termination at Synology DSM Reverse Proxy](ADR-0006-tls-termination-synology-reverse-proxy.md) — the `*.sora.services` wildcard; explicitly NOT used here (see "Why not docs.sora.services" below).
- Operations runbook: [docs-site-hosting-cf-tunnel.md](../operations/docs-site-hosting-cf-tunnel.md)

---

## Context

ADR-0013 (2026-05-08) chose GitHub Pages with custom domain
`docs.sora.services`, deployed via `.github/workflows/docs-site-publish.yml`
on every `develop` merge, with the artifact mirrored Gerrit → GitLab →
GitHub.

That path has been **non-functional since 2026-05-07**: the GitLab→GitHub
mirror push is rejected because the GitHub PAT lacks the `workflow` scope
needed when `.github/workflows/**` changes. The operator has decided not to
repair the GitHub mirror (it is a visibility-only mirror, off the critical
path). With the mirror dead, no GitHub Actions run, so the GitHub Pages
deploy never fires and `https://docs.sora.services/` returns nothing.

Meanwhile the live application already runs behind a **Cloudflare Tunnel**
(`cloudflared` → Caddy) on the `sora-dev.app` Cloudflare zone, and a Caddy
reverse proxy is already running in the prod compose stack. Standing up a
static file server there is a small, in-house change that does not depend on
the broken GitHub path.

## Decision

**Serve the docs site from the existing Caddy container via a dedicated
`:8081` `file_server` block, exposed publicly as `https://docs.sora-dev.app/`
through the already-running Cloudflare Tunnel. TLS terminates at the
Cloudflare edge. The static artifact is built locally by the
`squidfunk/mkdocs-material` Docker image and refreshed by a systemd timer.**

### What this ADR locks in

- **Canonical docs URL**: `https://docs.sora-dev.app/` (was
  `https://docs.sora.services/`). `mkdocs.yml` `site_url` and `CLAUDE.md`'s
  lessons link are updated to match.
- **Ingress**: Cloudflare Zero Trust dashboard → Networks → Tunnels →
  Public Hostnames → `docs.sora-dev.app` → Service `HTTP` → `caddy:8081`.
  Same operator-owned dashboard surface as the live app's `ai.sora-dev.app`.
- **Serving**: Caddy `:8081` `file_server` over `root * /srv/docs`, fed by a
  read-only bind mount `./docs-site-dist:/srv/docs:ro` (see
  `deploy/reverse-proxy/Caddyfile` and `docker-compose.prod.yml`).
- **Build**: `scripts/build_docs_site.sh` runs `squidfunk/mkdocs-material`
  (build env, no host mkdocs install) and rsyncs into `docs-site-dist/`.
  `deploy/systemd/omnisight-docs-build.{service,timer}` rebuild daily.
- **TLS**: Cloudflare-managed edge cert for the `sora-dev.app` zone. No
  operator-owned cert calendar entry.
- **CNAME file**: the GitHub-Pages `docs-site/docs/CNAME` custom-domain
  marker is **deleted** — Cloudflare Tunnel does not use it, and serving a
  public `/CNAME` would needlessly leak the hostname.

## Why not keep `docs.sora.services` (the Synology wildcard)?

ADR-0006's Sectigo `*.sora.services` wildcard would cover
`docs.sora.services` for free, and ADR-0013 §A considered self-hosting under
it. Rejected here because:

- The prod host is **WSL2**; it has no inbound LAN reachability. The Synology
  DSM Reverse Proxy (which fronts `*.sora.services`) cannot forward to a WSL2
  origin without Windows `netsh` port-proxying — a fragile net-new chain.
- The Cloudflare Tunnel is an **outbound** connector that already works from
  WSL2, and already fronts the live app. Reusing it is zero net-new ingress.
- `sora.services` DNS points at the Synology IP, not Cloudflare, so the
  tunnel cannot mint a cert for `docs.sora.services` anyway.

Net: `docs.sora-dev.app` (Cloudflare zone, tunnel-fronted) is the path of
least resistance. The canonical-URL change is the accepted cost.

## Rebutting ADR-0013 §A's original self-host rejections

ADR-0013 §A rejected self-host on four grounds; status now:

1. **"Ops chain is net-new / Caddy not running."** No longer true — Caddy is
   running in the prod stack as the app's edge. ADR-0013 §A assumed CF Tunnel
   went straight into the backend container; the architecture has since
   evolved so the tunnel terminates at Caddy `:8080`. Adding a `:8081`
   file_server block is incremental, not net-new.
2. **"Tier-M budget."** The serving + build pipeline was built in well under
   the envelope by reusing Caddy + the mkdocs-material image.
3. **"No actual benefit today."** The benefit is concrete now: the GitHub
   path is dead, so self-host is the *only* way to make the canonical docs
   URL resolve at all.
4. **"SPOF — docs share a failure domain with code review."** Accepted, with
   nuance: docs now share a failure domain with the *app* (Cloudflare Tunnel +
   this host), not with GitLab/Gerrit (Synology). Critical recovery runbooks
   are also kept readable directly from the git tree (`docs/operations/`), so
   a docs-site outage never blocks recovery.

## Consequences

- **Positive**: canonical docs URL resolves again without touching the broken
  GitHub mirror; reuses running infra; $0 marginal cost; in-house + reversible.
- **Negative**: docs availability now tracks the Cloudflare Tunnel + this host
  (not GitHub's edge); one more systemd timer to own; the build runs from the
  working tree, not a clean develop checkout (acceptable for a low-traffic
  internal site — see `scripts/build_docs_site.sh` header).
- **Reversibility**: if the GitHub mirror is later repaired, the dormant
  GitHub Pages path (workflow + ADR-0013) can be reactivated; switching back
  is a DNS + ingress change, same `docs-site-dist/` artifact.

## Retained dormant fallback

`.github/workflows/docs-site-publish.yml`, its contract tests
(`backend/tests/test_docs_site_publish_pipeline.py`), and ADR-0013 are
**intentionally preserved unchanged**. They document the GitHub Pages path
that reactivates if/when the mirror is repaired. They are dormant, not dead.
