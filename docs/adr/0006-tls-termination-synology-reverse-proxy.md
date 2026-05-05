# ADR 0006 — TLS Termination at Synology DSM Reverse Proxy + Sectigo Wildcard Cert

**Status**: Accepted (2026-05-05)

**Supersedes**: Phase 2 entry gate "HTTPS BLOCKER" item in [Phase 1-5 SOP](../sop/migration-plan-2026-05.md) — replaced with the architecture this ADR documents.

**Context**

[ADR 0002](0002-gitlab-primary-github-mirror.md) decided GitLab self-hosted is the primary code repo + Gerrit is the review layer (per [ADR 0003](0003-gerrit-code-review.md)). Both run on the same Synology NAS at `sora.services`. Phase 0 baseline (2026-05-05) ran all three services on plain HTTP:

| Service | HTTP endpoint (legacy) |
|---|---|
| GitLab | `http://sora.services:49154` |
| Gerrit web | `http://sora.services:29419` |
| Gerrit SSH | `sora.services:29418` (SSH, no TLS layer needed) |

PAT plaintext + LAN-only assumption was acceptable for Phase 0-1 testing, but Phase 2 (GitLab becomes primary, mirror push + GitHub webhook + CI runner registration) requires HTTPS for:

1. **CI runner registration** — runner SDKs refuse plain-HTTP for token-bearing flows on most managed CI providers
2. **Webhook delivery to external services** — GitHub mirror push, Slack notifications, future JIRA links
3. **OAuth callback URLs** — providers reject HTTP callbacks (security policy)
4. **Git LFS over HTTPS** — LFS spec assumes HTTPS for token transit
5. **Phase D legal-review compliance posture** — auditors expect transit encryption documented in security architecture

Three options were evaluated for HTTPS termination:

| Option | Where TLS terminates | Cert source | Operational burden |
|---|---|---|---|
| **A. Cloudflare Tunnel** | Cloudflare edge | Cloudflare-issued | Lowest (auto-renew, zero cert config locally) — but ties prod path to Cloudflare availability |
| **B. nginx/Caddy reverse proxy on host** | host process | Let's Encrypt or commercial | Medium (cert renew script + host nginx config maintenance) |
| **C. Synology DSM Reverse Proxy** | DSM-managed proxy | Cert imported into DSM | Low-medium (DSM auto-renew via Let's Encrypt OR commercial cert manual import) |

**Decision**

**Adopt Option C** — TLS termination at Synology DSM Reverse Proxy with Sectigo commercial wildcard cert (`*.sora.services`).

**Architecture (post-2026-05-05)**:

```
                                            ┌─────────────────────┐
internet / LAN client                       │  Synology NAS       │
                                            │                     │
─── HTTPS :49156 ────► DSM Reverse Proxy ──►│ GitLab container    │
   (Sectigo cert)                          ─┼ http :49154 → :80 ──┤
                                            │                     │
─── HTTPS :29420 ────► DSM Reverse Proxy ──►│ Gerrit container    │
   (Sectigo cert)                          ─┼ http :29419 → :8080─┤
                                            │                     │
─── SSH    :29418 ────► (direct, no proxy) ─►│ Gerrit container    │
                                            │ ssh :29418 → :29418 │
                                            └─────────────────────┘
```

**Cert details**:
- Subject: `*.sora.services` (wildcard, single-level subdomain coverage)
- Issuer: `Sectigo Limited` / `Sectigo Public Server Authentication CA DV R36`
- Verified clients trust without `-k` flag (`ssl_verify_result=0`)
- Auto-renewal: handled by Sectigo issuance flow + DSM cert manager (operator confirms before expiry)

**Per-service config**:

GitLab `GITLAB_OMNIBUS_CONFIG` env var:
```ruby
external_url 'https://sora.services:49156'
nginx['listen_port'] = 80
nginx['listen_https'] = false
nginx['proxy_set_headers'] = { 'X-Forwarded-Proto' => 'https', 'X-Forwarded-Ssl' => 'on' }
```

Internal nginx still listens on port 80 (HTTP); DSM Reverse Proxy 49156 → container 49154 → internal nginx 80. `external_url` tells GitLab Rails app it's served over HTTPS so generated URLs (web_url, http_url_to_repo, etc.) carry the right scheme.

Gerrit `gerrit.config`:
```ini
[gerrit]
canonicalWebUrl = https://sora.services:29420/

[httpd]
listenUrl = proxy-https://*:8080/
```

`proxy-https://` tells Gerrit it's behind a TLS-terminating proxy so HTTPS-aware features (cookie secure flag, HSTS, OAuth callback) work correctly.

**SSH stays direct** — `sora.services:29418` no proxy. SSH protocol has its own crypto layer; wrapping in TLS adds nothing.

**Consequences**

Positive:
- One cert covers all sora.services sub-port endpoints (49156, 29420, future Phase 5 prod env endpoints) without per-service cert config
- Auto-renew handled by Sectigo + DSM cert manager — no per-service cron / acme.sh script
- Phase 5 dev/prod isolation: DSM Reverse Proxy abstracts backend container changes — only the reverse proxy mapping changes, cert + endpoint stay constant
- DR/backup simpler: only DSM reverse proxy rules + Sectigo cert need backup; per-service config doesn't carry cert state
- SSH remains untouched (direct, no proxy hop adds latency)

Negative:
- DSM is a single point of failure for HTTPS termination — if NAS goes down, all HTTPS endpoints unavailable simultaneously (mitigation: existing NAS HA + offsite backup pipeline)
- DSM Reverse Proxy UI is GUI-only — config not in git, requires manual restoration in DR scenario (documented in DR runbook entry — see Phase 5 prep)
- Sectigo is a commercial cert (~$50-100/year for wildcard) — Let's Encrypt would be free, but DV-only and 90-day renewals add operational pressure; Sectigo trades cost for ops simplicity
- Wildcard cert only covers single-level subdomain — `app.sora.services` covered, `*.app.sora.services` NOT covered without separate cert. Future expansion may need cert refresh
- Cert expiry monitoring relies on DSM cert manager UI — operator must review proactively before expiry (recommendation: Prometheus probe or external monitoring, see Phase 5 prep)

Neutral:
- Internal services still HTTP — fine because they live inside container network not exposed externally; if Phase 5 splits to multiple physical hosts, internal traffic might want mTLS, separate ADR
- `nginx['listen_https'] = false` means GitLab won't try to manage cert itself (internal nginx pure HTTP) — keeps cert authority unified at DSM layer

**Phase rollout reflection (2026-05-05)**

This ADR was written *after* the migration completed because:
- Phase 0 baseline (2026-05-04) ran HTTP without HTTPS plan
- Phase 2 entry gate originally listed HTTPS as BLOCKER
- User chose to land HTTPS pre-Phase-2 (vs the HTTP-defer path discussed in earlier session)
- Sectigo cert procurement + DSM Reverse Proxy setup happened operator-side

Documenting now so the architecture is auditable + Phase 5 prod env rebuild has the recipe. Phase 2 entry gate's HTTPS BLOCKER is hereby ✅ resolved by this architecture.

**Verification snapshots (2026-05-05)**

GitLab API:
- `web_url` = `https://sora.services:49156/...` ✓
- `http_url_to_repo` = `https://sora.services:49156/...git` ✓
- `git clone https://oauth2:$PAT@sora.services:49156/...git` exit=0
- `ssl_verify_result=0` (trusted)

Gerrit REST + SSH:
- `https://sora.services:29420/projects/?d` returns 200 + correct project list
- `/config/server/info` reports anonymous URL `https://sora.services:29420/${project}` ✓
- `ssh -p 29418 claude-bot@sora.services gerrit version` → `gerrit version 3.13.5` ✓
- `ssh -p 29418 codex-bot@sora.services gerrit version` → ditto

**Related**

- [ADR 0002 — GitLab primary + GitHub mirror + Gerrit review](0002-gitlab-primary-github-mirror.md) — the architecture this ADR's TLS layer protects
- [ADR 0003 — Gerrit code review](0003-gerrit-code-review.md) — Gerrit canonicalWebUrl + listenUrl semantics
- [Phase 1-5 migration SOP](../sop/migration-plan-2026-05.md) — Phase 2 entry gate updated to reference this ADR
- `reference_gitlab_self_hosted.md` (memory) — Stage 2 HTTPS upgrade record
- `reference_gerrit_self_hosted.md` (memory) — HTTPS endpoint switch record

**Cert renewal calendar reminder**

Sectigo wildcard cert valid 1 year from issuance (2026-05-05 → expires ~2027-05). Reminder 60 days prior (2027-03-05) to verify auto-renew or initiate manual renewal. Operator owns this calendar entry.
