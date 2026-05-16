# Runbook — `OmniSightAlembicDrift` (Family ⑥ alembic image/DB drift)

**Owner**: on-call / deploy operator
**Alert**: `OmniSightAlembicDrift` (severity `page`, area `deployment`,
family `"6"`, defense dimension `D1`)
**Rule file**: `deploy/prometheus/rules/family6.yml`
**Spec source**: `docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`
**Binding ADR**: [`ADR-0036`](../architecture/ADR-0036-forward-only-deploy-invariant.md)
— forward-only-deploy invariant
**AlertBridge contract**: `docs/sop/alert-rule-contract.md`
**Cross-link (L2 rescue path)**: `v2-⑥-RescueCLI` — the operator-only
downgrade path described in §4 below.

---

## 1. What the alert means

`OmniSightAlembicDrift` fires when the backend image's bundled alembic
head disagrees with the live database's `alembic_version` row in the
**backward** direction — that is:

> `image_head < db_head` per the alembic revision graph.

In plain language: the running container's `backend/alembic/versions/`
directory does not contain a revision file the DB has already applied.
A query that references a column introduced by the missing revision
will 500 at runtime; the backend therefore *refuses to start*
(exit 78, `EX_CONFIG`) until the misalignment is resolved.

Two source signals drive the alert; the rule OR's them so detection
is robust regardless of which surface fires first:

| Source gauge                              | When it is 1                                                              | Producer                                  |
|---|---|---|
| `omnisight_alembic_drift{direction="backward"}` | Always-on 60 s collector observed `image_head < db_head`               | OP-1163 (`v2-⑥-1bc`) — `backend/metrics.py` |
| `omnisight_readyz_migrations_pending`     | Per-poll `/readyz` migration probe returned 503 with `migration_pending` | Pre-v2 readyz probe (`backend/routers/health.py`) |

The two sources are deliberately redundant: the always-on gauge is
the canonical Prometheus signal; the readyz probe is the legacy
per-poll surface that survives even if the always-on collector is
itself broken. AlertBridge dedupes them by `(alertname, family,
instance)` so the operator sees one page, not two.

## 2. Why severity is `page`

Backward drift is **not** a soft warning. Per ADR-0036:

* The image is the **canonical advance vector** for the schema. A
  backward image cannot apply the missing migrations forward
  (it does not have the source files) and cannot downgrade the DB
  backward (the same).
* Soft-recovering — "serve traffic for endpoints that don't touch the
  missing columns" — would hide the problem until the next code path
  tickled it. The 2026-05-14 retro
  (`docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`)
  shows exactly that failure mode: 118 consecutive failing healthchecks
  with no operator signal.
* The on-call needs to know **now**, not at the next deploy.

`warn` would be wrong; `info` would be catastrophically wrong.

## 3. Diagnose

### 3.1 Confirm direction and head pair

```bash
# Read the always-on gauge (preferred — it carries image_head / db_head
# as gauge labels per Family ⑥ §4.3 once OP-1167+ lands).
curl -fsS http://<backend-host>:8000/metrics \
  | grep '^omnisight_alembic_drift'

# Cross-check against the per-poll readyz body (always present today).
curl -fsS -o /tmp/readyz.json -w '%{http_code}\n' \
  http://<backend-host>:8000/readyz
jq '.migrations, .remediation' /tmp/readyz.json
```

The `migrations` block reports `drift_state` (`AHEAD` / `EQUAL` /
`BEHIND` / `UNKNOWN`) and the `image_head` / `db_head` pair. The
`remediation` block (Family ⑥ §5.2) lists the structured options
the operator can choose between.

### 3.2 Identify the offending instance

The alert's `instance` label (the `critical_label` per AlertBridge
§4.2) names the host or container that observed the drift. If
multiple Prometheus replicas scrape the same backend, the
deterministic `dedupe_key` ensures the operator sees one
notification per `(alertname, family, instance)` triple.

### 3.3 Distinguish backward-drift from `AHEAD` / `UNKNOWN`

The rule expression filters on `direction="backward"`; the
**`AHEAD`** state (image carries unapplied migrations) auto-resolves
via the startup hook (Family ⑥ §6) and does **not** fire this rule.
**`UNKNOWN`** (MANIFEST missing, DB unreachable, alembic graph
disjoint) is handled defensively as BEHIND for the `/readyz` 503
response — the always-on collector emits a separate
`omnisight_alembic_drift_unknown` gauge that a future
`OmniSightAlembicDriftStaleCollection` rule will surface.

## 4. Remediate

Two operator-actionable paths; the **forward path is preferred** per
ADR-0036. Choose deliberately — do not invoke both.

### 4.1 Preferred — deploy a newer image (no L2 fingerprint needed)

```bash
docker compose pull backend
docker compose up -d backend
# Wait for readyz to flip back to 200:
until curl -fsS http://localhost:8000/readyz | jq -e '.ready' > /dev/null; do
  sleep 5
done
```

This is the canonical forward-only path. The new image's startup
hook (Family ⑥ §6) acquires the advisory lock and runs
`alembic upgrade head` to converge `db_head` to `image_head` — but
only when `image_head >= db_head`, so re-pulling the image is the
right action when backward drift is the observed condition.

Authority required: none beyond normal operator deploy. Migration
files in the new image were reviewed in Gerrit at build time;
auto-applying does not bypass review.

### 4.2 Operator-only — `omnisight rescue drift` (downgrade DB)

If no newer image is reachable (network outage, GHCR down, hotfix
branch only), the operator may downgrade the DB to a known-safe
revision the current image **does** carry. This is the L2 rescue
path (`v2-⑥-RescueCLI`); it requires:

* L2 fingerprint per ADR-0033 (operator-authority audit).
* A backup row captured per §3.0.6 subcontract 5.
* The operator-window per ADR-0034 (separation of duties).

```bash
omnisight rescue drift \
  --confirm \
  --target <known-safe-rev>
```

The CLI refuses to run outside the operator window and refuses to
downgrade past the latest *lossless* migration available in the
running image. See the contract spec
(`docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`
§7) and ADR-0036 §"Specific binding consequences" for the full
authority model.

### 4.3 What NOT to do

* Do **not** swap the healthcheck from `/readyz` to `/livez` to
  "unstick" docker-compose. That was the 2026-05-14 lived-experience
  recovery; it zeroed in-band drift detection and is now an
  anti-pattern.
* Do **not** `docker run` with `--entrypoint sh` and manually
  `alembic upgrade head` against a backward image — the image does
  not have the missing revision files; the command will silently
  no-op and leave the DB ahead.
* Do **not** truncate `alembic_version` or write to it directly.

## 5. Aftermath / verification

Once the gauge returns to 0:

* AlertBridge emits the resolved-page envelope (AlertBridge §5):
  `[RESOLVED]` prefix on subject, severity downgraded to `info`,
  `resolution_duration` annotation populated.
* The deployment lifecycle dashboard (Family ⑤'s shipped-but-
  not-deployed audit) should also show a green row within one scrape.
* File a brief postmortem note linking back to the JIRA ticket if
  the BEHIND state lasted >30 min — that crosses the SLO threshold
  documented in `docs/sop/release-lifecycle-states.md`.

## 6. Why the alert exists at all (and why this runbook is short)

The forward-only-deploy invariant (ADR-0036) makes backward drift
**impossible to mask**: the AHEAD case auto-heals; the BEHIND case
refuses to start. The on-call's job is therefore narrow — pick the
forward path or the rescue path, then verify. There is no third
option, no soft recovery, no "wait and see". Short runbook = sharp
contract.

## 7. Related

* Spec: `docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`
* ADR: `docs/architecture/ADR-0036-forward-only-deploy-invariant.md`
* AlertBridge contract: `docs/sop/alert-rule-contract.md`
* Producer: `backend/metrics.py` (`omnisight_alembic_drift`)
* Producer: `backend/routers/health.py` (`/readyz` `remediation` field)
* Tests: `backend/tests/test_alert_rule_family6.py` (this rule's
  contract test)
* Lint: `scripts/promote-alert-rule.py` (gates rule additions in CI)
* Incident: `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`
