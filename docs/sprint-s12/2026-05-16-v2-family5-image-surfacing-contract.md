---
id: SPRINT-S12G-V2-FAMILY5-IMAGE-SURFACING-CONTRACT
version: v1 (2026-05-16)
title: G.A-v2 Family ⑤ — Image Surfacing Contract (what backend exposes about its own version)
scope: Contract spec for what the running backend MUST surface about its own image version so the downstream `deployment-audit.sh` + auto-redeploy + alerting can reason about "the running image vs. what shipped vs. what the DB expects". Doc-only ticket (v2-⑤-1a); no runtime change. Anchors are stable and consumed by every downstream Family ⑤ child.
status: Draft — OP-1154 (this ticket); locked per operator decision 2026-05-14 (Family ⑤ scope) + Sprint S12.G v1.4 spec §"Family ⑤ — Shipped-but-not-deployed runtime detector"
related:
  - sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 "Family ⑤ — Shipped-but-not-deployed runtime detector" (parent spec)
  - 2026-05-16-v2-alertbridge-framework-contract.md (OP-1144 — the AlertRule contract `v2-⑤-AlertRule` consumes)
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (incident that surfaced the 7-day stale-image gap)
  - docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md (prior "shipped vs deployed" sprint; AUDIT-23 / OP-976 promised parts of this and partially delivered)
  - scripts/deployment-audit.sh (today's systemd-unit / timer / container audit; this contract EXTENDS — does not replace — that file's responsibility into image-version auditing)
  - backend/main.py:471-473 (today's only "version" surface: `FastAPI(version="0.1.0")` — a hard-coded literal that knows nothing about the image)
  - JIRA OP-1154 (v2-⑤-1a — this spec)
  - JIRA OP-1155+ (v2-⑤-1bc, v2-⑤-Dockerfile-Manifest, v2-⑤-2bc, v2-⑤-2bc-Timer, v2-⑤-AlertRule, v2-⑤-AutoRedeploy, v2-⑤-Integration — downstream impl tickets that consume this spec)
---

# G.A-v2 Family ⑤ · Image Surfacing Contract — v1 (2026-05-16)

## §0. Reading order

1. §1 — the gap class this spec eliminates (5 minutes); the 2026-05-14 outage in one paragraph; what "image surfacing" means.
2. §2 — the **audit contract**: the four truth-sources for "currently running image version" and how they relate; which one wins when they disagree.
3. §3 — the **`/version` endpoint contract**: required response fields, examples, failure modes; this is what `v2-⑤-1bc` implements.
4. §4 — the **`MANIFEST.json` schema** and the build-time bake rule; this is what `v2-⑤-Dockerfile-Manifest` implements; explains why baking-at-build beats generating-at-runtime.
5. §5 — the **drift detection state machine**: when `deployment-audit.sh` flags drift, what thresholds, what severity tiers.
6. §6 — the **evidence file contract**: schema for `docs/audit/AUDIT-deployment/YYYY-MM-DD.json`; what must be present for forensic replay.
7. §7 — **alert routing**: the `OmniSightStaleImage` rule, including the v2-AlertBridge-1a contract labels and the v0 stdout+email delivery path.
8. §8 — **cross-family integration**: handoff to Family ⑥ (alembic head drift); how the audit feeds Family ⑥ Postgres drift detection.
9. §9 — the **AutoRedeploy contract (v2-⑤-AutoRedeploy)**: cron-vs-Watchtower decision; default cron-based Option (a); the backward-fail-fast invariant cross-link to v2-⑥-2bc.
10. §10 — claude-class override note for the spec ticket; filing-order + sequencing of the 8 downstream children.

This spec is the **contract** — not the implementation. Code lands in `v2-⑤-1bc` (the `/version` endpoint + tests), `v2-⑤-Dockerfile-Manifest` (the build-time bake step), `v2-⑤-2bc` (the audit script), `v2-⑤-2bc-Timer` (systemd timer + evidence file + Discord post), `v2-⑤-AlertRule` (the Prometheus rule consuming the v2-AlertBridge contract), and `v2-⑤-AutoRedeploy` (the closing-the-loop cron / sidecar pull). Anchors in §2 through §9 are stable and will be referenced from each downstream ticket's AC.

---

## §1. The gap class this spec eliminates

### §1.1 The class, in one sentence

> *"A backend that knows its own runtime API surface but does not know which image is providing that surface, and a deployment surface that knows which images were pushed but cannot tell which of them is actually running, with no third party able to reconcile the two — so a 7-day-stale image can be the live answer and nothing fires."*

Concrete instance from 2026-05-14: a routine operator check during the morning of the host-reboot incident surfaced that the prod backend was serving requests from an image tagged `:latest` whose GHCR digest had last changed **7 days earlier**. The repo had merged 11 commits in those 7 days. None of those merges were detectable from inside the running backend (no `/version` endpoint), from the host (`docker compose ps` showed the container as `Up 7d` but with no link from `Up` to the digest), or from any periodic audit (the audit script existed for systemd-units but not for image-vs-GHCR comparison).

Forensic walk:

| Layer | What it knew | What it didn't know |
|---|---|---|
| Running container (FastAPI) | `version="0.1.0"` (hard-coded literal, `backend/main.py:473`) | Image SHA, build time, git ref, alembic head, manifest path. |
| Docker daemon | Container ID, image ID, image RepoDigest at pull time | Whether `:latest` on GHCR has since moved (no periodic re-pull) |
| GHCR | The current digest behind `omnisight-backend:latest` | What is actually running anywhere in production |
| DB (`alembic_version`) | The schema head currently applied | What image was supposed to apply it; whether the running image even has the migration files for that head |
| Filesystem (`backend/alembic/versions/`) | Migration files in the image's filesystem | Whether they match what's running (they do by construction inside the container, but not from the host's perspective) |

The bug class is **no party can answer "what is running now, and was that intended"**. Each party has half the picture; nobody owns the join.

### §1.2 Why this is dangerous beyond a 7-day stale image

A 7-day-stale image is the benign end of the cost asymmetry. Worse instances of the same class:

- **Forward image, rolled-back DB.** A deploy script bug pulls a newer backend image but rolls the DB to an older alembic head. Today the backend starts and silently fails on the first query that hits a missing column. This is **Family ⑥ Option C "image AHEAD"** territory; without a `/version` endpoint surface, Family ⑥'s decision logic has nothing to anchor against.
- **Mixed-replica drift.** If we ever run >1 backend replica behind a load balancer, two replicas could be on different image SHAs after a partial deploy. From the outside, requests would intermittently see new-API and old-API responses with no way to distinguish.
- **Compromised image substitution.** A supply-chain incident could ship a tampered image with the same `:latest` tag. Without a baked `MANIFEST.json` that carries the *build-time* git ref, the running container has no way to prove its own provenance to itself.
- **Forensic blindness during incident.** When investigating a 02:00 outage, the on-call needs to know *which exact build* the bad behavior shipped in. Today the only answer is "whatever `:latest` was at the time, which is whatever happened to be pinned in the operator's terminal history" — useless for replay.

The cost asymmetry is: **today's 7-day stale image is the benignest possible instance of this class; the next instance will be worse.**

### §1.3 Why "deploy more often" doesn't dissolve it

Three reasons:

1. **Cadence is not a contract.** Deploying daily reduces the expected age of the running image but does not produce a number anyone can audit against. "It's probably recent" is not a defense; "the audit script ran at 02:00 and confirmed the running image is at digest X, which is the same as `:latest` at GHCR, both authored at 2026-05-15T18:42:00Z" is.
2. **Pull policy != deploy.** `docker compose pull` followed by `up -d` is a separate operator action; in the 2026-05-14 case the daily redeploy cron either did not exist or was silently failing. Without a separate observability layer, the deploy cron's silence is indistinguishable from the deploy cron's success.
3. **The DB axis.** Image freshness is not the same as schema freshness. The Family ⑥ Option C invariant ("image AHEAD: auto-upgrade DB; image BEHIND: fail-fast exit 78") *requires* the backend to know its own image's `alembic_head_in_image` — which today it does not. Solving image freshness only at the deploy-cadence level leaves the Family ⑥ invariant un-implementable.

The structural fix is to make the running image **self-describing**: the backend exposes `/version`, the image ships with `/app/MANIFEST.json`, and an audit script can compare both against external sources of truth. Once that surface exists, every downstream defense (Family ⑤'s own alert, Family ⑥'s asymmetric upgrade, Family ⑩'s runner pickup decisions) can read it.

---

## §2. The audit contract — four truth-sources and how they relate

This is the section every downstream Family ⑤ ticket cites. It enumerates the four places "what version is running" can be read from, what each one is authoritative for, and the join rule when they disagree.

### §2.1 The four truth-sources

| # | Source | What it answers | Authoritative for | Latency to read |
|---|---|---|---|---|
| **T1** | Running container `/version` endpoint (HTTP GET against the backend) | The image SHA + build time + git ref + alembic-head-baked-into-image that the live process believes it is | The *runtime* identity of the process | < 50 ms (one HTTP call) |
| **T2** | GHCR `:latest` digest (manifest API or `docker manifest inspect`) | What the **intended** running image is (the latest tag pushed by CI) | What *should* be running, per the deployment contract | 1–3 s (registry round-trip; network-bound) |
| **T3** | DB `alembic_version` table (one row, one column) | The schema head **currently applied** to the live database | The runtime identity of the *schema* | < 100 ms (one SQL query) |
| **T4** | Filesystem `backend/alembic/versions/` inside the running container (or equivalently, `alembic heads` in the image) | The schema head the **image's migration files** would advance to | The runtime identity of the *image's expected schema* | < 50 ms (filesystem read) |

T1 and T4 should agree by construction (the image bakes the same files the FastAPI process reads). T1 and T2 should agree after a successful redeploy. T3 and T4 should agree after a successful migration. The audit script's job is to verify all three agreement invariants periodically.

### §2.2 The join rule (when sources disagree)

Disagreements are not all equally bad. The audit script classifies each pairwise mismatch into a severity tier:

| Pair | Mismatch meaning | Severity | Action |
|---|---|---|---|
| T1 ≠ T2 (running image SHA ≠ GHCR `:latest` digest) | The running image is stale OR a redeploy is in flight | `warn` if age < 24 h, `page` if age ≥ 24 h | Trigger `OmniSightStaleImage` alert per §7; AutoRedeploy (§9) may attempt redeploy |
| T1 ≠ T4 (`/version`'s `image_sha` ≠ filesystem-derived SHA) | Impossible by construction; if seen, indicates a tampered binary or a corrupted MANIFEST | `page` always | Page on-call; treat as supply-chain incident; manual forensics |
| T1.`alembic_head_in_image` > T3 (image ahead of DB) | Forward drift — Family ⑥ Option C "auto-upgrade" path | `info` if upgrade succeeded < 5 min ago, `warn` if persists > 5 min | Family ⑥ §3.0.6 subcontract 3 runs `alembic upgrade head`; audit logs the transition |
| T1.`alembic_head_in_image` < T3 (image behind DB) | Backward drift — Family ⑥ Option C "fail-fast exit 78" path | `page` always (the running container will exit; this is detection of the cause) | Family ⑥ §3.0.6 subcontract 4 prevents this from ever shipping; if observed, indicates a deploy regression |
| T3 ≠ T4 (DB head ≠ image's expected head) | The DB has migrations the image doesn't know about, OR vice versa | Same as `T1.alembic_head_in_image` vs T3 (T4 ≈ T1 by construction) | Same routing as above |

The full state machine is in §5.

### §2.3 Which source wins for the "what is the running image?" question

For the **forensic** question ("what's running right now, on this host, in this process?"), **T1 is authoritative**. T1's `image_sha` is the source of truth because it's the one read from inside the live process; it cannot lie about its own identity (modulo §2.2 row 2's tampering scenario, which is detected, not ignored).

For the **deployment** question ("is the right image deployed?"), **T2 is the reference**. The deploy contract is "`:latest` on GHCR is what should be running"; T1's job is to match it.

For the **schema** question ("is the DB at the right head for this image?"), **T1.`alembic_head_in_image` vs T3** is the comparison; T1 wins on what the image expects, T3 wins on what the DB is at.

### §2.4 What this contract does NOT cover

- Multi-image stacks (frontend, omnisight-proxy, runner). This spec is scoped to **backend image surfacing only**. Other images may adopt the same `MANIFEST.json` shape (§4) and the same `/version` shape (§3) but their audit is out of scope for v2-⑤; each consuming image gets its own family or is added to deployment-audit.sh independently.
- Cluster-aware audit. If we ever run >1 replica, the audit script needs to enumerate replicas; the v0 contract assumes single-replica and the AlertRule `instance` label distinguishes hosts (per `v2-AlertBridge-1a` §1).
- Image-signing / supply-chain verification. The `MANIFEST.json` `git_ref` field is a *report*, not a *proof*. Cryptographic provenance is out of scope; the AutoRedeploy contract (§9) trusts GHCR's TLS chain and nothing else.

---

## §3. The `/version` endpoint contract

This is what `v2-⑤-1bc` implements. The contract is on the response shape; the underlying read is from the baked `MANIFEST.json` (§4) so this section depends on §4 being implemented but does not duplicate its schema.

### §3.1 Route, method, auth

- **Route:** `GET /version`
- **Method:** `GET` only; `HEAD` returns 200 with no body (standard FastAPI behavior); all other methods return 405.
- **Auth:** **PUBLIC**, no session, no token. This MUST be added to `PUBLIC_PATH_ALLOWLIST` per the Family ⑦ contract (`2026-05-16-v2-family7-allowlist-contract.md` §4); the v2-⑤-1bc implementer is responsible for the allowlist entry as part of the same change.
- **CORS:** Same policy as `/health` (allow any origin to GET). Rationale: external auditors (e.g., a future Watchtower-style sidecar) need to read this from outside the trust boundary.
- **Rate limit:** Exempt from `_rate_limit_gate` per the same Family ⑦ allowlist. A monitor calling `/version` every 60 s must not be throttled.

### §3.2 Response shape (success — HTTP 200)

```json
{
  "image_sha": "sha256:9b8e1c0d4f6e2a3c5b7d8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef01",
  "build_time": "2026-05-15T18:42:07Z",
  "git_ref": "b782b8b8c4e7f1d2a3b4c5d6e7f8a9b0c1d2e3f4",
  "alembic_head_in_image": "0204_add_runner_claims_table",
  "manifest_path": "/app/MANIFEST.json"
}
```

Field-by-field contract:

| Field | Type | Required | Semantics | Source |
|---|---|---|---|---|
| `image_sha` | string, `sha256:` prefixed, 64 hex chars after prefix | yes | The OCI image digest of the running container (the same string `docker inspect` shows as `Image.RepoDigest`) | Baked from `docker buildx imagetools inspect` at build time into `MANIFEST.json.image_sha` |
| `build_time` | string, RFC 3339 UTC (`Z` suffix), second precision | yes | When the image was built (CI build start, captured at bake time) | `MANIFEST.json.build_time` |
| `git_ref` | string, 40-hex-char git commit SHA | yes | The commit the image was built from; `git rev-parse HEAD` at build time | `MANIFEST.json.git_ref` |
| `alembic_head_in_image` | string, alembic revision identifier | yes | The single alembic head present in the image's `backend/alembic/versions/` at build time | `MANIFEST.json.alembic_head_in_image` |
| `manifest_path` | string, absolute path | yes | Where on the image filesystem the MANIFEST lives; constant `/app/MANIFEST.json` in v1; reserved as a field so a future image layout change doesn't break clients | Literal |

No additional fields. No timestamps for "when was this endpoint hit" (clients capture that). No backend-self-test fields (use `/readyz` for that). Keep this surface minimal so the schema is a stable wire contract.

### §3.3 Response shape (failure — HTTP 503)

If `/app/MANIFEST.json` is missing or unreadable at startup, the backend serves `/version` with HTTP 503 and the body:

```json
{
  "error": "manifest_unavailable",
  "remediation": "Image was built without MANIFEST.json. Rebuild via scripts/bake-image-manifest.sh as part of Dockerfile.backend; see docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md §4."
}
```

Rationale: a missing manifest is a build-time failure, not a runtime one. The 503 surfaces the build defect to the audit script (which then flags an `OmniSightStaleImage` -class alert with the `manifest_unavailable` annotation), instead of silently returning empty fields that would corrupt downstream comparisons.

### §3.4 Latency budget

p99 < 50 ms. The endpoint reads `MANIFEST.json` once at process startup, caches the parsed dict, and serves from memory. No filesystem read per request. The cache is invalidated only on process restart (which is the only event that can change the image identity).

### §3.5 Test contract (informs `v2-⑤-1bc` AC)

The implementing ticket MUST add tests covering:

1. **Happy path.** A manifest with all five fields present; GET returns 200 with the exact body shape per §3.2; field types validated.
2. **Missing manifest.** Manifest file deleted (or stubbed missing in test fixture); GET returns 503 with the body shape per §3.3.
3. **Malformed manifest.** Manifest file present but invalid JSON; GET returns 503 with `error: "manifest_unavailable"`.
4. **Schema mismatch.** Manifest missing a required field (e.g., `git_ref`); GET returns 503 with `error: "manifest_invalid"` and a `remediation` pointing at §4.
5. **Auth bypass.** Verify the endpoint is reachable with `OMNISIGHT_AUTH_BASELINE_MODE=enforce` and no Authorization header — must return 200. This is the regression test that prevents a future Family ⑦-class drift from accidentally requiring auth on `/version`.
6. **Latency.** p99 < 50 ms over 1000 requests; cache-after-startup verified by reading manifest once and asserting no further filesystem opens during the loop.

---

## §4. The `MANIFEST.json` schema (baked at build time)

This is what `v2-⑤-Dockerfile-Manifest` implements via a new `scripts/bake-image-manifest.sh` invoked from `Dockerfile.backend`. The contract is on the file shape and on the bake-time invariants; the runtime endpoint (§3) reads it but does not write it.

### §4.1 File location

`/app/MANIFEST.json` inside the image. Constant; the `/version` endpoint (§3.2 `manifest_path` field) reports the same value. The path is reserved as a field in the wire format so a future image layout reorganization is a contract change, not a silent break.

### §4.2 Schema

```json
{
  "image_sha": "sha256:<64 hex>",
  "build_time": "<RFC 3339 UTC>",
  "git_ref": "<40-hex git SHA>",
  "alembic_head_in_image": "<single alembic revision id>",
  "schema_version": 1
}
```

Field-by-field:

| Field | Source at bake time | Bake-time invariant | Failure mode |
|---|---|---|---|
| `image_sha` | Computed by `docker buildx imagetools inspect --raw $TARGET_IMAGE_REF` after build completes, then `sha256:` of the manifest descriptor | MUST match the digest GHCR will publish; verify by re-inspecting after push | If bake script cannot reach the local buildx daemon, build fails with exit 90 + message pointing here |
| `build_time` | `date -u +%Y-%m-%dT%H:%M:%SZ` at the start of the bake step | Monotonic; later builds MUST have later timestamps | If clock-skewed, build emits a warning but does not fail (CI host time is trusted) |
| `git_ref` | `git -C $REPO_ROOT rev-parse HEAD` at bake time | MUST be a full 40-char SHA (not a short hash, not a tag, not `HEAD`) | If `git rev-parse` returns abbreviated form, bake fails with exit 91 |
| `alembic_head_in_image` | `alembic -c backend/alembic.ini heads` parsed for the single revision id | MUST return exactly one head per Family ⑥ §3.0.6 subcontract 2 build-time invariant | If `alembic heads` returns 0 or >1 heads, bake fails with exit 92 and stderr lines pointing to the `alembic merge` remediation |
| `schema_version` | Literal `1` in v1 | Bumped only on schema break; v1 → v2 requires updating §3.2 and §4.2 simultaneously and adding a migration note in `docs/sprint-s12/lessons-learned.md` | If consumer reads schema_version > known, treat as warning + best-effort parse |

### §4.3 Why baked, not generated at runtime?

Three reasons; each is the rejection of a tempting alternative:

1. **Self-knowledge defeats tampering of the trivial kind.** If `/version` ran `git rev-parse HEAD` at request time, an attacker (or a misconfigured deploy) could substitute a binary that lies about its provenance just by replacing `/app/.git/HEAD`. A baked manifest is captured at build time outside the image's mutable filesystem; it is immutable once shipped (modulo container filesystem tampering, which is a different threat model handled by image signing — out of scope per §2.4).
2. **Build-time is the only moment the truth is recoverable.** The CI runner has access to the full git working tree, the alembic CLI in the build context, the buildx manifest inspector — none of which the runtime container has in production. Generating the manifest at runtime would require shipping the entire build toolchain into the production image, which violates §1.1 of the standard `Dockerfile.backend` minimalism contract.
3. **It makes the multi-head alembic invariant enforceable.** Family ⑥ §3.0.6 subcontract 2 requires that `alembic heads` returns exactly one head; this can only be enforced at build time (a runtime check could only complain after the image is already shipped). Bake-time failure (exit 92, §4.2 row 4) is the enforcement point.

Rejected alternatives:

- **"Read from environment variables at startup."** Tempting because Docker `ENV` directives are simpler than a JSON file. Rejected because env vars are a *flat* namespace shared with every other config knob; `MANIFEST.json` is structurally namespaced and extensible without env-var pollution.
- **"Use OCI image labels."** `docker inspect` already shows `Labels` set via `LABEL` in the Dockerfile; that data could carry `git_ref` etc. Rejected because labels are read from outside the container (operator-side) and the `/version` endpoint is read from *inside* the container (process-side); duplicating into JSON keeps the in-process path simple and avoids requiring the container to shell out to the docker daemon to introspect itself (which it cannot do without socket access — itself a security concern).

### §4.4 Bake script (`scripts/bake-image-manifest.sh`) outline

The implementing ticket (`v2-⑤-Dockerfile-Manifest`) writes `scripts/bake-image-manifest.sh`. Outline only — implementation details are the impl ticket's concern:

```bash
#!/usr/bin/env bash
# scripts/bake-image-manifest.sh — [OP-1155-ish] v2-⑤-Dockerfile-Manifest
# Emits /app/MANIFEST.json per the contract in
# docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md §4.
set -euo pipefail
REPO="${REPO:-$(pwd)}"
GIT_REF="$(git -C "$REPO" rev-parse HEAD)"
[ ${#GIT_REF} -eq 40 ] || { echo "bake-image-manifest: git_ref not 40 chars: $GIT_REF" >&2; exit 91; }
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
HEADS="$(alembic -c "$REPO/backend/alembic.ini" heads 2>/dev/null | awk '{print $1}')"
HEAD_COUNT="$(printf '%s\n' "$HEADS" | grep -cE '^[a-z0-9_]+')"
[ "$HEAD_COUNT" -eq 1 ] || {
  echo "bake-image-manifest: alembic heads count != 1 (got $HEAD_COUNT)" >&2
  echo "remediation: run 'alembic merge -m \"<reason>\" $HEADS' to converge branches" >&2
  exit 92
}
IMAGE_SHA="${TARGET_IMAGE_SHA:-sha256:0000000000000000000000000000000000000000000000000000000000000000}"
# Note: TARGET_IMAGE_SHA is set by the outer build script after `docker buildx
# imagetools inspect`; placeholder zero-sha allows local builds but is rejected
# by the bake-time-published-digest verifier in CI.
cat > /app/MANIFEST.json <<EOF
{
  "image_sha": "$IMAGE_SHA",
  "build_time": "$BUILD_TIME",
  "git_ref": "$GIT_REF",
  "alembic_head_in_image": "$HEADS",
  "schema_version": 1
}
EOF
```

The CI build wrapper performs the post-push step that fills in `TARGET_IMAGE_SHA` and re-bakes if needed (the second-bake-then-push pattern is standard for OCI digest self-reference; details belong in the impl ticket's commit message, not here).

### §4.5 Why a separate file, not embedding in the Python package?

`MANIFEST.json` is a *file*, not a Python module, deliberately:

- **External readers.** The deployment-audit script reads it via `docker exec <container> cat /app/MANIFEST.json` without needing a Python interpreter to be present. A future shell-based monitor (operator-runnable from `bash` only) can read it the same way.
- **Decoupled from import order.** A Python module with these constants would be loaded at import time; if a bug in the constants caused an import error, the FastAPI app would fail to start and `/version` could not even report the error. A file is read on demand.
- **Survives a Python package downgrade.** If a hot-patch needs to roll back the backend python wheel without rebuilding the image, the manifest still reflects the *image* (not the wheel), which is what auditors care about.

---

## §5. Drift detection state machine

This is what `v2-⑤-2bc` (`scripts/deployment-audit.sh` extension) and `v2-⑤-AlertRule` consume. The state machine describes how the audit script transitions between OK / warn / page states based on the four truth-sources from §2.

### §5.1 The five states

| State | Definition | Emitted as |
|---|---|---|
| `OK` | T1 == T2 (running image SHA matches GHCR `:latest`); T1.alembic_head_in_image == T3 (DB head matches image); GHCR `:latest` last-pushed timestamp < 24 h ago OR T1 == T2 regardless of age | No alert; evidence file written with `state: ok` |
| `WARN_STALE_IMAGE` | T1 != T2 AND age(T2 last push) < 24 h | `OmniSightStaleImage` warn-severity per §7 |
| `PAGE_STALE_IMAGE` | T1 != T2 AND age(T2 last push) ≥ 24 h | `OmniSightStaleImage` page-severity (auto-promoted from warn after threshold) |
| `PAGE_ALEMBIC_DRIFT` | T1.alembic_head_in_image != T3 (either direction); routed to Family ⑥ alerts | Family ⑥ AlertRules, not Family ⑤ |
| `PAGE_INTEGRITY` | T1.image_sha != T4-derived SHA (image self-inconsistency) | `OmniSightImageIntegrity` page-severity (rare; treated as supply-chain incident) |

### §5.2 Threshold rationale

- **24 h warn → page promotion.** A redeploy may legitimately lag the latest CI build by some hours (test runs, soak before promotion, operator confirmation). 24 h is the operationally-agreed window after which "the prod image is stale" is no longer "deploy in flight" but "deploy is broken or skipped". Same 24 h appears in the AlertBridge framework as the default cardinality rolling window (§6.1 of the AlertBridge contract); reuse minimizes config sprawl.
- **`PAGE_STALE_IMAGE` always pages.** No "wait a few more days" downgrade. Once 24 h is exceeded, the cost of staying stale exceeds the cost of waking an operator; the redeploy-in-flight grace has run out.
- **`PAGE_ALEMBIC_DRIFT` is always page.** No `WARN_*` variant. Schema misalignment is never recoverable by waiting; either the upgrade runs (Family ⑥ subcontract 3) or the container exits 78 (subcontract 4). Either way the operator wants to know.
- **`PAGE_INTEGRITY` is always page.** Never a warn. An image inconsistent with itself is a supply-chain incident class until proven otherwise.

### §5.3 State transitions

The state machine is **stateless across audit runs** (each run is a snapshot; no memory between runs). Transitions are computed by `deployment-audit.sh` per-run:

```
                ┌─────────────────────────────────┐
                │  start of audit run             │
                └────────────────┬────────────────┘
                                 ▼
                ┌─────────────────────────────────┐
                │  read T1 (curl /version)        │
                │  read T2 (GHCR manifest)        │
                │  read T3 (psql alembic_version) │
                │  read T4 (image alembic heads)  │
                └────────────────┬────────────────┘
                                 ▼
              ┌──────────────────┴──────────────────┐
              │                                     │
              ▼                                     ▼
   T1.image_sha == hash(T4)?              T1 == T2?
   no → PAGE_INTEGRITY                    yes → check alembic
   yes → continue                         no  → check age(T2)

                                                   ▼
                                          age(T2) < 24h → WARN_STALE_IMAGE
                                          age(T2) ≥ 24h → PAGE_STALE_IMAGE

   T1.alembic_head_in_image == T3?
   no → PAGE_ALEMBIC_DRIFT (route to Family ⑥)
   yes → OK (no alert)
```

A run that detects multiple conditions emits multiple alerts. The audit script does NOT collapse multi-finding runs into a single alert; AlertBridge §4 dedupe handles cross-run collapsing.

### §5.4 What the audit script does NOT do

- **It does not redeploy.** Even on `PAGE_STALE_IMAGE`, the script's job is to *detect* and *report*. AutoRedeploy (§9) is a separate process listening on the same evidence file (or a separate cron, decision in §9.1).
- **It does not fix.** Even on `PAGE_ALEMBIC_DRIFT`, the script does not run `alembic upgrade head`; that is Family ⑥'s startup hook (subcontract 3) running in the backend container, not the audit script running on the host.
- **It does not silence noisy alerts.** No `--quiet` flag, no rate-limiting at the script. AlertBridge §4 dedupe + §5 resolved-policy handle suppression.

---

## §6. Evidence-file contract — `docs/audit/AUDIT-deployment/YYYY-MM-DD.json`

This is what `v2-⑤-2bc-Timer` writes after each run. The evidence file is the forensic record of "what the audit saw at run time"; it must be sufficient to replay the decision a year later without re-querying any external source.

### §6.1 File location and naming

- **Directory:** `docs/audit/AUDIT-deployment/` (new; created by the timer's first run via `mkdir -p`).
- **Filename:** `YYYY-MM-DD.json` (UTC date of the audit run). One file per day; subsequent runs on the same day **overwrite** (the last run wins; the file is the daily snapshot, not an append log).
- **Permissions:** `0644` (operator-readable, auditor-readable, not auditor-writable).

If multi-run-per-day evidence becomes necessary (e.g., once we have hourly runs), the schema bumps `schema_version` and the naming becomes `YYYY-MM-DDTHH-MM-SS.json`; this is a forward-compatible change and the directory layout permits both forms.

### §6.2 Schema

```json
{
  "schema_version": 1,
  "audit_run_at": "2026-05-16T02:00:14Z",
  "audit_host": "omnisight-prod-01",
  "audit_script_version": "scripts/deployment-audit.sh@b782b8b8",
  "result_state": "OK",
  "truth_sources": {
    "T1_running_version": {
      "image_sha": "sha256:9b8e1c0d...",
      "build_time": "2026-05-15T18:42:07Z",
      "git_ref": "b782b8b8c4e7f1d2a3b4c5d6e7f8a9b0c1d2e3f4",
      "alembic_head_in_image": "0204_add_runner_claims_table",
      "manifest_path": "/app/MANIFEST.json",
      "fetched_at": "2026-05-16T02:00:14Z",
      "fetch_latency_ms": 18
    },
    "T2_ghcr_latest": {
      "digest": "sha256:9b8e1c0d...",
      "pushed_at": "2026-05-15T18:43:11Z",
      "registry": "ghcr.io/omnisight/omnisight-backend",
      "tag": "latest",
      "fetched_at": "2026-05-16T02:00:15Z",
      "fetch_latency_ms": 1240
    },
    "T3_db_alembic_version": {
      "head": "0204_add_runner_claims_table",
      "db_host": "omnisight-prod-pg-01",
      "fetched_at": "2026-05-16T02:00:15Z",
      "fetch_latency_ms": 42
    },
    "T4_image_alembic_heads": {
      "heads": ["0204_add_runner_claims_table"],
      "head_count": 1,
      "source": "docker exec backend alembic heads",
      "fetched_at": "2026-05-16T02:00:16Z",
      "fetch_latency_ms": 612
    }
  },
  "comparisons": {
    "T1_vs_T2_image_sha_match": true,
    "T1_vs_T2_age_seconds": 26823,
    "T1_alembic_vs_T3_match": true,
    "T1_image_sha_vs_T4_integrity": true
  },
  "alerts_emitted": [],
  "ghcr_query_evidence": {
    "method": "GET https://ghcr.io/v2/omnisight/omnisight-backend/manifests/latest",
    "status_code": 200,
    "response_digest_header": "sha256:9b8e1c0d..."
  }
}
```

### §6.3 Required fields for forensic replay

The forensic-replay requirement drives which fields are MUST vs MAY:

| Field | MUST / MAY | Why |
|---|---|---|
| `schema_version` | MUST | A consumer reading the file a year later needs to know the parser version. |
| `audit_run_at` | MUST | The wall-clock moment that anchors all `fetched_at` deltas. |
| `audit_host` | MUST | Multi-host fleets need to know which host produced this evidence. |
| `audit_script_version` | MUST | The `script@git_sha` lets a year-later reader run the exact same logic to verify replay. |
| `result_state` | MUST | The state machine's classification of this run; the headline finding. |
| `truth_sources.T{1..4}.*` | MUST | All four sources are required. Missing a source means the run didn't complete and the file should not have been written. |
| `truth_sources.T*.fetched_at` | MUST | Each source's read moment, separately, so a slow source can be diagnosed. |
| `truth_sources.T*.fetch_latency_ms` | MUST | The latency of each read, for performance-regression tracking. |
| `comparisons.*` | MUST | The pairwise outcomes — these are what the alert routing reads. Computed redundantly so a future audit script bug in comparisons can be replayed against the raw truth-sources. |
| `alerts_emitted` | MUST (array, possibly empty) | The list of `OmniSightStaleImage` / etc. alerts the run fired; correlates against AlertBridge audit log. |
| `ghcr_query_evidence.*` | MUST | The exact HTTP query made to GHCR + the status code + the digest header; forensics for a "did GHCR really say that?" dispute. |
| `truth_sources.T*.error` | MAY (only present if a source could not be read) | If e.g. T2 is unreachable, this field describes the error; `result_state` becomes `INCOMPLETE`. |

### §6.4 `INCOMPLETE` state

If any T-source cannot be read (e.g., GHCR is unreachable, the backend `/version` endpoint times out), the audit run emits a partial evidence file with `result_state: "INCOMPLETE"` and an `error` sub-field under the affected source. **An INCOMPLETE run does NOT emit `OmniSightStaleImage` alerts** (false-positive avoidance) but DOES emit a separate `OmniSightAuditIncomplete` warn-severity alert (route per §7.4). The forensic file is still written so post-mortem can see what the run got partway through.

### §6.5 Retention policy

- **Daily files** are retained indefinitely under `docs/audit/AUDIT-deployment/`. The directory is in-repo, version-controlled; one file per day is ≈1 KB, so 10 years is ≈ 3.6 MB. No prune.
- **The most recent file** is symlinked to `docs/audit/AUDIT-deployment/latest.json` after each run (atomic via `ln -sfn`). External monitors can poll the symlink.
- **A separate index file** `docs/audit/AUDIT-deployment/index.json` lists all dates with non-OK `result_state` (for quick incident scanning). Updated after each run.

---

## §7. Alert routing — `OmniSightStaleImage` per v2-AlertBridge-1a

This is what `v2-⑤-AlertRule` files: a single Prometheus rule that consumes the AlertBridge framework contract (`2026-05-16-v2-alertbridge-framework-contract.md`). All the AM-integration pitfalls (§0 of the AlertBridge doc) are pre-baked into the framework; this section only specifies the rule's own labels and remediation.

### §7.1 Rule shape

```yaml
groups:
  - name: omnisight-family-5
    rules:
      - alert: OmniSightStaleImage
        expr: |
          (time() - omnisight_image_ghcr_latest_pushed_timestamp_seconds) > 86400
          and on (instance)
          omnisight_image_running_matches_ghcr_latest == 0
        for: 5m
        labels:
          severity: warn          # promotes to page when age ≥ 24h via separate routing in §7.3
          area: deployment
          family: "5"
          defense_dimension: D1
        annotations:
          summary: "Running backend image is {{ $value }} seconds behind GHCR :latest"
          description: |
            The running backend on instance {{ $labels.instance }} reports
            image_sha != the current :latest digest on GHCR. The :latest
            digest last moved at {{ humanizeTimestamp .ghcr_pushed_at }}, which
            is {{ $value | humanizeDuration }} ago. Consult
            docs/audit/AUDIT-deployment/latest.json for the evidence file.
          runbook_url: https://docs.sora.services/runbooks/omnisight-stale-image
          remediation_hint: |
            Run scripts/auto-redeploy.sh on the prod host to pull and recreate
            the backend container; if the autoredeploy timer should have caught
            this, check journalctl --user -u omnisight-auto-redeploy.service.
        __bridge:
          critical_labels: [family, instance, image_repo]
          cardinality_caps:
            instance: 10
            image_repo: 5
```

### §7.2 Label conformance to v2-AlertBridge-1a

Per §1 of the AlertBridge contract: `severity`, `area`, `family`, `defense_dimension` are the routing labels.

- `severity: warn` is the **starting** severity; promotion to `page` is handled by the AlertBridge framework's severity-promotion rule (per §3 of AlertBridge contract — once `for: 24h` is exceeded the alert is re-emitted with `severity: page`). The rule does NOT re-declare a sibling page rule; the bridge handles promotion.
- `area: deployment` matches the runner's `area:deployment` JIRA-label convention (SOP §2 path-to-area mapping).
- `family: "5"` is ASCII per §1.1 of AlertBridge contract; the glyph `⑤` is for prose only.
- `defense_dimension: D1` per §2 of the runtime-defense spec — this alert reports on D1 (detection).
- `critical_labels: [family, instance, image_repo]` per §4.2 of AlertBridge contract; lets a multi-host fleet distinguish per-host staleness.

### §7.3 Severity promotion to page

The AlertBridge framework supports a single-rule warn→page promotion via the `for` clause cascading. v2-⑤-AlertRule keeps the rule's static `severity: warn` and configures the warning's age-threshold-to-page promotion in the framework config (not in the rule), so this rule does NOT carry a second `page` variant. Rationale: severity routing belongs in the framework (§7 of AlertBridge contract), and re-emitting a separate page-rule would double-count in the dedupe budget.

If for some reason the framework cannot express age-threshold promotion (verified during `v2-AlertBridge-1bc` implementation), the fallback is a sibling rule `OmniSightStaleImagePage` with `for: 24h` and `severity: page`, dedupe-collapsed against the warn rule by `(alertname-prefix, family, instance)`. This fallback is documented here so the implementer doesn't have to re-decide.

### §7.4 The `OmniSightAuditIncomplete` rule

A separate rule fires when `omnisight_deployment_audit_incomplete == 1` (gauge set by `deployment-audit.sh` per §6.4):

```yaml
- alert: OmniSightAuditIncomplete
  expr: omnisight_deployment_audit_incomplete == 1
  for: 1h          # multiple consecutive incompletes; one transient is OK
  labels:
    severity: warn
    area: ops
    family: "5"
    defense_dimension: D1
  annotations:
    summary: "Deployment audit could not complete on {{ $labels.instance }}"
    description: |
      The most recent deployment-audit run on {{ $labels.instance }} could not
      read all four truth-sources (T1/T2/T3/T4). See
      docs/audit/AUDIT-deployment/latest.json for which source failed.
    runbook_url: https://docs.sora.services/runbooks/omnisight-audit-incomplete
    remediation_hint: |
      Read latest.json's truth_sources.*.error field to find the failing source;
      typical causes are GHCR rate-limit (T2), backend startup-not-yet-complete
      (T1), or DB connection refused (T3).
  __bridge:
    critical_labels: [family, instance]
    cardinality_caps:
      instance: 10
```

This rule belongs in the same Family ⑤ rule group; it is not a Family ⑦/⑩ concern.

### §7.5 Delivery

Both rules use the AlertBridge v0 channel-adapter map (`SEVERITY_CHANNEL_MAP` per §7 of AlertBridge contract):

- `severity: warn` → email + stdout
- `severity: page` (after promotion) → email + stdout (v0); future: pagerduty + email

The Discord post (sent on `OmniSightStaleImage` fires) is a separate channel adapter wired by `v2-⑤-2bc-Timer`, not by the AlertRule itself. Discord is a notification channel for the *audit script*, not for the *Prometheus rule*; the two converge at the operator but enter via different paths. This separation lets `v2-⑤-2bc-Timer` ship before `v2-AlertBridge-1bc` if needed (deferred coupling).

---

## §8. Cross-family integration

Family ⑤'s audit is not standalone; it feeds Family ⑥ (alembic head drift) and consumes the AlertBridge framework. This section specifies the handoffs so the implementing tickets don't re-decide them.

### §8.1 Handoff to Family ⑥ (alembic head drift detection)

The audit script's evidence file (§6) writes `truth_sources.T1_running_version.alembic_head_in_image` AND `truth_sources.T3_db_alembic_version.head`. Family ⑥'s Postgres-drift detection (per `sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑥ subcontract 4) can correlate against these.

Concretely:

- Family ⑤ writes the daily evidence file.
- Family ⑥'s `alembic_drift` Prometheus gauge (per `v2-⑥-1bc`) is fed independently by the backend container's startup hook.
- The audit script's `T1.alembic_head_in_image vs T3.head` comparison is a **second, independent** check on the same invariant — by design. If the gauge says aligned but the audit says drifted (or vice versa), one of them has a bug; the audit script's evidence file is the forensic record that lets the diagnosis happen.

The Family ⑤ alert for the alembic mismatch (`PAGE_ALEMBIC_DRIFT` state per §5.1) is **routed to Family ⑥ AlertRules**, not emitted as a Family ⑤ rule. Concretely: the audit script's run produces a separate gauge `omnisight_alembic_drift_detected_by_audit` which Family ⑥'s `OmniSightAlembicDrift` rule reads (the rule's `expr` ORs the runtime-startup gauge with the audit-script gauge). This means:

- Family ⑤ does NOT add a sibling alembic rule; the existing Family ⑥ rule covers both detection sources.
- The audit script is a *redundant* detector for the same invariant, which is intentional — Postgres drift is severe enough that two detectors is the right cost.

### §8.2 Handoff to Family ⑩ (runner pickup)

The runner (`auto-runner-codex.py` / `auto-runner-jira.py` / `auto-runner-multi.py`) reads `/version` to log which image version it is running under, recorded in the per-pickup audit row (per `v2-⑩-1bc`'s `runner_claims.external_refs` field). This is a *consumer* relationship: the runner doesn't gate on `/version` (a stale runner still picks up tickets), but the post-hoc audit can answer "which runner image-version was active for this pickup". Implementing tickets in Family ⑩ MUST add `/version` read to the pickup logging path; v2-⑤-1bc's implementer should coordinate with the Family ⑩ implementer on the JSON shape.

### §8.3 Handoff to 31.G LibsGate (Prometheus + AM stack)

Once 31.G ships (the AM stack), the AlertBridge framework's `channel_adapter` swaps from `stdout_email` to `alertmanager_webhook` per §10 of the AlertBridge contract. The Family ⑤ rules require ZERO edits at that swap — verified by `v2-AlertBridge-AMMigrationTest`. The only Family ⑤ change at the swap is that the deployment-audit's gauge (`omnisight_image_ghcr_latest_pushed_timestamp_seconds`) MUST be scraped by the new Prometheus instance; that scrape config is a 31.G concern, not a Family ⑤ concern.

### §8.4 Handoff to 31.C Integration-external (Discord)

`v2-⑤-2bc-Timer` posts to Discord on drift detection. The Discord webhook URL + routing config is owned by 31.C-Integration-external (which is the cross-phase external dependency declared in the parent spec §6). The Family ⑤ timer ticket MUST consume the 31.C-provided client; it MUST NOT add a sibling Discord webhook config of its own. If 31.C is not yet shipped at the time `v2-⑤-2bc-Timer` is filed, the timer's Discord step is gated behind a feature flag (`OMNISIGHT_FAMILY5_DISCORD_ENABLED=0` by default); enable when 31.C ships.

### §8.5 Cross-family flow diagram

```
+--------------------+      writes      +-----------------------+
| deployment-audit.sh| ---------------> | docs/audit/AUDIT-     |
| (Family ⑤ §6)      |                  | deployment/*.json     |
+-----+--------------+                  +-----------+-----------+
      |                                             |
      | reads T1                                    | (forensic only)
      v                                             |
+-----+--------------+      reads MANIFEST          |
| backend /version   | <-----------------+          |
| endpoint (§3)      |   /app/MANIFEST   |          |
+-----+--------------+   .json (§4)      |          |
      |                                  |          |
      | scraped                          |          |
      v                                  |          |
+-----+--------------+ promotes to       |          |
| Prometheus +       | OmniSightStale-   |          |
| AlertBridge (§7)   | Image (§7.1)      |          |
+-----+--------------+                   |          |
      |                                  |          |
      v                                  |          |
+-----+--------------+                   |          |
| email + stdout v0  | (future AM via    |          |
| Discord via 31.C   |  §8.3 swap)       |          |
+--------------------+                   |          |
                                         |          |
+----------------------+  ORs gauge      |          |
| Family ⑥ OmniSight-  | <---------------+----------+
| AlembicDrift rule    |                            |
| (consumes our        |                            |
| audit gauge per §8.1)|                            |
+----------------------+                            |
                                                    v
                                          +---------------------+
                                          | Family ⑩ runner    |
                                          | logs /version read  |
                                          | per pickup (§8.2)   |
                                          +---------------------+
```

---

## §9. The AutoRedeploy contract (`v2-⑤-AutoRedeploy`)

This is the consumer-side ticket that closes the half of the deploy pipeline OP-1035 left open. Without it, detection + alert + evidence happen but the operator still has to ssh in and `docker compose pull && up -d` by hand. The contract specifies the redeploy mechanism, the cron-vs-sidecar decision, and the Family ⑥ backward-fail-fast cross-check.

### §9.1 Two implementation options

Per the parent spec §3 Family ⑤ row v2-⑤-AutoRedeploy, two options are on the table; operator decision is **default Option (a) cron**, with Option (b) sidecar tracked as a follow-up.

**Option (a) — cron-based, default.** A systemd timer `omnisight-auto-redeploy.timer` fires daily at a low-traffic hour (default 03:30 UTC) and runs `scripts/auto-redeploy.sh`:

```bash
#!/usr/bin/env bash
# scripts/auto-redeploy.sh — [OP-1156-ish] v2-⑤-AutoRedeploy Option (a)
set -euo pipefail
cd /opt/omnisight
docker compose pull --quiet
# Before recreate: check Family ⑥ backward-fail-fast invariant
# (the new image must not have alembic_head_in_image < db alembic head)
NEW_IMAGE_HEAD="$(docker run --rm --entrypoint cat ghcr.io/omnisight/omnisight-backend:latest /app/MANIFEST.json | jq -r .alembic_head_in_image)"
DB_HEAD="$(psql -tAc 'SELECT version_num FROM alembic_version')"
# Comparison is alembic-revision ordering, not lexicographic — needs alembic helper
docker run --rm --entrypoint alembic ghcr.io/omnisight/omnisight-backend:latest \
  -c /app/backend/alembic.ini history --rev-range="$DB_HEAD:$NEW_IMAGE_HEAD" 2>/dev/null \
  || { echo "auto-redeploy: image $NEW_IMAGE_HEAD is BEHIND db $DB_HEAD; refusing"; exit 78; }
docker compose up -d --quiet-pull
```

**Pros (a):** simple; idempotent (`up -d` is a no-op if nothing changed); operator-debuggable via `systemctl status omnisight-auto-redeploy.service`; no in-container long-running process.

**Cons (a):** up to 24 h staleness window between pushes and redeploys (acceptable per §5.2 threshold).

**Option (b) — Watchtower-style sidecar.** A long-running container watches GHCR digest changes and triggers a recreate within minutes of a new push.

**Pros (b):** lower staleness (minutes, not hours).

**Cons (b):** introduces a new sidecar process to monitor + secure; the `docker.sock` mount is a privileged surface; the recreate cadence interacts with rolling-deploy strategy (if any). Sidecar policy is best deferred until we have multi-replica needs.

**Decision: Option (a) ships first.** Option (b) is tracked as a post-v2-⑤-AutoRedeploy follow-up if 24 h staleness proves operationally insufficient.

### §9.2 Backward-fail-fast invariant (cross-link to v2-⑥-2bc)

The script's exit-78 guard above is the consumer-side application of Family ⑥ §3.0.6 subcontract 4 (backward fail-fast). The same invariant is enforced by the **backend's own startup hook** (subcontract 4); having the redeploy script ALSO check it is a defense-in-depth move:

- If the redeploy script's pre-check passes (image >= db head), the redeploy proceeds and the backend's startup hook is a no-op verification.
- If the redeploy script's pre-check fails (image < db head), the script exits 78 and the recreate is never attempted; the existing backend container keeps running and serving traffic from the older image.
- Without the script-side check, a backwards redeploy would partially succeed: `docker compose up -d` would replace the container, the new container would exit 78, and the service would be down with no automatic rollback. The script-side check makes the failure happen BEFORE the recreate, so the existing container stays up.

The implementing ticket (`v2-⑤-AutoRedeploy`) MUST include an integration test that injects a backward-image scenario (a synthetic image with `alembic_head_in_image` < the current DB head) and asserts:

1. The script exits 78 cleanly (no partial recreate).
2. The existing backend container is still running after the script exit.
3. An `OmniSightAutoRedeployBlocked` warn-level alert fires (rule defined in the AutoRedeploy ticket; consumes AlertBridge per §7).

### §9.3 Timer + service unit shape

The timer/service pair lives at `deploy/systemd/omnisight-auto-redeploy.{service,timer}`:

```ini
# omnisight-auto-redeploy.timer
[Unit]
Description=Daily backend auto-redeploy (Family ⑤)

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# omnisight-auto-redeploy.service
[Unit]
Description=Run scripts/auto-redeploy.sh once
After=network-online.target docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/omnisight
ExecStart=/opt/omnisight/scripts/auto-redeploy.sh
SuccessExitStatus=0 78  # 78 = backward-image blocked; not a script failure
TimeoutStopSec=120
```

`SuccessExitStatus=0 78` means a backward-fail-fast exit (78) is NOT a systemd-level failure — the script ran correctly and refused for correct reasons. Other non-zero exits (script bug, docker daemon unreachable) ARE systemd failures and trigger journald-level alerting per the Family ⑧ shutdown contract integration.

### §9.4 Operator override

If the operator needs to force a redeploy outside the cron window (e.g., to ship a hotfix), `systemctl --user start omnisight-auto-redeploy.service` triggers the same script on demand. There is NO separate "force" flag on the script — the same guards apply (the operator cannot bypass the backward-fail-fast invariant from this path; that would require the Family ⑥ rescue CLI per `v2-⑥-RescueCLI`).

### §9.5 Migration from today's state

Today (2026-05-16) there is no auto-redeploy. The migration is:

1. `v2-⑤-AutoRedeploy` lands `scripts/auto-redeploy.sh` + the systemd unit pair.
2. Operator manually copies the unit pair into `~/.config/systemd/user/` (or system-wide as appropriate), runs `systemctl --user daemon-reload`, then `systemctl --user enable --now omnisight-auto-redeploy.timer`.
3. First scheduled run is observed; evidence file at `docs/audit/AUDIT-deployment/` (written by the audit timer, NOT this one) reflects the new image.
4. After 7 days of clean runs, the `OmniSightStaleImage` alert should have fired at most once (during the first day of catch-up if the image at v2-⑤-AutoRedeploy ship time was already stale).

This migration is an operator action, not an automated one — declared in the ticket's `tag_type: operator-prepare-only` per the parent spec §3.0.5 boundary block.

---

## §10. Spec ticket notes

### §10.1 Runtime impact of this ticket: ZERO

No code in `backend/`, no migration, no env var, no CI yaml. The only files touched by OP-1154 are:

- `docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md` (this document, new)
- `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` (one cross-reference line added to the Family ⑤ section)

All Code AC items are documentation (schemas as JSON / YAML examples in this file); all Deploy AC items are markdown-lint + the x-ref line; all Integration AC items are the §-anchors that downstream tickets cite; the Exercised AC item is verified when the first downstream impl ticket (likely `v2-⑤-1bc`) lands without amending this spec.

### §10.2 Class override note

This ticket is filed under **claude** class (specs / ADRs / architecture memos route to claude per coordination.md). The 8 downstream children mix codex (impl: `1bc`, `Dockerfile-Manifest`, `2bc`) and claude (timer, alert rule, AutoRedeploy, integration). The class split mirrors the parent spec §3 Family ⑤ table.

### §10.3 Downstream filing order

Per parent spec §6 + dependency arrows in §3 Family ⑤ table:

```
v2-⑤-1a (THIS) ─┬─→ v2-⑤-1bc (backend /version endpoint)
                ├─→ v2-⑤-Dockerfile-Manifest (build-time bake)
                │       │
                │       └─→ v2-⑤-2bc (deployment-audit.sh extension)
                │                │
                │                └─→ v2-⑤-2bc-Timer (systemd timer + evidence + Discord)
                │
                └─→ v2-⑤-AlertRule (Prometheus rule)  ← also blockedBy v2-AlertBridge-1bc

v2-⑤-Integration (E2E)  ← blockedBy all of the above
v2-⑤-AutoRedeploy       ← blockedBy v2-⑤-Integration + v2-⑥-2bc
```

`v2-⑤-1bc` and `v2-⑤-Dockerfile-Manifest` may be filed in parallel (no inter-dependency) but `v2-⑤-2bc` waits on both (it reads `/version` AND requires the manifest to exist in the image).

### §10.4 Out of scope

Per parent spec + this spec §2.4: multi-image audit (frontend / proxy / runner), cluster-aware audit, image-signing / supply-chain verification, sidecar-based Watchtower (deferred). Each of these is a candidate for a future v2-⑤-* extension or its own family; none block the current 8-ticket plan.

### §10.5 Anchors stable for downstream AC citation

Downstream tickets MUST cite by §-anchor (not by line number) when AC items reference this doc. Stable anchors (matching the GitHub markdown convention `#section-id`):

- `#§2-the-audit-contract--four-truth-sources-and-how-they-relate`
- `#§3-the-version-endpoint-contract`
- `#§4-the-manifestjson-schema-baked-at-build-time`
- `#§5-drift-detection-state-machine`
- `#§6-evidence-file-contract--docsauditaudit-deploymentyyyy-mm-ddjson`
- `#§7-alert-routing--omnisightstaleimage-per-v2-alertbridge-1a`
- `#§8-cross-family-integration`
- `#§9-the-autoredeploy-contract-v2-autoredeploy`

If any section is renamed in a future revision, the anchor migration is a contract change and the v2-⑤-* tickets that cite it MUST be notified.

---

*End of contract spec. — OP-1154 / v2-⑤-1a / 2026-05-16*
