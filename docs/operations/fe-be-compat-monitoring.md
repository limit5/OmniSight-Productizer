# FE / BE bundle compatibility monitoring (OP-1483)

## Why this exists

On the morning of 2026-05-18, production silently ran
`backend=hotfix8` and `frontend=hotfix5` for several hours before
Codex spotted the skew during an unrelated investigation. The two
hotfixes happened to be internal-only, so user-visible damage was
zero — pure luck. A skew of the same shape across a contract-changing
release would have silently corrupted requests until on-call manually
checked image labels.

OP-1483 wires the runtime detector that would have flagged the
incident in under a minute. The chain:

1. The frontend (`lib/api.ts`) emits two diagnostic headers on every
   fetch:

   - `X-OmniSight-Frontend-Bundle: <bundle id baked at FE build time>`
   - `X-OmniSight-Frontend-Api-Contract: <api version FE was built against>`

2. The backend middleware
   (`backend/main.py::_frontend_compat_observer`) drops each
   observation into the in-process ring buffer maintained by
   `backend/frontend_compat.py`, and bumps the Prometheus counter
   `omnisight_fe_be_bundle_mismatch_total{fe_bundle, be_bundle}`
   when the FE bundle differs from the backend's own baked
   `bundle.json::bundle_id`.

3. `/readyz` surfaces `checks.frontend_compat_check` (operator
   eyeball view on a single replica).

4. The Prometheus rule
   `prometheus/rules/image-compat.yml::FEBEBundleMismatch` fires on
   `rate(...) > 0` over 5 minutes — i.e. the moment the first
   mismatched request lands.

5. The JIRA hotfix bridge
   (`backend/agents/fe_be_mismatch_bridge.py::handle_alert`)
   converts the Alertmanager webhook into a JIRA ticket labelled
   `runner-blocked:fe-be-mismatch`. The auto-runner refuses to pick
   up new TODO tickets while that label is set on an open ticket,
   so the deploy is the only thing the operator needs to fix —
   they don't also need to babysit the runner.

## What triggers the alert

`FEBEBundleMismatch` fires when:

- The backend has received at least one request in the last 5 minutes
  whose `X-OmniSight-Frontend-Bundle` header value is **different from
  the backend's own `bundle_id`** (the one baked into the image at
  build time, served via `GET /api/version`).
- The mismatch counter is process-wide and cumulative — even if the
  ring buffer rolls the offending sample out, the cumulative count in
  `omnisight_fe_be_bundle_mismatch_total` survives until process
  restart.

## Triage

### Step 1 — confirm the skew

```bash
curl -s http://<backend-host>/readyz | jq .checks.frontend_compat_check
```

Expect a JSON object with `ok: false`, `observed_fe_bundles` listing
the offending bundle id(s), and `mismatch_count > 0`. If `ok: true`,
the alert may already be recovering — re-run after 30 s; otherwise
the request middleware is not seeing FE headers (next step).

### Step 2 — confirm both sides' bundle ids

```bash
# What does the backend think it is?
curl -s http://<backend-host>/api/version | jq '{bundle_id,api_required,db_migration_head}'

# What is the FE container labelled as?
docker inspect <frontend-container> --format \
  '{{ index .Config.Labels "org.opencontainers.image.bundle.id" }}'
```

Both labels should match. If they don't, the deploy is genuinely
skewed — proceed to Step 3.

### Step 3 — reconcile the deploy

The operator path is one of:

- **Roll the lagging side forward** — re-deploy the image whose
  `bundle_id` is older. Bundle ids are sortable by build_time inside
  `bundle.json` so "older" is unambiguous.
- **Roll the leading side back** — if the newer image has a known
  defect (which is *why* the older side is still around), pin the
  newer side back to the older bundle id and follow the SOP at
  `docs/operations/deployment.md` §"emergency rollback".

Either way: the alert clears as soon as no inbound request in the
last 5 minutes still carries the mismatched FE bundle id. The JIRA
ticket auto-filed by the bridge should be closed to release the
`runner-blocked:fe-be-mismatch` runner gate.

### Step 4 — verify the runner gate has lifted

```bash
# Should show ZERO open tickets carrying the runner-block label.
gh jira-cli list --jql \
  'labels = "runner-blocked:fe-be-mismatch" AND status not in (Done, Closed, Resolved)'
```

## Single-replica vs. fleet-wide views

| Surface | What it sees | When to use |
|---------|--------------|-------------|
| `/readyz` `checks.frontend_compat_check` | Last 100 observations on **one** replica | Eyeball on a single replica during triage |
| `omnisight_fe_be_bundle_mismatch_total` | Cumulative count across the fleet | Prometheus / Grafana / alerting |
| `omnisight_fe_be_bundle_mismatch_total{fe_bundle,be_bundle}` series | Which pairs are skewed | Building the JIRA ticket annotations |

In **prod/dev** `/readyz` returns 200 even on mismatch — by design
(OP-1483 NON-GOALS: "Do NOT block requests on mismatch — alert only,
don't degrade UX further"). The page-out source is the Prometheus
alert; readyz is the operator-curl surface.

In **staging** the same check is a HARD gate (RT-05c / OP-1575). The
staging compose sets `OMNISIGHT_REQUIRE_FRONTEND_COMPAT`, which flips
`frontend_compat_check.gate_enforced` to `true`; an observed skew
(`ok: false`) then makes `/readyz` return **503**. The staging canary
suite (`backend/agents/staging_gate.py`, which GETs `/readyz`) goes red
on that 503, so a skewed bundle is never promoted out of staging. See
the section below.

## Reverse-test (incident replay)

To verify the alert wiring end-to-end without staging a real skew:

```bash
# 1. Start the backend stack.
docker compose up -d backend frontend

# 2. Force the FE to claim a different bundle id than the backend has.
docker compose exec frontend env \
  NEXT_PUBLIC_BUNDLE_ID=v0.5.0-rc2-FAKE-3hotfixesbehind \
  node server.js &

# 3. Drive one request through the FE.
curl -s -H "X-OmniSight-Frontend-Bundle: v0.5.0-rc2-FAKE-3hotfixesbehind" \
       -H "X-OmniSight-Frontend-Api-Contract: v1" \
       http://localhost:8000/api/version

# 4. Within 60 s, /readyz should report ok=false:
curl -s http://localhost:8000/readyz | jq .checks.frontend_compat_check
```

This reproduces the "FE 3 hotfixes behind BE" scenario named in
OP-1483 AC#4 without staging a real cross-image deploy.

## Configuration knobs

- `NEXT_PUBLIC_BUNDLE_ID` (frontend build env): overrides the bundle
  id the FE advertises. Default flow seeds this from
  `bundle.json::bundle_id` via `next.config.mjs`.
- `NEXT_PUBLIC_API_CONTRACT` (frontend build env): API version the FE
  was built against. Default `"v1"` from
  `bundle.json::contracts.frontend_built_against_api`.
- `OMNISIGHT_FE_BE_BRIDGE_AGENT_CLASS` (backend env): the JIRA agent
  identity the bridge daemon files tickets as. Defaults to
  `subscription-merger` — see `backend/agents/fe_be_mismatch_bridge.py`
  for the rationale.

## Why mismatch is observation-only in prod, a hard gate in staging

In **prod** the ticket's NON-GOALS bullet is explicit: blocking
requests on a mismatch is a UX regression on top of the already-broken
deploy. The operator's job is to fix the deploy, not the user's job to
retry past a 503. So in prod (and dev/CI) the alert + JIRA bridge are
the only feedback loop and `/readyz` stays 200.

In **staging** the calculus is the opposite: there are no real users to
protect, and the entire point of staging is to catch a skew *before* it
reaches prod. RT-05c (OP-1575) therefore makes the check a hard gate
there. The toggle is the `OMNISIGHT_REQUIRE_FRONTEND_COMPAT` env flag
(same shape as RT-08's `OMNISIGHT_REQUIRE_DEPLOY_OVERLAY`): the staging
compose sets it, prod/dev/CI leave it unset. When set, an observed FE/BE
bundle skew flips `/readyz` to 503, which reds the staging canary gate
and blocks promote. The gate fires only on an *observed* skew — a
freshly-booted staging replica that has not yet seen the injected probe
still reports `ok: true` and stays ready.
