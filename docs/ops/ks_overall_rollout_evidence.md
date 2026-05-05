# KS Overall Rollout Evidence Index

> Status: final KS Definition of Done evidence index
> Scope: the overall KS row after Phase 1 envelope, Phase 2 CMEK,
> Phase 3 BYOG proxy, and cross-cutting security gates have all landed.
> Source ADR:
> [`ks-multi-tenant-secret-management.md`](../security/ks-multi-tenant-secret-management.md)

This page closes the final KS "overall" row. It does not replace the
phase-specific evidence pages. It points reviewers to the artifacts that
prove the three runtime knobs are distinct, the ADR is complete, the
operator runbook is complete, and customer onboarding material exists
for each tier.

## 1. Scope boundary

This index covers only the final all-KS rollout row:

- KS full stack can run with the envelope, CMEK, and BYOG knobs disabled
  independently.
- Existing Tier 1 / AS / OAuth / customer-secret behavior does not
  regress when later-tier knobs are disabled.
- The ADR is complete enough to be the architectural source of truth.
- Operator runbook coverage is complete across normal rollout,
  rollback, revoke recovery, SIEM ingest, Tier 2 -> Tier 3 migration,
  and production evidence gates.
- Customer onboarding material is complete for Tier 1, Tier 2, and
  Tier 3.

Current status is `dev-only`. The repository contains code, docs, and
drift guards. Production status becomes `deployed-active` only after an
operator records a production image digest, env snapshot, tenant smoke,
and per-tier onboarding packet in the private security evidence vault.

## 2. Three-knob rollout matrix

| Knob | Disabled behavior | Existing behavior that must remain green | Guard |
|---|---|---|---|
| `OMNISIGHT_KS_ENVELOPE_ENABLED=false` | Historical migration rollback marker only; KS.1 completion does not let writers create legacy single-Fernet provider credential carriers. | Tier 1 envelope helper still encrypts/decrypts; bootstrap/provider credentials still persist JSON envelope material; AS session token packing stays envelope-backed. | `backend/tests/test_security_envelope.py`, `backend/tests/test_ks113_envelope_security_integration.py`, `backend/tests/test_bootstrap_llm_provision.py`, `backend/tests/test_ks_overall_dod.py` |
| `OMNISIGHT_KS_CMEK_ENABLED=false` | Tier 2 wizard, Tier 1 -> Tier 2 upgrade, and live CMEK checks are hidden or skipped; tenant status presents Tier 1 fallback; Tier 2 -> Tier 1 downgrade remains available. | Tier 1 envelope read/write paths remain available; disabling CMEK must not disable envelope writes or legacy AS/OAuth/customer secret flows. | `backend/tests/test_cmek_single_knob.py`, `backend/tests/test_cmek_phase2_regression.py`, `backend/tests/test_ks_overall_dod.py` |
| `OMNISIGHT_KS_BYOG_ENABLED=false` | Tier 3 BYOG proxy registration and proxy-mode selection are hidden; Tier 1 and Tier 2 status contracts stay unchanged when CMEK is enabled. | Direct Tier 1 and Tier 2 flows continue; proxy unreachable still fails closed for Tier 3 tenants and never falls back to direct provider egress. | `backend/tests/test_byog_single_knob.py`, `backend/tests/test_byog_proxy_fail_fast.py`, `backend/tests/test_ks_phase3_byog_proxy_ga.py`, `backend/tests/test_ks_overall_dod.py` |

All three knobs are resolved lazily through `feature_flags` /
env-backed helpers, so a multi-worker deployment derives the same value
from the registry or process environment without sharing Python
module-global memory.

## 3. Regression evidence packet

Before marking this row deployed-active, store one evidence packet in
the private security evidence vault containing:

- Backend image digest and requirements lock hash.
- Runtime env snapshot for `OMNISIGHT_KS_ENVELOPE_ENABLED`,
  `OMNISIGHT_KS_CMEK_ENABLED`, `OMNISIGHT_KS_BYOG_ENABLED`,
  `OMNISIGHT_REDIS_URL`, and the active KMS / Vault provider knobs.
- Test transcript for `backend/tests/test_ks_overall_dod.py`,
  `backend/tests/test_security_envelope.py`,
  `backend/tests/test_ks113_envelope_security_integration.py`,
  `backend/tests/test_cmek_single_knob.py`,
  `backend/tests/test_byog_single_knob.py`, and
  `backend/tests/test_byog_proxy_fail_fast.py`.
- Three staging smokes:
  `all knobs false`, `CMEK false / BYOG true`, and
  `CMEK true / BYOG false`.
- One tenant smoke proving OAuth refresh/revoke, tenant secrets, and
  bootstrap/provider credentials still recover plaintext through
  envelope-backed storage and emit `ks.decryption` audit rows.
- One no-fallback smoke proving a Tier 3 proxy outage returns BYOG error
  payloads and does not call direct provider egress.

## 4. ADR completeness map

The ADR is complete when it contains all of the following sections and
links to the evidence/runbook/customer-facing material below:

| ADR area | Required content | Evidence |
|---|---|---|
| Decision | 3 tier x 3 phase model, BP/KS/HD schedule position, BP-period multi-tenant policy | `docs/security/ks-multi-tenant-secret-management.md` sections 2 and 12 |
| Architecture | Tier 1 envelope, Tier 2 CMEK, Tier 3 BYOG proxy, KMS adapter contract, AS Token Vault evolution | ADR sections 3, 4, and 6 |
| Rollback knobs | `OMNISIGHT_KS_ENVELOPE_ENABLED`, `OMNISIGHT_KS_CMEK_ENABLED`, `OMNISIGHT_KS_BYOG_ENABLED`, with Tier 1 no legacy fallback | ADR section 8.1 and this index section 2 |
| Test strategy | Phase 1/2/3 acceptance tests and final compat regression | ADR section 9 and this index section 3 |
| Risk and cross-cutting | R46-R50, incident response, pentest, SOC 2, GDPR / DSAR | ADR sections 10 and 11 plus `docs/ops/ks_cross_cutting_evidence.md` |
| Operations and onboarding | Operator runbook and per-tier customer onboarding material | `docs/ops/ks_operator_runbook.md`, `docs/ops/ks_customer_onboarding.md` |

## 5. Operator and customer material

The operator runbook is
[`docs/ops/ks_operator_runbook.md`](ks_operator_runbook.md). It ties
together:

- Production image and env readiness.
- Three-knob rollout / rollback.
- Tier 1 envelope validation and Priority I readiness.
- Tier 2 CMEK onboarding, revoke recovery, SIEM ingest, upgrade, and
  downgrade.
- Tier 3 BYOG proxy deployment, Tier 2 -> Tier 3 cutover, no-fallback
  smoke, and self-hosted image alignment.
- Evidence vault and N10 ledger updates.

The customer onboarding guide is
[`docs/ops/ks_customer_onboarding.md`](ks_customer_onboarding.md). It
contains per-tier prerequisites, customer actions, OmniSight operator
actions, completion criteria, rollback / exit behavior, and escalation
notes for Tier 1 envelope, Tier 2 CMEK, and Tier 3 BYOG proxy.

## 6. Production status

This index does not deploy runtime code.

**Production status:** dev-only
**Next gate:** deployed-active - rebuild the production backend image,
capture the three-knob env snapshot, run the regression evidence packet
above in staging or production-equivalent topology, attach per-tier
customer onboarding packets, and append the final N10 KS rollout row
with image digest, evidence SHA-256, tenant smoke SHA-256, and
disposition `deployed-active`.
