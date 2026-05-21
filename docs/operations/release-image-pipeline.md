# Release Image Pipeline (OP-1478)

> **Status:** activated as of META OP-1478 (scope
> `scope:op-1478-activation-2026-05-19`). Replaces the OP-763 GHCR-only
> flow, which is preserved as an appendix at the bottom of this
> document. The activation chain is: register self-hosted GitLab runner
> (OP-1478 T1.2) → first green `v*` pipeline → bundle manifest sealed →
> GitLab Container Registry populated → compose cutover to GitLab CR
> (already done per OP-1473) → GHCR decommission window (OP-1478 T2.3).
>
> Governance: [`ADR-0038`](../adr/ADR-0038-image-pipeline-on-gitlab.md)
> records the GHCR → GitLab CR decision; [`ADR-0002`](../adr/ADR-0002-gitlab-primary-github-mirror.md)
> establishes GitLab as the single source of truth ("CI 全面走 GitLab Only").

## 1. Architecture overview

The activated pipeline is a strict left-to-right pull chain. There is
no GHCR push from this repository's GitLab project anymore — the only
GHCR images consumers still see come from the legacy
`.github/workflows/build-images.yml` workflow during the OP-1478 T2.3
observation window (§10).

```
┌────────────┐  push to    ┌───────────────────┐  triggers   ┌─────────────────────┐
│  Gerrit    │ refs/heads/ │   GitLab (sora.   │  v* tag     │  GitLab CI runner   │
│  review    │────────────▶│   services:29420) │────────────▶│  (privileged dind   │
│  (29418)   │             │                   │             │   executor, T1.2)   │
└────────────┘             └───────────────────┘             └──────────┬──────────┘
                                                                        │ push by digest
                                                                        ▼
                          ┌──────────────────────────────────┐  pull   ┌──────────────┐
                          │  GitLab Container Registry       │────────▶│  prod host   │
                          │  sora.services:49154/omnisight/  │ digest- │  docker      │
                          │  OmniSight-Productizer/{...}     │ pinned  │  compose     │
                          └──────────────────────────────────┘         └──────────────┘
```

The runner that closes the diagram was missing on 2026-05-19 11:00 CST
(GitLab API `/runners` returned `[]`, all 15 jobs in pipeline #28
`skipped`), which is why every prior `v*` tag pipeline reported
`failed` and the registry stayed empty. Runner registration is owned
by OP-1478 T1.2; this document assumes it has landed.

Prod hosts pull from GitLab CR using the read-only deploy token
provisioned per OP-1470 (§6 below). They do **not** call back to
GitLab CI or to Gerrit at deploy time.

## 2. Triggers

The pipeline lives in [`.gitlab-ci.yml`](../../.gitlab-ci.yml) at the
repository root. Its top-level `workflow.rules` fires the pipeline
**only** for tags matching `^v.*`:

```yaml
workflow:
  rules:
    - if: '$CI_COMMIT_TAG =~ /^v.*/'
    - when: never
```

Concretely:

| Event | Pipeline fires? | Notes |
|---|---|---|
| `git push origin develop` | no | `when: never` swallows branch pushes |
| `git push origin feature/...` | no | same |
| `git push origin vX.Y.Z` (annotated tag) | **yes** | every job runs under `.tag_rules` |
| `git push origin vX.Y.Z-rcN` | **yes** | release candidates use the same rule |
| GitLab UI "Run pipeline" on a branch | no | no `CI_COMMIT_TAG` set |

Branch builds are out of scope on purpose. The retired GHCR workflow
published `sha-<short>` images on every `main` push; the activated
GitLab pipeline does not. If you need an out-of-band build, cut a
throwaway `vX.Y.Z-rcN-canary` tag (see §8 of
[`release-cut-runbook.md`](release-cut-runbook.md)).

## 3. Pipeline stages

`.gitlab-ci.yml` declares five sequential stages, each fanned out by
`.image_matrix` across `backend`, `frontend`, and `bridge`:

| Stage | Job | What it does |
|---|---|---|
| `build` | `build-image` | `docker buildx build --platform linux/amd64,linux/arm64 --push` for `Dockerfile.{backend,frontend,bridge}`. Pushes `:${CI_COMMIT_TAG}`, `:sha-${CI_COMMIT_SHORT_SHA}`, `:latest`. |
| `sign` | `sign-image` | Resolves the just-pushed multi-arch digest, then `cosign sign --key "$COSIGN_KEY" <image>@<digest>`. Verifies the signature with `deploy/cosign/cosign.pub` before exiting the job — a broken signing step is caught inside the same pipeline run, not at deploy time. |
| `sbom` | `sbom-image` | Installs `syft v1.17.0`, generates `sbom-<image>.cdx.json` (CycloneDX JSON), uploads as a 90-day artifact. |
| `attest` | `attest-image` | Builds an in-toto predicate (`{image, image_ref, digest, git_sha, git_ref, pipeline_url, builder: "gitlab-ci"}`) and runs `cosign attest --predicate ... --type https://in-toto.io/Statement/v1`. |
| `audit-emit` | `audit-emit` | Writes `image-audit-<image>.json` (shape compatible with the backend `audit.log()` payload, see [`backend/audit.py`](../../backend/audit.py)) as a 90-day artifact. The prod-side audit poller ingests these into the hash-chained audit log. |

### Cosign key-based signing

The `sign` and `attest` stages use the encrypted cosign private key
provided through `COSIGN_KEY`. The public verifier key is committed at
`deploy/cosign/cosign.pub`, and every promote/deploy gate must call the
same key-based verifier script.

| Variable | Value (defaults from `.gitlab-ci.yml`) |
|---|---|
| `COSIGN_KEY` | Path to the CI-mounted cosign private key |
| verifier public key | `deploy/cosign/cosign.pub` |

Operator verification uses the committed public key via
[`scripts/verify_image_signature.sh`](../../scripts/verify_image_signature.sh).
For the full stage-by-stage variable contract and runner requirements
(privileged dind, mounted signing key, network egress) see
[`gitlab-ci-image-build.md`](gitlab-ci-image-build.md).

## 4. Image naming

Each pipeline pushes three images, each with three tags. Substitute
the GitLab-CI variables in the table below — they are resolved
automatically at build time:

```
sora.services:49154/omnisight/OmniSight-Productizer/{backend,frontend,bridge}:${CI_COMMIT_TAG}
sora.services:49154/omnisight/OmniSight-Productizer/{backend,frontend,bridge}:sha-${CI_COMMIT_SHORT_SHA}
sora.services:49154/omnisight/OmniSight-Productizer/{backend,frontend,bridge}:latest
```

The base prefix is `${CI_REGISTRY_IMAGE}` (provided by GitLab when the
project container registry is enabled). The `:latest` tag is updated
on **every** `v*` pipeline; pre-release tags (`vX.Y.Z-rcN`) therefore
move `:latest` — operators must pin by digest at deploy time
(`@sha256:...`, see `docker-compose.prod.yml` line 163).

### Cross-registry naming during the T2.3 observation window

While both pipelines run in parallel (until OP-1478 T2.3 decommissions
ghcr.io), the same release exists under two names:

| Registry | Path | Tag set | Lifetime |
|---|---|---|---|
| GitLab CR (canonical) | `sora.services:49154/omnisight/OmniSight-Productizer/{backend,frontend,bridge}` | `${CI_COMMIT_TAG}`, `sha-${CI_COMMIT_SHORT_SHA}`, `latest` | indefinite (subject to §7) |
| GHCR (legacy) | `ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-{backend,frontend,bridge}` | `vX.Y.Z`, `sha-<short>`, `latest` | until T2.3 cutoff |

Digest parity between the two is verified by `release-cut-runbook.md`
§8 Action 2 on every cut; drift is a stop-the-line incident.

## 5. Bundle manifest

The release bundle is the contract surface a deployed stack reads to
prove what it is running. It is produced by
[`scripts/build_image_bundle.py`](../../scripts/build_image_bundle.py),
validated against
[`omnisight-bundle.schema.json`](../../omnisight-bundle.schema.json),
and baked into the backend image at `/app/bundle.json` (served by
`/api/version`).

```json
{
  "bundle_id":   "v0.5.0-3f1c0a4e",
  "git_ref":     "refs/tags/v0.5.0",
  "git_sha":     "3f1c0a4e...",
  "build_time":  "2026-05-19T11:00:00Z",
  "images": {
    "backend":  { "digest": "sha256:..." },
    "frontend": { "digest": "sha256:..." },
    "bridge":   { "digest": "sha256:..." }
  },
  "contracts": {
    "api_required": "v1",
    "api_supported": ["v1", "v2"],
    "openapi_hash": "<hex>",
    "db_migration_head": "<alembic-rev>",
    "frontend_built_against_api": "v1"
  },
  "signatures": [ ... ]
}
```

### Tie-in to V5 `frontend_compat_check`

`/readyz` surfaces a `checks.frontend_compat_check` key (see
[`fe-be-compat-monitoring.md`](fe-be-compat-monitoring.md) §3). The
check compares the backend's bundled `contracts.api_supported` against
the API version the running frontend was built against
(`contracts.frontend_built_against_api`). A frontend whose declared
API target is not in the backend's `api_supported` list flips the
check to FAIL, which a prod operator catches via the `/readyz` gate
before traffic reaches the new replicas. The bundle manifest is the
authoritative source for both sides of that comparison — there is no
runtime negotiation, only a baked-in claim.

## 6. Pull credentials

Prod hosts authenticate to `sora.services:49154` with a project-scoped
deploy token that has only `read_registry` scope. The full procedure
— enabling the registry, configuring the cleanup policy, provisioning
the token, deploying the secret to prod hosts, and rotating it — is
owned by [`gitlab-cr-pull-credentials.md`](gitlab-cr-pull-credentials.md)
(shipped under OP-1470).

In short:

```bash
TOKEN="$(cat ~/.config/omnisight/gitlab-cr-pull-token)"
USER="omnisight-cr-puller"
echo "$TOKEN" | docker login sora.services:49154 \
    --username "$USER" --password-stdin
```

The token is never written to source. The administrative GitLab
actions (project setting toggle, deploy-token creation, prod-host
secret deployment) are explicitly operator-driven per OP-1470 — this
document does not automate them.

## 7. Retention policy

The retention policy is owned by
[`image-retention-policy.md`](image-retention-policy.md) (OP-1480, the
"V2" rewrite that replaced the original tagged-forever / untagged-30d
rule with per-tag-class retention).

Headline rules (canonical version in the linked doc):

| Tag class | Retention |
|---|---|
| Immutable release `vX.Y.Z` | forever |
| Hotfix `vX.Y.Z-hotfix-N` | forever |
| Release candidate `vX.Y.Z-rcN` | 90 days post-ship, 30 days otherwise |
| Develop build `develop-<12sha>` | 14 days OR newest 30, whichever is more |
| Feature build `feature-<12sha>` | 7 days |
| Untagged orphan | 7 days |
| Mutable alias (`latest`, `staging`, `prod`, `canary`, `develop-latest`) | never deleted as a standalone target |
| Unknown tagged version | kept for manual review (conservative — GHCR/GitLab CR deletions are irreversible) |

### Manual run

Dry-run a single package (substitute `omnisight-frontend` /
`omnisight-bridge` as needed):

```bash
GH_TOKEN=...  python3 scripts/enforce_image_retention.py \
  --dry-run \
  --package omnisight-backend
```

Write the same decision table to a file (preferred when attaching to
a JIRA comment):

```bash
GH_TOKEN=...  python3 scripts/enforce_image_retention.py \
  --dry-run \
  --package omnisight-backend \
  --summary-file artifacts/retention/omnisight-backend.txt
```

Remove `--dry-run` to actually delete. The script honours the
`MIN_KEEP` / `MAX_AGE_DAYS` floor described in
`image-retention-policy.md`; a misconfiguration cannot prune below
the floor.

## 8. Monitoring

The GitLab CR side is observed by `gitlab-cr-monitor.timer` (a
systemd `.timer` + `.service` pair installed on the prod monitoring
host) — provisioning is owned by **OP-1478 T1.4**. The timer fires
on a 5-minute cadence and emits the following Prometheus series:

| Metric | Labels | Meaning |
|---|---|---|
| `gitlab_cr_reachable` | `registry` | 1 if `GET /v2/` returns 401, 0 otherwise |
| `gitlab_cr_pull_latency_seconds` | `image`, `registry` | wall-time of `docker pull <image>:latest` from a clean cache |
| `gitlab_cr_last_digest_age_seconds` | `image` | seconds since the `:latest` digest was last updated |
| `gitlab_cr_runner_count` | `project` | count of registered runners with `online: true` for the project |

The `gitlab_cr_runner_count` series is the canary for the failure
mode that caused the 2026-05-19 outage (zero registered runners,
every `v*` pipeline failing silently). A value of `0` paged via
`prometheus/rules/image-compat.yml`. Once T1.4 lands, that alert
will route to the on-call image-pipeline owner.

Until T1.4 deploys the timer, operators check runner health
manually:

```bash
curl -fsSL --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://sora.services:49154/api/v4/projects/omnisight%2FOmniSight-Productizer/runners" \
  | jq 'map({id, description, online, status})'
```

An empty array or `online: false` on every runner means the next
`v*` tag will fail.

## 9. Operator runbooks index

| Task | Runbook | Owner ticket |
|---|---|---|
| Stand up / re-register a self-hosted GitLab runner with privileged dind | `gitlab-runner-install.md` (in flight) | OP-1478 T1.2 |
| Cut a release tag (the only event that triggers this pipeline) | [`release-cut-runbook.md`](release-cut-runbook.md) §2, §8 | OP-880 / OP-1488 |
| Promote an already-built bundle between staging / canary / prod | [`image-promotion-runbook.md`](image-promotion-runbook.md) | OP-1481 |
| Pre-flight a prod deploy (digest pinning, cosign verify, cache check) | [`prod-deploy-runbook.md`](prod-deploy-runbook.md) | OP-881 |
| Flip the prod `.env` from ghcr.io to GitLab CR | [`gitlab-cr-cutover-checklist.md`](gitlab-cr-cutover-checklist.md) | OP-1473 |
| Provision / rotate the read-only GitLab CR pull token | [`gitlab-cr-pull-credentials.md`](gitlab-cr-pull-credentials.md) | OP-1470 |
| Enforce / dry-run image retention | [`image-retention-policy.md`](image-retention-policy.md) | OP-1480 |
| Detailed CI variable contract, runner egress, canary procedure | [`gitlab-ci-image-build.md`](gitlab-ci-image-build.md) | OP-1488 / OP-1474 |

`gitlab-runner-install.md` is not yet checked in — T1.2 owns its
delivery. Until it lands, runner registration follows the standard
GitLab Runner installation guide
(`https://docs.gitlab.com/runner/install/`) plus the project-specific
config required by [`gitlab-ci-image-build.md`](gitlab-ci-image-build.md)
§"Runner Requirements" (Docker-in-Docker `--privileged`, mounted cosign
signing key, egress to `github.com` for cosign/syft installers).

## 10. Decommissioning ghcr.io

GHCR is retained as a fallback for the duration of the OP-1488 G4
dual-publish observation window. The exit criteria are in
[`release-cut-runbook.md`](release-cut-runbook.md) §11:

> ≥ 3 successful release cuts via dual-publish over a ~2-week window
> with zero divergence incidents.

A digest-drift FAIL (§8 Action 2 of `release-cut-runbook.md`), a
staging-smoke FAIL that triggers §10 rollback, or a `P0`/`P1` INCIDENT
JIRA tagged `image-pipeline` resets the count to zero.

**OP-1478 T2.3** owns the actual GHCR removal commit: stripping the
`ghcr.io` default fallback from `${OMNISIGHT_REGISTRY:-ghcr.io/...}`
in `docker-compose.{prod,staging}.yml`, retiring the
`.github/workflows/build-images.yml` GHCR push workflow, and freezing
the GHCR packages read-only. **Target cutover date is set on T2.3 once
the §11 observation-window count clears**; this document will be
updated with the concrete date when T2.3 transitions to In Progress.
Until then, ghcr.io paths in compose files are load-bearing — do not
remove them piecemeal.

---

## Historical (OP-763, deprecated)

The text below describes the **retired** GHA-only image pipeline that
shipped with OP-763. It is preserved for archaeological context only —
do not follow it for new work. All operational reality has moved to
the GitLab path documented in §§1–10 above, per ADR-0038. The GHA
workflow file (`.github/workflows/build-images.yml`) remains in the
repo solely to keep the dual-publish observation window alive
(see §10 above) and will be removed by OP-1478 T2.3.

### Original goal

Every merge to `main` and every `v*` tag produced signed Docker
images for the three runtime planes — backend, frontend, hardware
bridge — pushed to GHCR, tagged immutably by git SHA, and signed with
cosign keyless. A retention sweep pruned old untagged builds while
keeping release tags forever.

### Original triggers

The workflow `.github/workflows/build-images.yml` fired on:

| Event | Builds | Tags applied |
|---|---|---|
| `push` to `main` | yes | `:sha-<short>` |
| `push` `v*` tag | yes | `:sha-<short>`, `:v<X.Y.Z>`, `:latest` |
| `workflow_dispatch` | yes | same as the underlying ref |
| `schedule` (weekly) | no | retention-only |

`:latest` was only updated by tag pushes — main-branch merges never
moved `:latest`. The activated GitLab pipeline moves `:latest` on
every `v*` (including release candidates), which is why the V4 lock
files (`prod.env.lock.json`, etc.) pin by digest rather than tag.

### Original image inventory

| Image | Dockerfile | Source |
|---|---|---|
| `ghcr.io/<owner>/omnisight-backend` | `Dockerfile.backend` | `backend/` |
| `ghcr.io/<owner>/omnisight-frontend` | `Dockerfile.frontend` | Next.js app in repo root |
| `ghcr.io/<owner>/omnisight-bridge` | `Dockerfile.bridge` | `tools/hardware_daemon/` |

All three were multi-arch (`linux/amd64` + `linux/arm64`). The
activated pipeline keeps multi-arch — see §4.

### Original signing model

Cosign keyless via GitHub OIDC. Identity:
`https://github.com/<owner>/<repo>/.github/workflows/build-images.yml@<ref>`,
issuer `https://token.actions.githubusercontent.com`. Replaced by the
key-based GitLab CR signing model in §3.

### Original verification command

```bash
scripts/verify_image_signature.sh ghcr.io/<owner>/omnisight-backend:v0.4.0
```

The same script verifies GitLab CR images today with the key-based
public key documented in §3.

### Original retention policy

| Class | Policy |
|---|---|
| Tagged (`v*`, `latest`, `sha-*`) | Kept forever |
| Untagged, ≥ 30 days old | Deleted (subject to floor) |
| Untagged, < 30 days old | Kept |
| Floor | Last 20 untagged versions per package always kept |

Replaced wholesale by the per-tag-class V2 policy in §7 /
[`image-retention-policy.md`](image-retention-policy.md).

### Original V4 env-lock + preflight (OP-1482)

The V4 work introduced per-env JSON lock files
(`staging.env.lock.json`, `canary.env.lock.json`, `prod.env.lock.json`),
[`scripts/load_env_lock.sh`](../../scripts/load_env_lock.sh), and
[`scripts/verify_image_bundle.py`](../../scripts/verify_image_bundle.py).
That machinery is **not** retired — it is the deploy-side preflight
gate the activated pipeline still relies on. See
[`prod-deploy-runbook.md`](prod-deploy-runbook.md) for the
load-lock → verify → `docker compose up -d --wait` procedure.

## See also

- [`gitlab-ci-image-build.md`](gitlab-ci-image-build.md) — full CI
  variable contract, runner requirements, and canary procedure.
- [`gitlab-cr-pull-credentials.md`](gitlab-cr-pull-credentials.md) —
  registry enablement, cleanup policy, deploy-token provisioning and
  rotation.
- [`gitlab-cr-cutover-checklist.md`](gitlab-cr-cutover-checklist.md) —
  the prod `.env` cutover that completed Phase 5 of OP-1468.
- [`image-retention-policy.md`](image-retention-policy.md) — V2
  per-tag-class retention rules.
- [`image-promotion-runbook.md`](image-promotion-runbook.md) — bundle
  promotion (retag by digest, attest the promotion event).
- [`prod-deploy-runbook.md`](prod-deploy-runbook.md) — orchestrator,
  preflight, /readyz gate.
- [`release-cut-runbook.md`](release-cut-runbook.md) — the only event
  that triggers this pipeline (`git push origin vX.Y.Z`) and the
  dual-publish observation window (§§8–11).
- [`fe-be-compat-monitoring.md`](fe-be-compat-monitoring.md) — the
  `/readyz` `frontend_compat_check` that consumes the bundle's
  `contracts.api_supported` claim.
- [`ADR-0038`](../adr/ADR-0038-image-pipeline-on-gitlab.md) — Phase 5
  cutover decision (GHCR → GitLab CR).
- [`ADR-0002`](../adr/ADR-0002-gitlab-primary-github-mirror.md) — the
  governing "GitLab Only" principle.
- [`omnisight-bundle.schema.json`](../../omnisight-bundle.schema.json)
  + [`scripts/build_image_bundle.py`](../../scripts/build_image_bundle.py)
  — bundle manifest schema and CI-side builder (OP-1479).
