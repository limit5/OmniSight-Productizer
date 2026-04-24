# Cloudflare Dashboard Settings — `sora-dev.app`

**Last configured**: 2026-04-19 by `user`
**Zone**: `sora-dev.app`
**Plan**: Free (items marked **[Pro]** deferred until upgrade)
**Active subdomain**: `ai.sora-dev.app` → CF Tunnel → `frontend:3000`

This document records every setting applied to the `sora-dev.app` zone for
OmniSight's production go-live. It is the reference for:

1. Verifying the posture is intact after any dashboard change
2. Rebuilding the zone from scratch if the account is ever reset
3. Understanding *why* each value was chosen (future you / future operator)

The command-line post-mortem of the go-live that drove these choices is
[`deploy_postmortem_2026-04-19.md`](./deploy_postmortem_2026-04-19.md); the
security audit that justified the specific WAF + rate-limit rules is in
that post-mortem's "security follow-ups" section.

---

## Index

| § | Area | What was done | Why |
|---|---|---|---|
| 1 | DNS | `ai` CNAME proxied | Tunnel target + CF edge active |
| 2 | SSL/TLS | Full (strict) + HSTS 6mo | Browser → CF TLS locked down |
| 3 | Security Settings | Medium + Bot Fight Mode on | Bot baseline |
| 4 | WAF Managed Rules | Cloudflare Managed Ruleset | OWASP-ish coverage |
| 5 | WAF Custom Rules | Challenge empty UA (script-kid filter) | Was 2 rules; Rule 5.1 `Block-direct-system-endpoints` removed 2026-04-24 as redundant (§9 Access is the authoritative admin-path gate) |
| 6 | Rate Limiting | `/auth/login` throttle | Brute-force deterrent |
| 7 | Scrape Shield | Email obfuscation + hotlink protection | Small content-protection wins |
| 8 | Tunnel | `ai.sora-dev.app → http://frontend:3000` | Origin is Next.js, NOT Caddy |
| 9 | Zero Trust Access | Email-OTP gate on `/api/v1/system/*` | Belt-and-braces on top of backend's new `require_role("admin")` |
| 10 | Notifications | L7 DDoS alert email | Reactive coverage |

---

## § 1 — DNS

**Path**: `dash.cloudflare.com` → `Websites` → `sora-dev.app` → `DNS` → `Records`

### Settings applied

| Field | Value |
|---|---|
| Type | `CNAME` |
| Name | `ai` |
| Target | `<tunnel-id>.cfargotunnel.com` (auto-created by Zero Trust when the Public Hostname was added) |
| Proxy status | **Proxied** (orange cloud 🟠) |
| TTL | Auto |

### Why

- **Proxied (orange cloud) is mandatory** — a grey cloud would leak the
  origin IP and bypass every other setting in this document. An attacker
  with the origin IP can DDoS around CF.
- Target is CF's CNAME for the tunnel; managed automatically by the Zero
  Trust "Public Hostname" flow when the user added `ai.sora-dev.app`.

### Root `sora-dev.app` note

The root domain record was NOT touched — it has a pre-existing A/CNAME
that blocked creating a root Public Hostname. We used the `ai` subdomain
instead. If a future need wants the root:

```bash
# Find what's on root
# (do this in the dashboard DNS Records page, filter Name=@ or sora-dev.app)
```

Then either delete that record (if a placeholder) or repoint it to the
tunnel CNAME.

### Verify

```bash
curl -sI https://ai.sora-dev.app/ | grep -iE 'server|cf-ray'
# Expect:
#   server: cloudflare
#   cf-ray: <id>-SIN   (region varies)
```

The `server: cloudflare` header is proof-of-proxy — if DNS is grey-cloud
this header will be `server: Caddy` or similar.

---

## § 2 — SSL/TLS

**Path**: `Websites` → `sora-dev.app` → `SSL/TLS`

### 2.1 Overview → Encryption mode

| Setting | Value | Why |
|---|---|---|
| SSL/TLS encryption mode | **Full (strict)** | CF edge to origin is encrypted; `strict` validates the origin cert. With CF Tunnel the "origin" is actually the tunnel endpoint, and CF fully trusts it. Any less strict mode permits a MITM between CF and origin, which defeats the point of the tunnel. |

### 2.2 Edge Certificates

| Setting | Value | Why |
|---|---|---|
| Always Use HTTPS | **On** | Force HTTP → HTTPS redirect at the edge. Saves one Caddy round-trip. |
| Minimum TLS Version | **TLS 1.2** | 1.0/1.1 are deprecated + not PCI-compliant. 1.3 as a minimum breaks some old mobile/embedded clients, 1.2 is the right balance. |
| Opportunistic Encryption | **On** | Lets HTTP/1.0 clients opportunistically upgrade to TLS. Free perf + security. |
| TLS 1.3 | **On** | Enables for capable clients while 1.2 stays as minimum. |
| Automatic HTTPS Rewrites | **On** | Patches mixed-content references (`http://` inside HTML) to `https://`. |

### 2.3 HSTS

| Setting | Value | Why |
|---|---|---|
| Enable HSTS | **Yes** |  |
| Max Age Header | **6 months** | 12 months would lock us in longer if we ever need to stop serving TLS (unlikely, but keep the exit hatch short on first rollout). |
| Apply HSTS policy to subdomains | **Yes** | `includeSubDomains` — covers `ai.`, any future `api.`, etc. |
| Preload | **No** ❌ | **Deliberately OFF.** Preload list submission is hard to revoke — requires months of patience to unlist. Leave off until ops is confident we'll always serve HTTPS on this hostname. |
| No-Sniff Header | **Yes** | Adds `X-Content-Type-Options: nosniff` — mitigates MIME-type confusion XSS. |

### Verify

```bash
# HSTS header present on 200 responses
curl -sI https://ai.sora-dev.app/login | grep -i 'strict-transport-security'
# Expect:
#   strict-transport-security: max-age=15552000; includeSubDomains

# TLS 1.1 connection refused
curl -sI --tls-max 1.1 https://ai.sora-dev.app/ 2>&1 | head -3
# Expect curl error "alert protocol version" or similar
```

> Note: HSTS header can take 5–10 min to propagate globally after enabling
> in CF. If the verify above returns empty the first time, wait a bit.

---

## § 3 — Security Settings

**Path**: `Websites` → `sora-dev.app` → `Security` → `Settings`

| Setting | Value | Why |
|---|---|---|
| Security Level | **Medium** | High challenges a lot of legitimate users. Medium blocks known-bad IPs + challenges suspicious ones. Appropriate default for a typical B2B tool. |
| Challenge Passage | **30 min** | How long a human who solved a challenge is trusted before being re-challenged. 30 min is the CF default for Medium and balances UX vs. session theft. |
| Browser Integrity Check | **On** | Blocks requests missing/forging common browser headers — cheap bot filter. |

**Not set** (deliberate):
- *Privacy Pass Support* — not surfaced on all dashboards; CF is
  deprecating this. No action needed.

### 3.5 Bots

**Path**: `Security` → `Bots`

| Setting | Value | Why |
|---|---|---|
| Bot Fight Mode | **On** | Free-tier bot protection. Blocks the most obvious automated traffic. |
| Super Bot Fight Mode | **[Pro]** deferred | Pro+ only; adds per-classification action granularity. |
| AI Scrapers and Crawlers | **[Pro]** deferred | Pro+ only; blocks LLM training scrapers (GPTBot / ClaudeBot / PerplexityBot). Our service is an AI-agent platform but our OUTBOUND calls to Anthropic don't go through CF — only inbound human traffic. Pro upgrade path: turn this on; it won't affect normal users. |

---

## § 4 — WAF Managed Rules

**Path**: `Security` → `WAF` → `Managed rules`

| Ruleset | State | Why |
|---|---|---|
| Cloudflare Managed Ruleset | **Enabled** (default) | CF's curated protection — includes common SQLi / XSS / well-known-CVE rules. Free tier has this on by default; confirming it's active. |
| OWASP Core Ruleset | **[Pro]** deferred | Pro+ feature. Covers the canonical OWASP Top-10 signatures beyond what CF Managed already does. Upgrade-path: sensitivity Medium, action Block, PL2. |

---

## § 5 — WAF Custom Rules

**Path**: `Security` → `WAF` → `Custom rules`

Free tier allows 5 custom rules total. We use **1** (was 2 until 2026-04-24; Rule 5.1 removed as redundant — see below).

### Rule 5.1 — `Block-direct-system-endpoints` ⚠️ **REMOVED 2026-04-24**

> **Status**: Rule deleted from CF dashboard on 2026-04-24 as part of A3 row 58
> follow-up. Kept here for historical context only — **do not re-create**
> without first reviewing the §9 Access overlap discussion below.

**Why removed**: After Phase-3 P6 (commit `0e0bcd46`, 2026-04-20) atomically
renamed all `/api/v1/system/*` backend routes to `/api/v1/runtime/*`, the
`/api/v1/system/*` path has **no live backend handler** — both the WAF
block (§5.1) and the Zero Trust Access gate (§9) were shadowing a dead
path. During P7 cleanup, the WAF Managed Challenge was actively harming
logged-in operator flows (diagnostic trail in `HANDOFF.md` 2026-04-20 P5/P6
entries) — each dashboard load that still linked to the old path got a
CF Challenge HTML response that the frontend mis-parsed as a 403,
triggering the "ERR 發生錯誤" ErrorBoundary cascade. Post-P6 the cascade
symptom was solved by path rename, but Rule 5.1 kept firing silently on
stale bookmarks and external link-tests — pure cost with zero benefit
because §9 Zero Trust Access already covers the same path with a
cleaner UX (SSO 302 instead of JS Managed Challenge).

**Single authoritative gate now**: §9 Zero Trust Access (email-OTP SSO)
is the one path-level gate on `/api/v1/system/*`. Backend admin endpoints
live at `/api/v1/runtime/*` and are gated by `Depends(auth.require_role("admin"))`
+ the `auth_baseline` middleware (401 for non-authenticated).

**Verification (2026-04-24)**:
```
$ curl -sI https://ai.sora-dev.app/api/v1/system/info
HTTP/2 302
location: https://omnisight-dev.cloudflareaccess.com/cdn-cgi/access/login/...
cf-mitigated: (empty)
# NO "Just a moment..." Managed Challenge HTML — confirmed grep match = 0
```

**Prior rule (for history only)**:

| Field | Value |
|---|---|
| ~~When incoming requests match~~ | ~~`URI Path` `starts with` `/api/v1/system/`~~ |
| ~~Then take action~~ | ~~**Managed Challenge**~~ |

### Rule 5.2 — `Challenge-empty-ua-on-api`

| Field | Value |
|---|---|
| When | (`URI Path` starts with `/api/v1/`) **And** (`User Agent` equals `` empty string) |
| Action | **Managed Challenge** |

**Why**: Cheap script-kid filter. Real browsers and legitimate tools
(curl, Python requests, Next.js SSR) all set a User-Agent by default.
An empty UA is almost always a dumb scanner.

### Rules NOT added (capacity reserved)

3 slots remain. Candidates the audit flagged but we didn't use yet:

- **Block non-Taiwan** sources (if our user base is regional) — good
  trimming for public-internet scanning but risks locking out travel
  + VPN users. Revisit once we know who our real users are.
- **Block known-bad ASNs** — Shodan / Censys / common scan clouds.
  Low false positive but very narrow benefit.

---

## § 6 — Rate Limiting

**Path**: `Security` → `WAF` → `Rate limiting rules`

Free tier allows 1 rate-limit rule. Pro adds 4 more (total 5).

### Rule 6.1 — `throttle-login`

| Field | Value |
|---|---|
| If incoming requests match | `URI Path` equals `/api/v1/auth/login` |
| When rate exceeds | **10 requests per 1 minute per IP** |
| Then take action | **Block** for **10 minutes** |

**Why**: Defends against brute-force password guessing. The backend
also has `OMNISIGHT_LOGIN_MAX_ATTEMPTS=5` at the application layer
(per-email lockout), but that's per-user. This rule stops per-IP
credential-stuffing where the attacker tries many different emails
fast. The 10/min threshold is above any plausible human retry pattern
(a person mistyping a password 3 times in a minute is still allowed).

### Rule 6.2 `throttle-api-global` — **[Pro]** deferred

When upgraded to Pro, add:

| Field | Value |
|---|---|
| If | `URI Path` starts with `/api/v1/` |
| When | 100 req / 1 min per IP |
| Action | Managed Challenge for 1 minute |

Generic API-wide DoS throttle.

### Verify

```bash
# Burst 20 login attempts; rule 6.1 should block after 10
for i in $(seq 1 20); do
  curl -sS -o /dev/null -w "%{http_code} " -X POST https://ai.sora-dev.app/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"email":"test@example.com","password":"x"}'
done; echo
# Expect first ~10 are 401/503, then subsequent requests 429
```

Only run this during a maintenance window (it will lock legitimate
login attempts from your IP for 10 min).

---

## § 7 — Scrape Shield

**Path**: `Websites` → `sora-dev.app` → `Scrape Shield`

| Setting | Value | Why |
|---|---|---|
| Email Address Obfuscation | **On** | Auto-obfuscates `@`-containing strings in HTML before serving — cheap anti-spam-harvester. Our pages shouldn't contain raw emails anyway, but this is a safety net. |
| Server-side Excludes | *(not visible on this plan/UI — skipped)* | Free plan doesn't always surface this. No action needed. |
| Hotlink Protection | **On** | Blocks third-party sites from embedding our static images directly. Irrelevant for an app like this (no public static content), but costs nothing and protects against bandwidth theft if we ever add marketing assets. |

---

## § 8 — Tunnel Configuration Review

**Path**: Zero Trust dashboard → `one.dash.cloudflare.com` → `Networks` → `Tunnels` → our tunnel (ID starts `7c8aff25`)

### Public Hostname

| Field | Value |
|---|---|
| Subdomain | `ai` |
| Domain | `sora-dev.app` |
| Path | *(empty)* |
| Service — Type | **HTTP** |
| Service — URL | `frontend:3000` |

**Why `frontend:3000` (not `caddy:80` / `caddy:8000` / `caddy:443`)**:
the Caddyfile's `:443` and `:8000` blocks ONLY reverse-proxy to the
backend pool — they do not serve the Next.js UI. A request for `/`
going to `caddy` would return the backend's JSON root response
(`{"name":"OmniSight Engine",…}`), not the UI. Only `frontend:3000`
(Next.js standalone) serves the user-facing pages, and its SSR-side
API calls proxy through `http://caddy:8000` to the backend pool
internally via the `BACKEND_URL` env.

### Additional Application Settings

| Field | Value | Why |
|---|---|---|
| HTTP Host Header | *(empty)* | Don't rewrite Host. Caddy + the bootstrap middleware both look at the incoming Host header; overriding it here would hide the real hostname. |
| Connect Timeout / Read Timeout | defaults | No tuning needed. Next.js cold-start (dev) is the worst case and fits default. |
| HTTP/2 | **On** | Default; enables multiplexing to origin. |
| No TLS Verify | *(not shown — HTTP origin has no TLS to verify)* | Irrelevant for our `http://frontend:3000` origin. Only matters if origin URL is `https://`. |
| Origin Server Name | *(empty)* | See above — origin is HTTP so no SNI to set. |

---

## § 9 — Zero Trust Access (Email-OTP Gate on Admin API)

**Path**: `one.dash.cloudflare.com` → `Access` → `Applications` → our `OmniSight Admin` application

| Field | Value |
|---|---|
| Application type | Self-hosted |
| Application name | `OmniSight Admin` |
| Session Duration | **24 hours** |
| Subdomain | `ai` |
| Domain | `sora-dev.app` |
| Path | `api/v1/system` |
| Identity providers | **One-time PIN** (email OTP, built-in, no config) |
| Policy — name | `operator-email` |
| Policy — Action | **Allow** |
| Policy — Include | **Emails** → operator's email |

**Why this layer exists**:

There are now THREE layers in front of `/api/v1/system/*`:

1. **CF edge** (§5.1) — Managed Challenge blocks bot/script traffic
2. **CF Access** (§9) — Email-OTP pin required for legitimate users
3. **Backend auth** (commit `e2d981ff`) — session cookie + `role=admin` dep

A single-layer failure (CF rule accidentally disabled, session token
leaked, backend auth regression) leaves at least 2 other layers. We
explicitly want belt-and-braces here because this router exposes
hardware-deploy and pipeline-advance endpoints — consequence of
compromise is high.

### Testing Access

```bash
# With NO session and NO CF Access cookie, /api/v1/system/* should
# get the CF Access login page
curl -sI https://ai.sora-dev.app/api/v1/system/debug -o /dev/null -w "%{http_code}\n"
# Expect: 302 (to CF login) or 403 depending on CF edge ruleset order
```

### Operator experience

When you (as admin) hit any `/api/v1/system/*` URL in your browser:
1. CF edge may challenge you (rule 5.1) — solve it once
2. CF Access shows a "sign in with email" page — enter your email,
   get a 6-digit OTP, paste it
3. You're redirected back; cookie lasts 24 hours (session duration)
4. Backend finally sees the request + checks your admin session

For CLI automation, CF Access has a `cloudflared access login`
workflow that caches a JWT. Document if you need it — skipped for
now (we don't have CLI admin flows yet).

---

## § 10 — Notifications

**Path**: `dash.cloudflare.com` (account level) → `Notifications`

| Notification | State | Why |
|---|---|---|
| DDoS Attack L7 | **On**, email to operator | Alert on CF-detected layer-7 DDoS. Free includes L3/L4 mitigation "silently"; L7 alerts tell you when HTTP-level is under attack. |
| Origin Monitoring / Health Checks | Not configured — free tier doesn't offer active origin health checks | Upgrade path: Pro adds Health Checks that you can point at `https://ai.sora-dev.app/api/v1/health`. |

---

## Overall restore-from-scratch checklist

If the CF account is ever rebuilt from scratch and needs to match this
state:

```
DNS                  § 1   1 line
SSL/TLS              § 2   3 subsections (encryption / edge cert / HSTS)
Security baseline    § 3   3 sliders + Bot Fight Mode
WAF Managed          § 4   1 toggle (enabled default)
WAF Custom Rules     § 5   1 rule (was 2; Rule 5.1 removed 2026-04-24)
Rate Limiting        § 6   1 rule (Free) / 2 rules (Pro)
Scrape Shield        § 7   2 toggles
Tunnel Public Hostname   § 8   1 entry
Zero Trust Access    § 9   1 Application + 1 Policy
Notifications        § 10  1 email alert
```

Total clicks: ~45.

---

## Pro-tier upgrade checklist (do when you upgrade)

When the account upgrades to Pro, turn these on:

- [ ] § 3.6 Super Bot Fight Mode — Definitely automated = Block, Likely
      automated = Managed Challenge, Verified bots = Allow
- [ ] § 3.7 AI Scrapers and Crawlers = Block
- [ ] § 4 Cloudflare OWASP Core Ruleset = Enabled, Sensitivity Medium,
      Action Block, Paranoia PL2
- [ ] § 6.2 Rate limit: `/api/v1/*` global throttle @ 100/min → Managed
      Challenge 1min
- [ ] § 10 Origin Monitoring on `/api/v1/health`

---

## Linked docs

- Architecture of the stack being protected: [`multi-wsl-deployment.md`](./multi-wsl-deployment.md)
- Go-live that configured all of this: [`deploy_postmortem_2026-04-19.md`](./deploy_postmortem_2026-04-19.md)
- Security audit that justified the WAF + rate-limit + Access choices:
  same post-mortem, "security follow-ups" section
- Dev + prod coexistence concerns: [`dev_prod_coexistence.md`](./dev_prod_coexistence.md)
