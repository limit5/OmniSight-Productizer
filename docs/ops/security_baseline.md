# OmniSight Security Baseline — Pre-Deployment Checklist

This checklist must be completed before exposing an OmniSight instance to any
network beyond localhost.

---

## 1. Authentication Mode

| Env var | Required value |
|---|---|
| `OMNISIGHT_AUTH_MODE` | `strict` |
| `OMNISIGHT_ENV` | `production` |

Setting `OMNISIGHT_ENV=production` with any auth mode other than `strict` will
**refuse to start** (exit code 78 — `EX_CONFIG`).

```bash
# docker-compose.prod.yml already ships these defaults.
OMNISIGHT_AUTH_MODE=strict
OMNISIGHT_ENV=production
```

---

## 2. Admin Password

The bootstrap admin ships with password `omnisight-admin`. On first boot the
system flags `must_change_password=1` and **every API call returns 428
Precondition Required** until the password is changed via:

```http
POST /api/v1/auth/change-password
Content-Type: application/json

{
  "current_password": "omnisight-admin",
  "new_password": "<strong-passphrase-12+-chars>"
}
```

Best practice: set `OMNISIGHT_ADMIN_PASSWORD` in `.env` to a strong value
**before first boot** so the flag is never set.

```bash
OMNISIGHT_ADMIN_PASSWORD="$(openssl rand -base64 24)"
```

---

## 3. Bearer Token (Service-to-Service)

> **DEPRECATED — FX2.D4.4 / OP-237 (2026-05-16).**
> `OMNISIGHT_DECISION_BEARER` is on the sunset path. The supported
> replacement is the K6 `api_keys` table — issue per-service `omni_*`
> bearers from **Admin UI → API Keys** (each scoped, hashed, auditable,
> revocable without a backend restart). The legacy env var is
> auto-migrated into a hashed `ak-legacy-<sha[:12]>` row on first
> boot (`backend.api_keys.migrate_legacy_bearer`), so existing CI
> scripts continue to authenticate while you cut over.
>
> Removal of the env-var fallback is targeted at the next major
> release. New deployments should not use `OMNISIGHT_DECISION_BEARER`
> at all; existing deployments should plan a one-cycle migration:
>
> 1. Boot once with the env var present so the legacy row is written.
> 2. Create per-service keys via Admin UI > API Keys.
> 3. Roll callers onto the new keys.
> 4. Unset `OMNISIGHT_DECISION_BEARER`.
>
> See `docs/security/as_0_4_credential_refactor_migration_plan.md`
> Track B for the full expand-migrate-contract recipe.

The `OMNISIGHT_DECISION_BEARER` token allows CI/CD pipelines and internal
services to call Decision Engine mutator endpoints without a session cookie.

| Rule | Detail |
|---|---|
| Minimum length | 16 characters (128-bit entropy) |
| Scope | Restrict to CI runner IPs via reverse-proxy allowlist |
| Rotation | Rotate quarterly; revoke immediately on compromise |
| Status | **Deprecated** — prefer K6 `api_keys` per-service tokens |

```bash
OMNISIGHT_DECISION_BEARER="$(openssl rand -base64 32)"
```

**IP allowlist (Cloudflare / nginx example):**

```nginx
# Only allow bearer-authenticated requests from CI runners
location /api/v1/decisions/ {
    allow 10.0.0.0/8;     # internal CI network
    deny  all;
    proxy_pass http://backend:8000;
}
```

---

## 4. Cookie Security

| Env var | Required value | Why |
|---|---|---|
| `OMNISIGHT_COOKIE_SECURE` | `true` | Prevents session cookie leaking over HTTP |

Set this once HTTPS termination is in place (Cloudflare Tunnel, nginx TLS,
etc.).

---

## 5. CORS Origins

```bash
OMNISIGHT_FRONTEND_ORIGIN=https://your-domain.com
# Additional origins (comma-separated):
OMNISIGHT_EXTRA_CORS_ORIGINS=https://staging.your-domain.com
```

Never leave the default `http://localhost:3000` in production.

---

## 6. Docker Runtime

| Env var | Recommended value |
|---|---|
| `OMNISIGHT_DOCKER_RUNTIME` | `runsc` (gVisor) |

gVisor provides a user-space kernel that blocks most container-escape CVEs.
Ensure `runsc` is installed on the host (`runsc --version`).

---

## 7. Summary Checklist

- [ ] `OMNISIGHT_ENV=production`
- [ ] `OMNISIGHT_AUTH_MODE=strict`
- [ ] `OMNISIGHT_ADMIN_PASSWORD` set to a strong passphrase (not `omnisight-admin`)
- [ ] `OMNISIGHT_DECISION_BEARER` set (16+ chars), access restricted to CI allowlist IPs *(deprecated — see §3; prefer per-service K6 keys via Admin UI → API Keys, sunset target = next major)*
- [ ] `OMNISIGHT_COOKIE_SECURE=true`
- [ ] `OMNISIGHT_FRONTEND_ORIGIN` set to production domain
- [ ] Default admin password changed after first login
- [ ] `OMNISIGHT_DOCKER_RUNTIME=runsc` (gVisor installed on host)
- [ ] Docker socket access restricted (rootless Docker or socket proxy)
- [ ] Database volume backed up and encrypted at rest
