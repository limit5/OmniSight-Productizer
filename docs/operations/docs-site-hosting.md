# Docs-Site Hosting + DNS + TLS (OP-793)

> **DORMANT FALLBACK (2026-05-20).** This GitHub Pages hosting path is no
> longer the live serving path — superseded by
> [ADR-0022](../adr/ADR-0022-docs-site-hosting-cf-tunnel.md) (Cloudflare
> Tunnel at `docs.sora-dev.app`). It is retained unchanged as the fallback
> that reactivates if the GitHub mirror (broken since 2026-05-07) is repaired.
> For the LIVE runbook, see
> [docs-site-hosting-cf-tunnel.md](docs-site-hosting-cf-tunnel.md).

Operator-facing runbook for the public docs site at
**`https://docs.sora.services/`**.

Decision rationale — see
[ADR-0013](../adr/ADR-0013-docs-site-hosting-dns-tls.md). This document
is the operational counterpart: how to bring the hosting up from
nothing, how to verify it, what to rotate, and what to do if it breaks.

## Architecture

```
                 (public client, browser)
                          │
                          ▼
        DNS:  docs.sora.services  CNAME  →  limit5.github.io.
                          │
                          ▼
                 GitHub Pages edge
              (TLS: Let's Encrypt cert
               auto-provisioned, 60-day rotate)
                          │
                          ▼
              docs-site-dist/  (artifact)
                          ▲
                          │
              actions/deploy-pages@v4
                          ▲
                          │
   .github/workflows/docs-site-publish.yml  (on: push develop)
                          │
                          ▼
           python -m backend.docs_static_site
                          │
                          ▼
               docs-site/docs/  (markdown, MkDocs config,
                                  CNAME source-of-truth)
```

**Single host**: GitHub Pages. **Single TLS authority**: GitHub-managed
Let's Encrypt. **Single DNS record**: `CNAME docs.sora.services →
limit5.github.io.`. No NAS, no Caddy, no Sectigo cert in the docs path.

## Host choice

GitHub Pages, with the GitHub Pages cert (Let's Encrypt) auto-provisioned
on the custom domain. The pipeline that publishes to it is OP-792's
`.github/workflows/docs-site-publish.yml`, unchanged in shape — OP-793
adds one step (`stage CNAME for custom domain`) and one source-of-truth
file (`docs-site/docs/CNAME`).

Alternatives (Netlify, self-host via Caddy on `sora.services`) are
documented and rejected in ADR-0013 §Alternatives.

## DNS configuration

**Single record**, set at the registrar that owns `sora.services`:

| Type | Host | Value | TTL |
|---|---|---|---|
| `CNAME` | `docs.sora.services.` | `limit5.github.io.` | 3600 |

Notes:
- Owner: the operator who controls the `sora.services` zone (not in
  this repo). DNS is the only out-of-band step CI cannot do.
- The trailing dot on `limit5.github.io.` matters at some registrars
  (it's an FQDN, not a relative name); check the registrar UI.
- GitHub publishes its Pages IPs (185.199.108.153 etc.) for `A`-record
  setups. We use `CNAME` rather than apex-`A` because `docs` is a
  subdomain, not an apex — `CNAME` is the recommended record type for
  custom subdomains in
  [GitHub's Pages docs](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site).
- DNS propagation can take up to 1h; cert provisioning needs DNS to
  resolve before GitHub will issue the LE cert.

## TLS

GitHub-managed **Let's Encrypt** cert, auto-provisioned and
auto-rotated on the custom domain.

| Property | Value |
|---|---|
| Issuer | Let's Encrypt (R3 / R4 etc., per LE issuance current) |
| Subject | `docs.sora.services` |
| Provisioning | Automatic, ~10 min after DNS resolves + custom domain set in repo Pages settings |
| Rotation | Automatic, ~60 days before expiry. No operator action. |
| Renewal calendar entry | **None required** (contrast: ADR-0006 Sectigo wildcard, which has a 1y operator-owned calendar entry for `*.sora.services`) |

If GitHub fails to provision the cert (UI shows a yellow banner under
`Settings > Pages`), the most common causes:

1. DNS doesn't resolve yet — `dig +short docs.sora.services` should
   return `limit5.github.io.` (or its A records).
2. Custom-domain field in `Settings > Pages` was set before the
   `CNAME` file was committed — clear and re-enter the domain to
   trigger re-validation.
3. AAAA record exists for the apex — GitHub Pages strongly prefers
   no AAAA on the custom subdomain unless it points at GitHub's
   Pages IPv6.

## Access control

| Surface | Access | Notes |
|---|---|---|
| Public read | Anyone | Same authority model as the markdown corpus in this repo (public mirror) |
| Repo `Settings > Pages` | Repo admins (`limit5`/operators) | Sets custom domain, toggles "Enforce HTTPS" |
| DNS record | Operator (registrar account holder) | Out-of-band; not in repo |
| Workflow that publishes | Anyone with merge rights to `develop` | Subject to per-branch protections + Gerrit review per ADR-0003 |
| Pages env secret rotation | N/A | OP-792 uses ephemeral OIDC token via `id-token: write`, not a long-lived secret |

If a future requirement gates docs read access (private operator-only
pages), the migration path is Caddy on `sora.services` with auth at
the DSM RP layer — see ADR-0013 §Reversibility.

## Rotation

| What | Rotates | How | Operator action |
|---|---|---|---|
| TLS cert | Every ~60 days | GitHub auto-renews LE | None |
| DNS record | Never (until docs URL changes) | — | None |
| Pages OIDC token | Per-run | GitHub-issued, ephemeral | None |
| Workflow source | Per-merge | Repo PRs | Standard review |
| CNAME source-of-truth | Per-merge | Edit `docs-site/docs/CNAME` | Code-review change |

Net: no operator-owned rotation calendar for this stack. Compare
ADR-0006 (Sectigo wildcard, 1y, calendar entry on operator) — that
calendar entry **does NOT extend** to `docs.sora.services` because
this hostname is on the GitHub-managed cert, not the Sectigo one.

## One-time operator setup checklist

Run once when first standing up the public hostname (or re-running
under DR):

- [ ] **DNS**: At the `sora.services` registrar, add
      `CNAME docs.sora.services → limit5.github.io.` (TTL 3600).
- [ ] **Verify DNS**: `dig +short docs.sora.services` returns
      `limit5.github.io.` or one of GitHub's Pages IPs.
- [ ] **GitHub repo `Settings > Pages`**: set "Custom domain" to
      `docs.sora.services`. Save.
- [ ] **Wait** ~10 min for GitHub to provision the cert. The Pages
      settings page will show "DNS check successful" then "Certificate
      issued for docs.sora.services".
- [ ] **Enable "Enforce HTTPS"** checkbox once the cert is issued.
- [ ] **Verify cert**: `curl -sI https://docs.sora.services/ | head`
      returns `HTTP/2 200` and the cert chain includes Let's Encrypt
      (`openssl s_client -connect docs.sora.services:443 -servername
      docs.sora.services </dev/null 2>&1 | grep "issuer="`).
- [ ] **Verify content**: Open `https://docs.sora.services/docs/sop/lessons/`
      and confirm the lessons index renders (matches `CLAUDE.md` line 30
      reference).

After this list, no recurring operator action is needed.

## Migration redirects

**None required.** Audit (2026-05-08) confirmed:

- No legacy Confluence / Notion / GitBook / Read the Docs URLs
  exist for OmniSight docs (`grep -i "readthedocs\|gitbook\|wiki\.\|
  notion\." docs/` returned only third-party integration mentions,
  not our docs).
- The pre-OP-793 `<owner>.github.io/<repo>` default URL was never
  publicly advertised as canonical — `CLAUDE.md` and `mkdocs.yml`
  always pointed at `docs.sora.services`.
- Internal links inside the docs corpus use repo-relative paths,
  not absolute URLs; rendering picks up the new host automatically.

If a future hosting migration introduces a redirect requirement,
GitHub Pages supports redirects via `<meta http-equiv="refresh">` in
HTML files, or — for a self-host migration — Caddy `redir` directives
at the reverse-proxy layer.

## Disaster recovery

Loss scenarios and recovery:

| Scenario | Recovery |
|---|---|
| GitHub Pages deploy fails (build error) | Previous green deploy stays live (workflow `deploy` job needs `build`); fix the error in a follow-up merge. See [docs-site-pipeline.md](docs-site-pipeline.md). |
| GitHub Pages outage | Read fallback: `git clone` the repo and read `docs/` markdown directly. Public docs are non-critical. |
| Custom domain breaks (cert revoked, DNS poisoned) | Re-run "One-time operator setup checklist" from the top. Workflow itself is unaffected — only operator-side config needs restoration. |
| GitHub repo lost | Restore from the GitLab mirror (per ADR-0002) into a new GitHub repo, re-run the setup checklist with the new repo's `<owner>.github.io` target. |

## Verification

Contract tests live at
`backend/tests/test_docs_site_hosting.py`. They cover:

- CNAME source-of-truth file exists with the right hostname.
- Workflow stages CNAME into the published artifact.
- `mkdocs.yml` `site_url` matches the public hostname.
- ADR-0013 + this runbook reference each other.

Run with:

```bash
.venv/bin/python -m pytest backend/tests/test_docs_site_hosting.py -v
```

## Related

- [ADR-0013 — Docs-site hosting + DNS + TLS](../adr/ADR-0013-docs-site-hosting-dns-tls.md)
- [ADR-0012 — Docs-site framework: MkDocs (Material)](../adr/ADR-0012-docs-site-framework.md)
- [ADR-0006 — TLS termination at Synology DSM Reverse Proxy](../adr/ADR-0006-tls-termination-synology-reverse-proxy.md)
  (sora.services Sectigo wildcard — explicitly NOT in this docs path; see ADR-0013 §Alternatives)
- [docs-site-pipeline.md](docs-site-pipeline.md) — OP-792 build/deploy CI flow
