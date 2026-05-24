# Deploy-line deep audit — dev → testing → staging → canary → prod (2026-05-25)

3-way audit (me/live-state + 2 sub-agents + codex), cross-verified against live state, ADR-0040/0042,
the 2026-05-22 release-train readiness handoff, and the prod-deploy runbook. Sources reconciled; two
sub-agent findings were corrected by live evidence (noted). Codex review:
`docs/audit/codex-reviews/deploy-pipeline-audit-codex-2026-05-25.txt`.

## Verdict
The **happy path works** — we shipped v0.6.0 end-to-end today (candidate CI → staging → canary → promote
→ prod), and the core safety primitives are real + verified (env-contract guard, fail-closed-on-absent
overlay, promote-time digest binding, candidate signing, atomic lock writer). **But the GATING/ENFORCEMENT
layer that's supposed to PROTECT the line is substantially half-inert or wrong, ADR-0040/0042 are only
partially enforced, and there are real security leaks.** The line *ships*; it does not fully *protect*.

## BLOCKER
1. **[COSIGN/SEC] CI cosign sign+attest omit `--tlog-upload=false`** → candidate image digests + metadata
   leak to the public Sigstore Rekor log. The v0.6.0 candidate signing today leaked. `.gitlab-ci.yml:404,466,497,550`
   (0 `tlog-upload=false`). Promote (`sign_promotion_attestation.py`) is correct; only CI build-signing leaks.
   Violates handoff §6/§55. (Recurrence of the v0.5.0 Rekor-leak lesson.)
2. **[ADR/SEC] GHCR not decommissioned** — `.github/workflows/build-images.yml` (REGISTRY=ghcr.io, on push+`v*`+cron)
   + `docker-publish.yml` are `state=active`. **The OP-1687 mirror restore today re-triggered them** (Build Images /
   Docker Publish / Release fired on the 14:41Z push, all failed). Latent: fixing GHCR creds resumes GHCR pushes +
   `v*`-tag builds (RT-20 divergent-digest hazard). Violates ADR-0042:20-21, ADR-0040:34.
3. **[STAGING] `/readyz` overlay gate checks lock SHAPE, not SUBSTANCE** — `_check_deploy_overlay` (health.py:481-490)
   returns true whenever the lock exists + is well-formed; never cross-checks lock digests vs the *running* image.
   A stale/hand-edited well-formed lock passes /readyz + the staging gate. (Digest binding exists only at promote,
   `assert_staging_gate_passed`.)
4. **[CANARY] smoke gate permanently dead** — `staging-gate-smoke.timer` not installed (only `.service` linked);
   last smoke record RED (401 no-credential), wrong revision (`fa862a1`), >2 days stale. R3 smoke gate can never green.
5. **[PROMOTE] `promote_image_bundle.py` default registry is WRONG** — `DEFAULT_REGISTRY = sora.services:49154/omnisight/OmniSight-Productizer`
   (web port + wrong case) vs the real `:49160/omnisight/omnisight-productizer`. Omitting `--registry` promotes to the
   wrong CR namespace. (promote_image_bundle.py:51.) Violates ADR-0042/handoff §12.
6. **[PROD] `deploy-prod.sh --digest` path broken** — writes `OMNISIGHT_IMAGE_DIGEST` then sets `OMNISIGHT_IMAGE_TAG`
   to the digest; compose ignores `OMNISIGHT_IMAGE_DIGEST` + uses tag syntax → invalid ref or deploys old tag.
   (deploy-prod.sh:255-278 vs docker-compose.prod.yml:152.) The advertised "deploy by digest" (ADR-0040:36/82) is unwired.
7. **[DEV] A1 `REVOKE CONNECT` not implemented** — the "only provable" DB-level dev→prod prevention exists only in the
   dev-compose comment + env_contract docstring; no migration/script applies it. App-level A2 guard works; DB-level
   belt-and-suspenders does not. Violates the A1 design doc.

## HIGH
8. **[PROD/B] prod-deploy orchestrator (path B) is multiply broken** (OP-1690 + new): router unmounted (dead API,
   main.py never includes `prod_deploy.router`); Slack gate = env-allowlist stub; smoke target = nonexistent
   `staging-mirror.internal:8080`; `ProductionDeployOrchestrator.ship()` exports `OMNISIGHT_RELEASE_IMAGE_TAG` but
   compose consumes `OMNISIGHT_IMAGE_TAG` → may deploy whatever `.env` says. (production_release.py:281,292.)
9. **[CANARY] documented canary runbook drives a STUB SLO monitor** — `canary-runbook.md:41` instantiates
   `CanaryOrchestrator` with no `monitor=` → `_StubMonitor` always healthy (canary.py:121-126,178). Following the
   runbook, the SLO half of the canary gate is a silent no-op.
10. **[CANARY] two divergent canary subsystems + p95 unenforced** — live `canary_rollout.CanaryController` +
    `RollingDeploySloMonitor` (real) vs the stub `orchestrator/canary.py` the runbook points at; even the real
    monitor never populates p95 (canary_rollout.py:73-77) → latency SLO unenforced on both paths.
11. **[CANARY] smoke suite uncredentialed** — `prod_smoke_test.py` needs `OMNISIGHT_API_TOKEN`; the smoke unit
    only optionally sources it → 401 → can't green even if the timer were installed.
12. **[DEV] dev backend UNHEALTHY** — `omnisight-dev-backend-a-1` up 20h unhealthy; dev stage degraded.
13. **[STAGING] staging `.env` `OMNISIGHT_IMAGE_TAG=latest`** — defeats ADR-0040 fail-closed-on-unset intent
    (`${VAR:?}` only guards truly-unset); latent "deploy newest regardless of approval".
14. **[CI] dead `^v`-tag pipeline family + `auto_tag_release` still present** — `.tag_rules` jobs + auto_tag_release.py
    + auto-tag-release.service exist (unit NOT currently armed). If enabled / a `v*` tag is pushed → rebuild a
    divergent digest (RT-20 hazard) + ship zero-digest bundle. Violates ADR-0040 RT-20 + migration step 1.
15. **[CROSS] gate + control-plane units run from a SEPARATE checkout** (`/home/user/sora-bridge`) — dual-checkout
    drift vs the live `/home/user/work/sora/...`. (OP-1608 "stranded on main@rc1" itself appears RESOLVED — sora-bridge
    HEAD is on develop 8a8b640 — but the two-checkout topology + the gate units' WorkingDirectory=sora-bridge remain.)

## MED
16. **[DEV] A2 guard misses 3 `sa.create_engine` prod-DB paths** — deploy_audit.py:137, release_conductor/state_machine.py:177,
    orchestrator/prod_deploy.py:235 open `OMNISIGHT_DATABASE_URL` with no env_contract import, contradicting the "every
    DB entry point" claim.
17. **[DEV] `OMNISIGHT_CI_MODE`/`PYTEST_CURRENT_TEST` silent bypass** — `enforce_env_db_contract` returns early when
    in-test + cls!=prod → dev↔staging cross-contamination un-caught under CI_MODE leak. (env_contract.py:179-181.)
18. **[DEV] prod compose has BOTH a PG URL and a SQLite PATH set** — live prod correctly runs PG (`pg-primary`, URL wins),
    but `OMNISIGHT_DATABASE_PATH=/app/data/omnisight.db` is also set → latent SQLite-fallback footgun if the URL ever drops.
    (Corrects sub-agent A's "prod runs SQLite" — it does not; verified live.)
19. **[DEV] dev stack ≠ staging topology** — only `postgres + backend-a + inert dag-executor`; no frontend/caddy/backend-b
    despite the compose header ("Mirrors deploy/staging/") + `.env` declaring those ports. Incomplete vs docs.
20. **[ARCH] no distinct "testing" environment** — "testing" = CI gates only; no integration-test env between dev + staging.
21. **[CROSS] `deployment-audit.sh` alembic check is buggy** — compares prod head vs the *local stale checkout* (0247)
    → false RED "run upgrade on prod" when prod is correctly ahead (0248).
22. **[CROSS] prod compose working_dir is stale + dirty** — main checkout 37 commits behind develop + 22 uncommitted files;
    prod deploys run from a drifted context (compose matched this time by luck).
23. **[STAGING] `/api/version` serves all-zero placeholder `*_image_digest`** alongside the real `deployed_digest_*`;
    older clients keying on the former get zeros. Violates ADR-0040 §85.
24. **[STAGING] canary "green" is `/healthz`+`/readyz` 2xx only, not digest-bound** — a wrong-but-healthy deploy yields
    a green canary line (digest binding only at promote).
25. **[CI] `check_deploy_ref.sh` Layers 1+2 gate on signed `v*` git tags** that RT-20 guarantees never exist — dead/misleading deploy gate.
26. **[ENV] gate/promote trip pydantic `extra_forbidden`** when run from the operator's interactive shell (env pollution)
    → fail-open; must run with `env -i`. (Found in the v0.6.0 exercise.)

## LOW
27. **[DEV] dev backend-a `8020:8000` on 0.0.0.0 + AUTH_MODE=open + DEBUG=1** → unauth admin surface if 8020 isn't localhost-confined.
28. **[CI] fast-gate impact policy**: devops/tooling = lint-only (no pytest), docs = skip → deploy-affecting changes merge "green" with no test execution.
29. **[CROSS] hands-off release automation disabled** — `auto-promote-develop.timer` + `auto-promote-main` disabled (manual cuts only; RT-04a/auto-trigger inactive).
30. **[CROSS] `auto-promote-main` flagged RED by `deployment-audit.sh`** but is expected-dead under ADR-0040 (no-main) — stale audit check.
31. **[DOC] doc-drifts** — runbook says prod runs `rc5-hotfix4` (was `v0.5.0`, now `v0.6.0`); handoff §7 says OP-1608 "stranded on main@rc1" (now on develop); CI cosign var naming `SORA_COSIGN_*` (ADR-0023) vs `COSIGN_*` (.gitlab-ci.yml).
32. **[DEV] no `deploy/dev/.env.example`** committed (staging has a template).
33. **[STAGING] overlay-lock change needs manual `--force-recreate`** to be re-read (unautomated) → /readyz can advertise stale identity after a lock rewrite.
34. **[STAGING] staging PG co-tenant with prod host** — isolation by DB name/port/volume only; OP-927 dedicated host pending.
35. **[CROSS] dead/aspirational deploy targets** — `deploy/{k8s,helm,render,railway,digitalocean,blue-green}` not live (no kubectl/helm), carry stale `ghcr.io` refs, misrepresent the real (compose) line.

## Verified WORKING (disproof attempts that failed)
- env-contract guard: all 5 hookpoints real + unconditional; `classify_dsn` fail-closed (stricter than design); DISABLE refused when prod.
- candidate-* CI path: seals REAL post-build digests; sign/sbom/attest real (fail-closed abort on failure); `del(.images.bridge)`.
- promote-by-digest: retag = identical digest; `assert_staging_gate_passed` binds evidence to promoted digests; v0.6.0 row `final_digest_equality:true`.
- deploy-overlay: fail-closes (503) on ABSENT/incomplete lock when `OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1`; writer atomic + 644.
- staging stack: boot-survivable, healthy, isolated (ports/DB/volumes); canary timer installed/enabled/running, last 3 green vs develop-tip.
- v0.6.0: shipped end-to-end + cosign attestation verifies.
- CI cosign: empirically WORKS (signs + verifies; #137 green) — but insecurely (finding #1).
- prod runs Postgres (pg-primary), A2 guard active + protecting it.

## Cross-cutting themes
1. **Ships but doesn't protect** — smoke dead, canary SLO stub, overlay shape-not-substance, canary not digest-bound.
2. **ADR-0040/0042 partially enforced** — GHCR workflows live (re-armed by the mirror), `^v`-tag path present, ghcr refs, audit-check main vestiges.
3. **Security leaks** — CI cosign → public Rekor; dev unauth surface; mirror re-arm of GHCR.
4. **Doc-vs-reality drift** — prod tag, OP-1608, the "sanctioned" orchestrator path B (dead).
5. **Operational hygiene** — two checkouts, stale+dirty prod compose dir, buggy audit check.
