---
id: ADR-0013
title: Docs-site hosting + DNS + TLS — GitHub Pages with custom domain docs.sora.services
status: Superseded by ADR-0022 (live path) — retained as dormant GitHub Pages fallback
date: 2026-05-08
---

# ADR 0013 — Docs-site hosting + DNS + TLS: GitHub Pages w/ `docs.sora.services`

> **Superseded (2026-05-20) for the LIVE path by [ADR-0022](ADR-0022-docs-site-hosting-cf-tunnel.md).**
> The GitHub mirror has been broken since 2026-05-07 (PAT lacks `workflow`
> scope), so this GitHub Pages path no longer fires. The live docs site is
> now served via Cloudflare Tunnel at `docs.sora-dev.app` (ADR-0022). This
> ADR + its workflow + contract tests are **retained, unchanged, as a dormant
> fallback** that reactivates if the GitHub mirror is ever repaired.

**Status**: Accepted (2026-05-08, Sprint E — META OP-793); superseded as live path by ADR-0022 (2026-05-20)

**Decider**: sora (operator) + AI fleet

**Related**:
- [ADR-0006 — TLS termination at Synology DSM Reverse Proxy](ADR-0006-tls-termination-synology-reverse-proxy.md) — sora.services *.cert architecture (this ADR explicitly does NOT extend it to docs traffic; rationale below)
- [ADR-0012 — Docs-site framework: MkDocs (Material)](ADR-0012-docs-site-framework.md) — the framework whose output this ADR hosts; the "Caddy serve config on `sora.services`" line in §Consequences/Deferred is what this ADR closes
- OP-785 — MkDocs scaffold (declared `site_url: https://docs.sora.services/`)
- OP-792 — develop-merge publish pipeline (already publishes `docs-site-dist` to GitHub Pages; this ADR formalises the public URL surface around it)

---

## Context

OP-792 stood up a publish pipeline that builds `docs-site-dist/` on every
`develop` merge and uploads to GitHub Pages — but the `pages.github.io`
default URL is undocumented as the canonical home, has no DNS handle, and
the certificate it serves is GitHub's wildcard, not `*.sora.services`.

OP-785 (`docs-site/mkdocs.yml`) and `CLAUDE.md` (line 30) already declare
`https://docs.sora.services/` as the canonical docs URL — operators and
agents are expected to deep-link into `docs.sora.services/lessons/...`
when handing off lesson references. Today those links 404. This ADR
closes the gap.

The decision is host + DNS + TLS, scoped to **public-read documentation
that mirrors the `docs/` markdown corpus** — same authority model as the
git history it renders from. No private docs are in scope; if private
docs ever materialise, they are a separate hosting decision.

### Decision criteria

1. **Cost** — operator project, prefer $0 marginal cost.
2. **Ops complexity** — number of moving parts the operator owns
   (DNS records, cert renewals, deploy steps, monitoring).
3. **Integration with existing infra** — fewer net-new systems is better.
4. **Time-to-live** — Tier M (3–5h) total budget for OP-793; deliverable
   that requires net-new CI deploy chains is out of scope.
5. **Reversibility** — the choice should not lock us out of a later
   self-host migration if priorities change.

## Decision

**Adopt GitHub Pages with custom domain `docs.sora.services`. TLS via
GitHub-managed Let's Encrypt cert (auto-provisioned + auto-renewed by
GitHub on the custom-domain endpoint).**

DNS: a single `CNAME` record at the `docs.sora.services` apex of the
subdomain pointing at `<owner>.github.io` (resolved per the GitHub
Pages docs — for this repo, `limit5.github.io.`). The `CNAME` file
inside the published artifact (`docs-site-dist/CNAME`) tells GitHub
Pages to serve the custom hostname. With both records in place GitHub
provisions a Let's Encrypt cert for `docs.sora.services` within
~10 min and rotates it automatically thereafter (60-day rotation
cadence, no operator action).

### What this ADR locks in

- **Canonical docs URL**: `https://docs.sora.services/` (no trailing
  path prefix; subdomain not subpath, per ticket spec choice).
- **Host**: GitHub Pages, `actions/deploy-pages@v4` (already in
  `.github/workflows/docs-site-publish.yml` from OP-792 — no
  workflow rewrite, only a CNAME-copy step added).
- **Source of truth for the CNAME content**: `docs-site/docs/CNAME`
  (one line: `docs.sora.services`). Workflow copies it to
  `docs-site-dist/CNAME` before `upload-pages-artifact`. Storing
  in `docs-site/docs/` is forward-compatible with the eventual
  `mkdocs build` migration deferred by ADR-0012, since MkDocs
  copies non-markdown files from `docs_dir` to `site_dir` verbatim.
- **TLS**: GitHub-managed Let's Encrypt, 60-day auto-rotation, no
  operator-owned cert calendar entry.
- **Access control**: public read (GitHub Pages cannot gate access
  on a public repo — and the docs corpus is already public via the
  mirror). Write/admin scoped to repo collaborators with
  `Settings > Pages` access.
- **Migration redirects**: none required. Audit confirmed no legacy
  wiki / Confluence / GitBook / Read the Docs URLs reference docs
  content (`grep -i 'readthedocs\|gitbook\|wiki\.\|notion\.'` in
  `docs/` returned only third-party integration mentions, not our
  own old hosting). The only existing non-`docs.sora.services`
  reference is the GitHub Pages default URL itself, which the
  custom-domain switch supersedes.

### Operator one-time setup (out-of-band — see runbook)

Operator-side work (NOT automatable from this repo, captured in
[`docs/operations/docs-site-hosting.md`](../operations/docs-site-hosting.md)
as a checklist):

1. Add DNS `CNAME` record `docs.sora.services` → `limit5.github.io.`
   at the registrar that owns `sora.services`.
2. In repo `Settings > Pages`, set custom domain to `docs.sora.services`
   and enable "Enforce HTTPS" once GitHub finishes provisioning the cert.
3. Verify `curl -sI https://docs.sora.services/` returns 200 with a
   Let's Encrypt cert in the chain.

CI does NOT own these steps — they are one-time. The runbook is the
durable record so a future operator can re-execute under DR.

## Alternatives considered

### A. Self-host on `sora.services` via Caddy + Sectigo wildcard — rejected (for now)

The temptation: ADR-0006 already has a wildcard cert for
`*.sora.services` (Sectigo, ~1yr renewal) terminated at Synology DSM
Reverse Proxy. `docs.sora.services` falls under that wildcard for free.
The repo even ships `deploy/reverse-proxy/Caddyfile` that could front
a `file_server` for `docs-site-dist/`.

Why rejected today:

- **Ops chain is net-new**. Caddy is configured but not running on
  `sora.services` — the existing prod path runs through Cloudflare
  Tunnel into the backend container, not Caddy as a doc fileserver.
  Standing up the static-fileserver layer needs:
  1. Deploy step from GitHub Actions to NAS (rsync over Tailscale,
     SCP via deploy key, or a docker-volume mount strategy).
  2. DSM Reverse Proxy entry for `docs.sora.services` →
     internal HTTP service. Per ADR-0006 negative consequence #2,
     DSM RP is GUI-only and not in git — adds DR-recovery burden.
  3. NAS HA / availability tracking for what is otherwise a
     read-only static asset.
- **Tier M budget**. Building that chain is M+ on its own; it would
  blow the 3–5h envelope.
- **No actual benefit today**. Sharing the Sectigo wildcard with
  GitLab/Gerrit doesn't help docs traffic — public docs and
  authenticated code-review traffic don't gain from chain unification.
- **SPOF**. Pinning docs availability to the same NAS that runs
  GitLab+Gerrit means a NAS outage takes both code review *and*
  the docs that explain how to recover from a NAS outage.
  GitHub Pages decouples those failure domains.

This option is **kept open as the migration target** if priorities
later favour all-in-house hosting. Switching from GitHub Pages to
Caddy-on-NAS is bounded — same `docs-site-dist/` artifact, only the
workflow's deploy step + DNS record change.

### B. Netlify free tier — rejected

- Functionally equivalent to GitHub Pages (free, auto-LE, custom
  domain, easy DNS).
- Adds a third-party account (Netlify) that would need
  `auth_provisioning` integration for write-access management,
  whereas GitHub Pages is already governed by the same repo ACL we
  already operate.
- No marginal feature we'd use — preview deploys per PR are nice
  but not in OP-793's spec.
- Net: more operator surface, zero feature win.

### C. Path-based hosting `sora.services/docs` — rejected

- Spec offered subdomain vs path. Path-based requires the same
  Caddy/DSM chain as Option A plus URL-rewrite rules, and complicates
  the `external_url` semantics of GitLab (which is already at
  `https://sora.services:49156`). Subdomain is the cleaner break.

## Consequences

**Positive**

- Zero net-new ops surface. OP-792's pipeline keeps working; one
  workflow step (`cp docs-site/docs/CNAME docs-site-dist/CNAME`)
  plus one CNAME source file in the repo.
- Cert rotation is GitHub-managed — no operator-owned calendar
  entry, no per-cert renewal script.
- Free.
- Operator-side setup is three steps, documented as a checklist;
  reproducible in DR.
- Reversible: the `docs-site-dist/` artifact is the same on disk
  whether GitHub Pages or Caddy serves it; switching costs are
  bounded to the workflow deploy step + DNS.

**Negative**

- Hosting is on GitHub. If GitHub Pages availability drops, docs
  drop. (Mitigation: docs are mirrored in the repo as markdown;
  `git clone` is the offline fallback. Acceptable for non-critical
  docs.)
- Cert chain is not unified with the `*.sora.services` Sectigo
  wildcard. Consequence: when ADR-0006's cert is rotated, this docs
  endpoint is unaffected (which is also positive — independent
  rotation cadences) but means two cert-rotation calendars exist
  for `*.sora.services` apex services overall.
- Custom domain on GitHub Pages cannot enforce private read access.
  If a future requirement requires gated docs (e.g. operator-only
  pages), we'd migrate to Option A or split that subset out.

**Neutral**

- DNS owner is operator-side, not in this repo. The runbook
  documents the record but cannot enforce it; a missing record
  shows up as a 404 / cert-mismatch from `curl` and is the
  observability surface.

**Reversibility**

If GitHub Pages proves unsuitable, the migration target is Caddy on
`sora.services` (Alternative A). Rough switching cost:

- 1 workflow change: replace `actions/deploy-pages@v4` step with
  rsync-to-NAS step + drop `id-token: write` permission.
- 1 DNS change: CNAME → A/AAAA pointing at NAS public IP.
- 1 DSM RP entry: `docs.sora.services` → internal Caddy port.
- 1 Caddy block in `deploy/reverse-proxy/Caddyfile` adding a
  `file_server` route.
- Cert: zero work, Sectigo wildcard already covers.

Estimated migration cost: 1 ticket, Tier M.
