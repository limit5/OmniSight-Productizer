---
id: ADR-0036
title: Forward-Only Deploy Invariant — image is the advance vector, DB follows
status: Proposed
date: 2026-05-16
relates_to:
  - ADR-0023 (Foundation Rebuild)
  - ADR-0033 (Governance Engine + Operator Authority)
  - ADR-0034 (Override Review Lifecycle + Separation of Duties)
  - ADR-0035 (Runner FSM + Error Handling Contract)
  - ADR-0037 (Runner State Substrate Decoupling)
  - docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md §3 Family ⑥
  - docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md (companion contract spec)
  - docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md (supplies the baked MANIFEST.json `alembic_head_in_image`)
  - docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md (the incident this ADR is the structural response to)
ticket: OP-1153 (v2-⑥-1a — this doc); OP-1156 (v2-⑥-2a — ratification ticket for binding adoption)
---

# ADR-0036 — Forward-Only Deploy Invariant

## Status

Proposed (2026-05-16). Filed under Sprint S12.G G.A-v2 Family ⑥ (Image-vs-DB alembic head drift — HIGHEST PRIORITY). Locks in the architectural decision summarised in `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑥ before any `v2-⑥-1bc` / `v2-⑥-2bc` / `v2-⑥-3bc` / `v2-⑥-AlertRule` implementation lands.

This ADR is the **binding** statement of the forward-only-deploy invariant; the companion contract spec (`docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`) is the **implementation contract** for that invariant. The ADR's vocabulary (forward-only, image-as-advance-vector, asymmetric upgrade, rescue-as-only-downgrade-path) is the vocabulary every downstream Family ⑥ ticket cites by name.

Ratification path: this ADR ships in Proposed state with OP-1153 (`v2-⑥-1a`). It transitions to **Adopted** once OP-1156 (`v2-⑥-2a`) lands, which is the dedicated ratification ticket — separate so the binding-doc adoption gets its own review window independent of this drafting ticket. The two-ticket split mirrors ADR-0035's pattern of "draft in one ticket, adopt-via-explicit-ratification in another".

## Context

### The 2026-05-14 outage in one paragraph

On 2026-05-14, the prod backend image was 7 days old; its bundled alembic migrations topped out at `0200_provider_usage_event.py`. The DB had been advanced to `0202` between 2026-05-07 and 2026-05-13 by operator-driven migrations against a freshly-merged develop branch. A Windows-host forced reboot stopped the container; on restart, the 7-day-old image attempted to serve traffic but its `/readyz` migration probe (correctly) returned 503 with `migration_pending: current=0202 latest_file=0200_provider_usage_event.py`. systemd marked the service `failed`, the `depends_on: service_healthy` chain stopped the rest of the compose stack, and the public URL was down from 05:29 (reboot) until 10:50 (manual recovery). The same drift had been silently failing readyz 118 consecutive times in the pre-reboot live container without alerting anyone. Full incident analysis: `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md`.

### Why this is the right level for an ADR

Three reasons:

1. **It is an invariant, not an algorithm.** The contract spec (Family ⑥ §2) describes three states and three behaviors; what makes the choice non-arbitrary is the *invariant* that image is the advance vector and DB follows. That invariant is the load-bearing structural commitment; everything else (the gauge, the hook, the `/readyz` body shape, the rescue CLI) derives from it.
2. **It binds every container restart.** Unlike a per-feature design that affects one code path, this invariant decides what happens on *every* backend startup forever, including operator-rollback scenarios, kubelet evictions, hypervisor migrations, and CI-driven canary-runner promotions. A binding ADR is the right granularity for a decision with that reach.
3. **It binds future families.** Family ⑤ (image surfacing) bakes the `alembic_head_in_image` field because the invariant requires it; Family ⑩ (runner) logs `image_head` per pickup because the invariant requires forensic continuity across restarts; Family ⑧ (shutdown) does NOT detect drift because the invariant assigns that responsibility to Family ⑥. Future families will inherit the same constraints. An ADR is how those constraints get pinned down.

### Lived-experience evidence

The retro's "what actually broke" walk identified five gaps; the gaps are not *coding bugs*, they are *architectural assumptions that failed*:

| Assumption | How it failed |
|---|---|
| The image will be rebuilt when migrations are added. | OP-1035 shipped per-commit image builds to GHCR but no consumer-side recreate; `pull_policy: missing` meant prod kept the old image even with new tags available. |
| `/readyz` is sufficient detection for migration drift. | Polling-coupled; conflated with other readyz checks; can be swapped to `/livez` (and was, in the recovery), zeroing detection. |
| Operator will always see the failing healthcheck and act. | 118 consecutive failing healthchecks before any operator noticed; healthcheck failures are visible in `docker inspect .State.Health.FailingStreak` but not surfaced to any actionable channel. |
| Rebuild is a routine ritual; deploy automation closes the loop. | Half-pipeline state: build-half exists, deploy-half does not. The half-state is worse than no-state because it creates false confidence. |
| Rollback is safe because we have automation. | No automation distinguishes "rollback image is acceptable for this DB" from "rollback image cannot serve this DB". Rolling back image without rolling back DB is a silent-corruption class. |

### Why "we just deploy more often" doesn't dissolve it

The contract spec (Family ⑥ §1.3) enumerates five reasons; in ADR terms they reduce to: cadence is not a contract. Saying "we will rebuild daily" produces no number anyone can audit, no signal anyone can alert on, no contract any future engineer can lean on. A binding invariant produces all three.

## Decision

**The forward-only-deploy invariant is the canonical path for OmniSight production:**

> *The backend image's bundled alembic migration files are the advance vector for the database schema. The database moves forward at the direction of the image; the image never moves the database backward. Moving the database backward is exclusively an operator-authority action, surfaced via the `omnisight rescue` CLI with L2 fingerprint + audit-row + backup. Any backend image whose `alembic_head_in_image` is BEHIND the live `db_head` refuses to start (exit 78, `EX_CONFIG`) until the operator resolves the misalignment.*

### The three-state behavior (Option C asymmetric upgrade)

| State | Definition | Container behavior | Authority required |
|---|---|---|---|
| **AHEAD** | `image_head > db_head` | Auto-run `alembic upgrade head` inside a Postgres advisory lock; idempotent | none (image's own authority via review-at-build-time) |
| **EQUAL** | `image_head == db_head` | No-op; start normally | none (steady state) |
| **BEHIND** | `image_head < db_head` | Refuse to start; exit 78; structured log with remediation hint | rescue CLI required to recover; L2 fingerprint per ADR-0033 |

The asymmetry is the point. Forward is automatic because the image is the canonical advance source. Backward is operator-explicit because moving DB backward is destructive (lossy migrations cannot be inverted; the operator must consciously elect the loss).

### Specific binding consequences

1. **Image is the alembic-head source of truth at deploy time.** `MANIFEST.json.alembic_head_in_image` is the authoritative answer to "what migrations does this image carry"; runtime filesystem scans are forbidden as a second source (the filesystem is tamperable post-build per Family ⑤ §4.3).
2. **Multi-head at build time fails the build.** `alembic heads` returning >1 head MUST fail the image build with structured remediation pointing at `alembic merge`. No silent pick-first; no warn-and-continue. (Implementation: `scripts/bake-image-manifest.sh` exit 92 per Family ⑤ §4.4 + Family ⑥ §9.)
3. **The advisory lock is the multi-replica coordinator.** Backend replicas startup-coordinate via Postgres `pg_try_advisory_lock(alembic_lock)`; at most one replica runs the upgrade. (Implementation: `v2-⑥-2bc` per Family ⑥ §6.3 / §8.1.)
4. **Drift is detected always-on, not only at healthcheck poll.** A periodic in-process gauge `omnisight_alembic_drift` (60 s cadence) surfaces the state independently of `/readyz`. (Implementation: `v2-⑥-1bc` per Family ⑥ §4.)
5. **`/readyz` 503 carries a structured `remediation` field.** Operators see two concrete recovery options (redeploy newer image, or rescue-downgrade DB) directly in the response body. (Implementation: `v2-⑥-3bc` per Family ⑥ §5.)
6. **The rescue CLI is the only sanctioned downgrade path.** No other code path downgrades the DB — not the startup hook, not the healthcheck, not the runner. Raw `alembic downgrade` against prod is forbidden by SOP. (Implementation: `v2-⑥-RescueCLI` per Family ⑥ §7.)
7. **Rescue actions write an immutable audit row.** `runner_audit_events` row written BEFORE the alembic downgrade invocation per ADR-0034's audit contract; row persists across rescue-process kill. (Implementation: `v2-⑥-RescueCLI` per Family ⑥ §8.7.)
8. **Exit 78 is the BEHIND exit code.** Not 1, not 70, not "fail-silent and serve partial traffic". `EX_CONFIG` (78) communicates to systemd / docker-compose / kubelet that this is a deploy-time misconfiguration — not a transient failure (so don't auto-restart) and not a program crash (so don't page on `up == 0` generically; page on `OmniSightAlembicDrift{drift_value=2}` instead).
9. **Family ⑧ (graceful shutdown) does NOT detect drift.** Cross-family delegation: post-restart drift detection is Family ⑥'s gauge. Family ⑧'s operator-runbook (`v2-⑧-4a-Doc`) instructs the operator to check the Family ⑥ alert after any forced shutdown — not to invoke a Family ⑧-specific drift check.
10. **Family ⑤ (image surfacing) is a redundant detector by design.** The deployment-audit script's daily `T1.alembic_head_in_image vs T3.head` comparison runs out-of-band from the in-process gauge; the two are expected to agree and disagreement is a forensic event (per Family ⑤ §8.1 + Family ⑥ §10.2).

## Alternatives considered

### Alternative A — auto-up always (Option A)

**Definition:** every startup runs `alembic upgrade head` unconditionally.

**Rejected because:**

1. **Rolled-back image silently advances DB further.** With `image_head=0200` and `db_head=0202`, the image's `upgrade head` is a no-op (image has no `0201` / `0202` files), so the symptom is "DB stays at 0202 while image serves 0200" — IDENTICAL to the 2026-05-14 outage. Option A solves AHEAD but does nothing for BEHIND.
2. **Bakes "image always advances DB" assumption.** Hotfix images built off release branches may legitimately ship without latest migrations; Option A would refuse to start them OR would apply stale migrations.
3. **Removes the operator's "stop the world" option.** Sometimes the operator wants the system to halt and not advance the DB further; Option A never halts. The asymmetric Option C halts when the image is BEHIND, which is exactly when halting is the correct response.

### Alternative B — always block (Option B)

**Definition:** every startup refuses to start if `image_head != db_head`; operator manually runs migration before each start.

**Rejected because:**

1. **Deploy script bug = hard outage.** If the routine deploy's migration step silently skips, Option B refuses to start the new image, refuses to start the old image (it's gone), and the system is down. Option C lets the new image self-heal the AHEAD case.
2. **Per-deploy operator toil compounds.** Every routine deploy carries a manual migration step; across hundreds of deploys, someone forgets, and we are back to the failure mode of reason 1.
3. **Does not match the operator's mental model of "deploy this image".** Operators expect "deploy" to be one action. Option B splits it into two; the second action is silent failure if forgotten.

### Alternative D — block forward, allow backward (mirror image of C)

**Definition:** image-BEHIND is auto-downgrade; image-AHEAD refuses.

**Rejected because:**

1. **Downgrade is generally lossy.** Migrations that drop columns or transform data cannot be cleanly reversed. Auto-downgrade silently destroys production data.
2. **Inverts the operator's intent.** A forward deploy IS the operator's intent; refusing it is hostile. A rollback is sometimes the operator's intent but is sometimes a script bug; auto-doing it would amplify the bug.
3. **Makes the DB the advance vector.** Inverts the entire architecture of "image carries code + migrations together"; would require a different release artifact model where DB schema changes are separately shippable.

### Alternative E — no in-container decision; deploy-script decides

**Definition:** the backend container itself is dumb. The deploy script (`scripts/auto-redeploy.sh`) decides AHEAD / EQUAL / BEHIND before starting the container; container always assumes EQUAL.

**Rejected because:**

1. **Some restarts are not deploy-driven.** systemd, kubelet, host-reboot all start the container without the deploy script's involvement. If the container assumes EQUAL, those non-deploy restarts walk into AHEAD or BEHIND blind.
2. **The decision is information-symmetric on both sides.** The container has access to its own `image_head` (baked) and the DB's `db_head` (queryable); the deploy script has access to the same two values. Putting the decision in only one place loses the redundancy; putting it in both is defense-in-depth (Family ⑤ §9.2 / Family ⑥ §6).
3. **The hook can run in <1 s.** It is not a meaningful startup-latency cost.

## Consequences

### Positive

- **Drift becomes impossible to mask.** AHEAD self-heals; BEHIND is loud (exit 78 + alert + `/readyz` body). Operators see the state on every restart, not 118 polls into a stale image.
- **Routine forward deploys need no operator action.** `docker compose pull && up -d` is sufficient; the hook handles migrations automatically.
- **Rollback semantics become explicit.** "Rollback this image" is now answered by "use the rescue CLI to downgrade the DB to a revision the rollback image can serve" — a documented, audited, backed-up path. Today's path is "deploy the rollback image and hope" with silent-corruption fallback.
- **The defense generalizes.** Family ⑤ (image-versus-GHCR audit), Family ⑩ (runner pickup logging), and future families inherit the same `image_head` / `db_head` truth-sources. Adding a new defense dimension (e.g., a future Postgres-version drift check) plugs into the same gauge / `/readyz` / AlertRule frame.
- **Audit trail is permanent.** Every rescue action writes `runner_audit_events`; forensics across rescue-process restarts are possible (per Family ⑥ §8.7).

### Negative

- **Rolled-back image needs operator action.** Today a rollback "just works" (it serves traffic, even if some queries silently 500). Tomorrow a rollback fails fast and requires either (a) deploy a newer image OR (b) rescue-downgrade DB. This is more operator toil at the moment of rollback — but only for the (rare) backward-incompatible rollback case. **Net:** trades silent corruption for explicit operator action. ADR considers this the correct trade.
- **The advisory-lock cost.** A backend startup with `image_head > db_head` now holds an advisory lock on `alembic_lock` during the upgrade; concurrent replica starts queue. Worst-case startup time = `(upgrade duration) × (replica count - 1)`. For our single-replica prod today this is zero impact; if we go multi-replica, the queue is bounded by the lock-timeout of 60 s (per Family ⑥ §6.3); replicas that hit the timeout exit 78 and the next scheduler tick retries them.
- **The rescue CLI is a new operator surface to design + secure.** L2 fingerprint custody, GPG-encrypted backup path, eligibility-matrix maintenance. This is real work; it lives in `v2-⑥-RescueCLI` (post-rc2 per Family ⑥ §11).
- **Every new alembic migration carries a `downgrade_safe: …` tag obligation.** Per Family ⑥ §8.6 subcontract 6. Migration authors must classify their migration; CI lint enforces. Trivial overhead per migration; non-trivial backfill for the existing `0001..<current>` set (one-shot backfill ticket prerequisite to `v2-⑥-RescueCLI`).
- **Healthcheck wiring must stay on `/readyz` (not `/livez`) for the systemd dependency chain to work as designed.** The 2026-05-14 recovery's swap-to-/livez restored the URL but disabled the drift gate; reverting that swap is part of the rc2 closure checklist. Family ⑥ §10.3 + Family ⑦ ContractTest enforce this going forward.

### Operator override path

The rescue CLI is the **only** sanctioned downgrade path. It requires:

- **L2 fingerprint** per ADR-0033 — operator's pre-registered cryptographic identity must match an entry in `runner_operator_fingerprints`.
- **Backup capture** before any downgrade — `pg_dump | gpg --encrypt` to `/var/omnisight/rescue-backups/<ts>-<from>-<to>.sql.gpg`.
- **Eligibility check** — refuses if any spanned migration is tagged `downgrade_safe: irreversible`; warns + requires `--force` if `lossy-data`.
- **Audit row** before the alembic downgrade fires — `runner_audit_events` write happens first; the downgrade is gated on the write succeeding.

This is the override path. Outside of it, raw `alembic downgrade` against prod is forbidden by SOP (cited in `docs/sop/...` referenced from this ADR's section above). If an operator runs raw alembic against prod, that's an incident class — caught after-the-fact by the missing audit row and the missing backup file.

### Cross-ADR consequences

- **ADR-0033 (governance engine + operator authority):** the rescue CLI inherits the L2-fingerprint requirement; this ADR does not modify ADR-0033.
- **ADR-0034 (override review + separation of duties):** the rescue CLI's `runner_audit_events` row participates in the same separation-of-duties audit; this ADR does not modify ADR-0034.
- **ADR-0035 (runner FSM + error handling):** the runner reads `image_head` per pickup but does not gate on drift (runner pickup is upstream of backend `/readyz`); this ADR does not modify ADR-0035.
- **ADR-0037 (runner state substrate):** Family ⑥ stores no runner-coordination state; ADR-0037's `runner_claims` table is orthogonal. The two ADRs are independent.

### Reversal cost

This ADR is *adoptable to reverse*. If a future operator concludes that Option A or B is preferable, the reversal is:

1. Delete the startup hook in `v2-⑥-2bc`'s module (subcontracts 1, 3, 4 wiring).
2. Update `/readyz` body shape per a new contract version.
3. Retire the `alembic_drift` gauge or repurpose it.
4. Update the contract spec to reflect the new invariant.

The reversal cost is non-zero but bounded — same order as the initial adoption. The rescue CLI's backup + audit features would remain useful even under Option A or B.

## Adoption gate

This ADR transitions from **Proposed** to **Adopted** when:

1. **OP-1156 (`v2-⑥-2a`)** lands as Resolved + Done, having received a +2 from a non-AI reviewer per CLAUDE.md §"Safety Rules".
2. The contract spec (`docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md`) has been reviewed alongside this ADR; reviewers explicitly approve the asymmetric-upgrade invariant.
3. No P0 / P1 defect in either doc remains open at the time of the ratification ticket's Code Review.

Until those three hold, this ADR is informative-only; impl tickets `v2-⑥-1bc / 2bc / 3bc / AlertRule` MAY block on the Adopted transition before merging (this is the release captain's choice; we recommend they DO so to avoid implementing-against-a-moving-target).

## References

- `docs/sprint-s12/2026-05-16-v2-family6-image-db-drift-contract.md` — companion contract spec, this ADR's binding implementation contract.
- `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` §3 Family ⑥ — parent spec; the operator-locked decision (Q1, 2026-05-14) is recorded there.
- `docs/sprint-s12/2026-05-16-v2-family5-image-surfacing-contract.md` — Family ⑤; supplies the `MANIFEST.json.alembic_head_in_image` field this ADR depends on.
- `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` — the incident this ADR is the structural response to.
- `docs/adr/ADR-0033-governance-engine-and-operator-authority.md` — L2 fingerprint authority basis for the rescue CLI.
- `docs/adr/ADR-0034-override-review-and-separation-of-duties.md` — `runner_audit_events` schema basis.
- `docs/adr/ADR-0035-runner-fsm-and-error-handling.md` — pattern for split-out binding ADR + audit-style appendix doc.
- `docs/adr/ADR-0037-runner-state-substrate-decoupling.md` — pattern for ADR ratification distinct from drafting ticket.
- JIRA OP-1153 (this doc + the contract spec); OP-1156 (ratification ticket).

---

*ADR-0036. — OP-1153 / v2-⑥-1a / 2026-05-16. Proposed; adoption gated on OP-1156.*
