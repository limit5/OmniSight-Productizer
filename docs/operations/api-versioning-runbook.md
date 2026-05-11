# API Versioning + Backwards-Compatibility Runbook (OP-885)

Sprint D D13 (META OP-761 Phase 3).

This runbook documents the day-to-day operator workflow for:

- choosing between `/api/v1/...` and `/api/v2/...`,
- the `api-compat` CI gate that refuses breaking changes,
- the `api:approved-breaking` override trailer,
- per-endpoint deprecation headers and the 90-day client window,
- the rollback paths for each step.

## 1. Versioning policy

| Surface         | Status     | When to use                                                       |
|-----------------|------------|-------------------------------------------------------------------|
| `/api/v1/...`   | Deprecated | Default. Every existing route is mounted here.                    |
| `/api/v2/...`   | Active     | New shapes that **break** v1. Add a new route under v2.           |
| `/api/version`  | Active     | Version metadata — clients call this to negotiate compatibility.  |

Rules:

1. **Additive changes** (new optional field, new endpoint, new optional
   query param) ship under the same version. No CI gate flip required.
2. **Breaking changes** (removed field, renamed endpoint, newly-required
   field) ship under a higher version. The old route stays mounted
   under the old version until its `Sunset` date passes.
3. **Version sunset.** v1 has a global sunset configured in
   `backend.api_versioning.V1_SUNSET_HEADER`. Individual endpoints can
   announce their own sunset via
   `register_endpoint_deprecation(path, ...)` — useful when retiring a
   single route earlier than the full version.

The version split is **by URL prefix on purpose** — header-based
versioning hides the contract from caches, intermediaries, and the
FastAPI OpenAPI generator. Do not introduce an `Accept` / `X-API-Version`
versioning scheme.

## 2. The `api-compat` CI gate

Workflow: `.github/workflows/api-compat.yml`
Script:   `scripts/check_api_compat.py`

The gate runs on every PR that touches `openapi.json`, `backend/main.py`,
`backend/api_versioning.py`, `backend/routers/**`, the script itself,
or the workflow file. It does:

1. Resolve the OpenAPI spec at the PR base ref (via `git show <base>:openapi.json`).
2. Resolve the OpenAPI spec at the PR head (the working tree if HEAD).
3. Diff the two for breaking-change kinds:
   - **`removed_endpoint`** — a `(path, method)` pair disappeared.
   - **`renamed_endpoint`** — an `operationId` moved to a different path/method.
   - **`removed_field`** — a property (request, response, or query parameter) disappeared.
   - **`required_new_field`** — a property was added to a request schema (or query parameter) with `required: true`, or an existing field flipped optional → required.
4. Scan the PR's commit messages for the override trailer
   (`api:approved-breaking` bare token, or `API-Approved-Breaking:` Gerrit-style).
5. Exit:
   - **0** — clean, OR breaking present but override trailer found.
   - **1** — `APIBreakingChangeRefused` — breaking change, no trailer.
   - **2** — `OpenAPIDiffParseFailed` — degrade to manual review.

The full JSON report is written to `artifacts/api-compat-report.json`
and uploaded as a CI artifact for 30 days.

### 2.1 What the diff catches

| Change                                           | Verdict       |
|--------------------------------------------------|---------------|
| Add `GET /api/v1/new`                            | safe          |
| Add optional field `metadata` to response        | safe          |
| Add optional field `metadata` to request body    | safe          |
| Add **required** field `tenant_id` to request    | **breaking**  |
| Flip request field optional → required           | **breaking**  |
| Remove `legacy_count` from response              | **breaking**  |
| Move `/api/v1/foo` → `/api/v1/bar` (same opId)   | **breaking** (renamed) |
| Delete `DELETE /api/v1/projects/{id}`            | **breaking**  |
| Tighten `maxLength` constraint                   | not detected (yet — see §6) |
| Loosen `required: true` → `false`                | safe          |

### 2.2 Local invocation

```bash
# Default — diff origin/main against your working tree
python scripts/check_api_compat.py

# Diff two arbitrary refs
python scripts/check_api_compat.py --base-ref v1.4.0 --head-ref HEAD

# Diff two on-disk snapshots (useful when scripting)
python scripts/check_api_compat.py \
    --base-file /tmp/before.json --head-file /tmp/after.json

# Bootstrap mode — accept that the base spec is missing, treat as
# "no prior contract" (exit 0 instead of OpenAPIDiffParseFailed)
python scripts/check_api_compat.py --allow-missing-base
```

## 3. Shipping a breaking change

Breaking changes are a procedural step, not a CI bypass.

1. **Decide if you really need to.** Adding a new endpoint under `/api/v2`
   is almost always cheaper than breaking `/api/v1`. Try that first.
2. If yes, write the change as normal. Re-run `python scripts/dump_openapi.py`
   to refresh `openapi.json`.
3. The CI gate will fail with `APIBreakingChangeRefused` and a JSON
   report listing each finding.
4. Add an `api:approved-breaking` trailer to your commit message, with
   a one-line reason. Either form is accepted:

   ```text
   [OP-XXX] Drop the legacy stream_count field

   The field has always returned 0 since OP-541. Confirmed unused by
   the dashboard and by every external integrator surveyed in OP-803.

   api:approved-breaking: removed dead field after consumer survey
   ```

   …or the Gerrit-style:

   ```text
   API-Approved-Breaking: removed dead field after consumer survey
   ```

5. Push again. The gate re-runs, detects the trailer, and lets the PR
   proceed.
6. **Announce.** In the PR description, link the migration guide for
   downstream clients. The frontend (`/api/version` consumer) and any
   bundled mobile build must be updated before the v1 sunset date.

The override is logged: the CI artifact records both the trailer text
and the diff findings, so the audit trail is complete.

## 4. Deprecating an individual endpoint (90-day window)

When you don't want to bump the entire API version but a specific route
is being retired, use the per-endpoint deprecation registry instead of a
v2 promotion.

```python
# In backend/main.py or a router setup function
from backend.api_versioning import register_endpoint_deprecation

register_endpoint_deprecation(
    path="/agents/legacy_status",
    method="GET",                  # or "*" to mark every verb
    # sunset_date omitted → defaults to now + 90 days
)
```

Effect on every response to `GET /api/v1/agents/legacy_status` (and the
v2 mount, if present):

```http
HTTP/1.1 200 OK
Deprecation: true
Sunset: Sun, 09 Aug 2026 12:34:56 GMT
```

`Sunset` is RFC 8594 IMF-fixdate format. The per-endpoint header
overrides the blanket v1 sunset (RFC 8594 forbids stacking conflicting
values). Clients that respect the header — including our own frontend
and the standard Anthropic SDK — will show a console warning until the
date, then 410 the call after.

To pick a non-default sunset date (e.g. accelerated retirement):

```python
from datetime import datetime, timezone
register_endpoint_deprecation(
    path="/agents/legacy_status",
    sunset_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
)
```

To clear the registry in tests, call `clear_endpoint_deprecations()`.

## 5. Rollback / recovery

### 5.1 `api-compat` CI gate is failing a green change

Symptom: the gate is reporting breaking changes that aren't really
breaking, blocking unrelated PRs.

Recovery, in order of escalation:

1. **Inspect the report.** Download the `api-compat` artifact and read
   `api-compat-report.json`. Often the "break" is a Pydantic field-order
   change that landed in `openapi.json` because someone forgot to run
   `pnpm run openapi:sync` locally.
2. **Re-generate the snapshot.** Run `python scripts/dump_openapi.py`
   on the head commit. If the report is now empty, the PR author needs
   to commit the refreshed `openapi.json`.
3. **Override with a trailer** if the diff is intentional (see §3).
4. **Disable as a last resort.** Comment out the workflow trigger
   under `.github/workflows/api-compat.yml` and open a META ticket to
   restore it. Do not delete the script — the override mechanism is
   audit-critical.

### 5.2 `OpenAPIDiffParseFailed`

Symptom: the gate exits 2 and the report has `parse_error` populated.

Cause: usually the base ref's `openapi.json` is malformed (someone
hand-edited it) or the head spec wasn't regenerated and somehow drifted
to invalid JSON.

Recovery:

1. Validate locally: `python -m json.tool openapi.json`.
2. Regenerate: `python scripts/dump_openapi.py`.
3. If the base side is the problem, cherry-pick a corrective commit
   onto the base branch first. The gate does not greenlight on parse
   error — a human reviewer must compare the OpenAPI surface by hand
   before the PR ships.

### 5.3 A bad breaking change reached production

Symptom: clients are failing in prod after a `api:approved-breaking`
override.

Recovery:

1. Revert the breaking commit on `main` (`git revert <sha>`).
2. Tag the affected client builds in a comment on the META ticket so
   the fleet rollout system flags them.
3. If full revert isn't possible, re-mount the legacy route under
   `/api/v1` with the old shape and add a per-endpoint deprecation
   header for a real 90-day window (§4). Document the incident in
   `docs/sop/lessons-learned.md`.

### 5.4 Per-endpoint deprecation header was added prematurely

Symptom: a route is emitting `Sunset` but shouldn't be.

Recovery: remove the `register_endpoint_deprecation(...)` call and
redeploy. The registry is in-memory; no migration needed.

## 6. Known gaps

- **Constraint tightening** (e.g. shrinking `maxLength`, narrowing an
  `enum`) is not yet a tracked breaking-change kind. Tracked in
  follow-up work; for now, treat it as breaking by convention and add
  the `api:approved-breaking` trailer manually.
- **Type changes** (e.g. `integer` → `string`) are not yet detected.
  Same convention as above.
- **Format-only diffs** (key reordering inside JSON Schema) are filtered
  by `_collect_properties()` walking the `properties` map, so they are
  not false-positive flagged.

## 7. Error catalog (per OP-885 §Error catalog)

| Exception                 | Trigger                                            | Operator action                                |
|---------------------------|----------------------------------------------------|------------------------------------------------|
| `APIBreakingChangeRefused`| Breaking diff present, no override trailer found.  | Revert OR add `api:approved-breaking` trailer. |
| `OpenAPIDiffParseFailed`  | Either side of the diff is not parseable.         | Validate JSON, regenerate, manual review.      |

## 8. References

- OP-885 ticket — this work item.
- OP-774 / `backend/api_versioning.py` — initial v1/v2 split.
- OP-865 / `scripts/check_migration_compat.py` — sibling DB-side gate
  this script's shape is modelled on.
- RFC 8594 — `Sunset` HTTP header.
- RFC 9745 — `Deprecation` HTTP header.
