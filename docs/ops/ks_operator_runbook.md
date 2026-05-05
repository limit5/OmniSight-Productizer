# KS Operator Runbook

> Status: operator runbook
> Scope: operational procedures for the KS three-tier secret-management
> rollout after the phase-specific code and evidence guards have landed.
> ADR:
> [`../security/ks-multi-tenant-secret-management.md`](../security/ks-multi-tenant-secret-management.md)

Use this runbook when enabling, disabling, validating, or recovering the
KS stack. It assumes the phase-specific docs remain the source of truth
for narrow procedures and keeps the operator path in one place.

## 1. Production image and env readiness

- Target backend image is built from the commit being deployed.
- `docker run --rm <image> python3 -c "from backend.security import envelope, kms_adapters, token_vault"` succeeds.
- Runtime env snapshot includes `OMNISIGHT_KS_ENVELOPE_ENABLED`,
  `OMNISIGHT_KS_CMEK_ENABLED`, `OMNISIGHT_KS_BYOG_ENABLED`,
  `OMNISIGHT_REDIS_URL`, and active KMS / Vault provider knobs.
- N10 ledger is writable and the private security evidence vault is
  reachable.
- Relevant tests are green:
  `backend/tests/test_ks_overall_dod.py`,
  `backend/tests/test_ks113_envelope_security_integration.py`,
  `backend/tests/test_cmek_single_knob.py`,
  `backend/tests/test_byog_single_knob.py`,
  `backend/tests/test_byog_proxy_fail_fast.py`.

## 2. Rollout sequence

1. Deploy the backend image with Tier 1 envelope enabled or with
   `OMNISIGHT_KS_ENVELOPE_ENABLED` unset.
2. Run the Priority I readiness checklist in
   [`priority_i_multi_tenancy_readiness.md`](priority_i_multi_tenancy_readiness.md).
3. Enable or keep disabled `OMNISIGHT_KS_CMEK_ENABLED` based on the
   customer launch plan.
4. If Tier 2 is enabled, run the CMEK wizard verify step and the SIEM
   ingest setup in [`cmek_siem_ingest.md`](cmek_siem_ingest.md).
5. Enable or keep disabled `OMNISIGHT_KS_BYOG_ENABLED` based on the
   Tier 3 customer launch plan.
6. If Tier 3 is enabled, follow
   [`tier2_to_tier3_byog_proxy_upgrade.md`](tier2_to_tier3_byog_proxy_upgrade.md)
   and confirm the proxy health endpoint reports `connected=true` and
   `stale=false`.
7. Run the final regression packet from
   [`ks_overall_rollout_evidence.md`](ks_overall_rollout_evidence.md).
8. Append N10 ledger rows for Priority I readiness, KMS control review,
   CMEK customer evidence, BYOG cutover evidence, and final KS rollout
   evidence as applicable.

## 3. Three-knob rollback

| Incident scope | First lever | Expected effect | Do not do |
|---|---|---|---|
| Tier 1 envelope helper regression | Roll back the backend image; do not use the historical envelope knob to re-enable single-Fernet writes | Existing envelope rows remain decryptable by the previous image | Do not create new legacy single-Fernet provider credential carriers |
| Tier 2 CMEK wizard or KMS integration regression | Set `OMNISIGHT_KS_CMEK_ENABLED=false` and restart workers | Tier 2 onboarding / upgrade hidden; status reports Tier 1 fallback; downgrade remains available | Do not downgrade Alembic or delete CMEK rows |
| Tier 3 BYOG registration or proxy-mode regression | Set `OMNISIGHT_KS_BYOG_ENABLED=false` and restart workers | Tier 3 hidden from settings; Tier 1 and Tier 2 remain visible when CMEK is enabled | Do not route a Tier 3 tenant to direct provider egress |
| Customer disables or revokes a CMEK key | Follow [`cmek_revoke_recovery.md`](cmek_revoke_recovery.md) | New requests return friendly non-retryable 403 until the same key is restored | Do not rotate to a new key in the revoke-recovery runbook |
| Customer proxy unreachable | Keep Tier 3 fail-closed | SaaS returns BYOG error; no direct provider fallback | Do not bypass mTLS or signed nonce checks |

## 4. Tier-specific operator checks

### Tier 1 envelope

- Confirm tenant secrets, OAuth refresh/revoke, and bootstrap/provider
  credentials still write envelope JSON and recover plaintext.
- Confirm every plaintext recovery path emits `ks.decryption`.
- Confirm no new writer can create a single-Fernet carrier.
- Confirm spend anomaly state uses Redis or an approved shared store in
  multi-worker deployments.

### Tier 2 CMEK

- Confirm the tenant admin completed the 5-step wizard.
- Store provider, key id, principal, policy snapshot SHA-256, and verify
  transcript in the evidence vault.
- Confirm native customer KMS audit events match
  [`cmek_siem_ingest.md`](cmek_siem_ingest.md).
- Run Tier 1 -> Tier 2 upgrade and Tier 2 -> Tier 1 downgrade smokes
  before marking a tenant deployed-active.
- If revoke is detected, use
  [`cmek_revoke_recovery.md`](cmek_revoke_recovery.md).

### Tier 3 BYOG proxy

- Confirm the customer uses the canonical `omnisight-proxy` image from
  [`ks_phase3_byog_proxy_ga.md`](ks_phase3_byog_proxy_ga.md).
- Confirm mTLS CA, pinned client certificate fingerprint, certificate
  expiry, and signed-nonce key reference are recorded.
- Confirm provider keys were imported into the customer proxy and then
  cleared from OmniSight according to
  [`tier2_to_tier3_byog_proxy_upgrade.md`](tier2_to_tier3_byog_proxy_upgrade.md).
- Confirm the no-fallback smoke: block the proxy, observe a BYOG error,
  and verify provider egress logs show no direct call.
- For self-hosted customers, confirm image and mode boundaries in
  [`self_hosted_byog_proxy_alignment.md`](self_hosted_byog_proxy_alignment.md).

## 5. Evidence and N10 ledger

Store full transcripts, policy JSON, customer screenshots, vendor
reports, and sensitive details in the private security evidence vault.
Commit only filenames, hashes, and summary counts to git.

Final KS rollout ledger row should include:

- Commit SHA.
- Backend image digest.
- Three-knob env snapshot SHA-256.
- Regression evidence packet SHA-256.
- Tenant smoke SHA-256.
- Customer onboarding packet SHA-256 for each active tier.
- Disposition: `deployed-active`, `risk-accepted`, or `blocked`.

## 6. Per-tier operator packet

Create one packet per tenant tier before declaring the tenant ready. The
packet filename should be
`ks-operator-<tenant-id>-tier-<1|2|3>-<YYYYMMDD>.md` in the private
security evidence vault; commit only its SHA-256 and storage path alias
to N10.

| Field | Tier 1 envelope | Tier 2 CMEK | Tier 3 BYOG proxy |
|---|---|---|---|
| Tenant metadata | tenant id, plan, operator, backend image digest | tenant id, plan, operator, backend image digest | tenant id, plan, operator, backend image digest |
| Enabled knobs | envelope state and Redis/shared-store state | envelope + CMEK state, BYOG state if disabled | envelope + CMEK + BYOG state |
| Required smoke | provider key write/read, OAuth refresh/revoke, `ks.decryption` audit row | Tier 1 -> Tier 2 rewrap, CMEK verify, customer key disable -> graceful 403 -> same-key restore | proxy health, one proxied provider request, streaming smoke where supported, proxy-unreachable no-fallback smoke |
| Evidence attachments | audit export, spend anomaly recipient snapshot, N10 ledger row | policy snapshot, verify transcript, customer KMS audit excerpt, SIEM ingest sample | image digest, mTLS fingerprint, signed-nonce key ref, customer proxy audit excerpt |
| Exit / rollback record | provider key rotation path tested | downgrade path and revoke recovery owner recorded | fail-closed owner and Tier 3 exit plan recorded |

Minimum packet checklist:

1. Record the backend image digest and the exact three-knob env snapshot.
2. Run the tier-specific smoke commands and attach transcripts.
3. Attach customer-visible completion evidence from
   [`ks_customer_onboarding.md`](ks_customer_onboarding.md).
4. Store sensitive attachments only in the private security evidence
   vault.
5. Append an N10 row with packet SHA-256, tenant smoke SHA-256, operator,
   and disposition.

## 7. Production status

This runbook does not deploy runtime code.

**Production status:** dev-only
**Next gate:** deployed-active - operator runs this sequence against the
target backend image, stores the evidence packet in the private security
evidence vault, and appends the final KS rollout N10 row.
