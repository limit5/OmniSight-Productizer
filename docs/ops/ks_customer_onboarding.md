# KS Customer Onboarding Guide

> Status: customer onboarding material
> Scope: per-tier onboarding steps for KS secret management.
> ADR:
> [`../security/ks-multi-tenant-secret-management.md`](../security/ks-multi-tenant-secret-management.md)

This guide is the customer-facing handoff for choosing and operating the
KS secret-management tier. Keep it aligned with the operator runbook and
the ADR; do not add a new tier here before the ADR is updated.

## 1. Choose a Tier

| Tier | Best fit | Customer-owned setup | Exit behavior |
|---|---|---|---|
| Tier 1 envelope | Individuals, startups, small teams, default SaaS tenants | No KMS setup; paste provider keys into OmniSight | Rotate/delete provider keys in OmniSight; OmniSight purges tenant DEK material during deletion |
| Tier 2 CMEK | Enterprise and regulated teams that need customer-managed KMS keys | AWS KMS, Google Cloud KMS, or Vault Transit key plus OmniSight encrypt/decrypt policy | Disable the customer key to stop new decrypts within 60 seconds; restore the same key to recover |
| Tier 3 BYOG proxy | Banks, government, healthcare, defense, or air-gapped customers | Run `omnisight-proxy` in the customer VPC with provider keys, mTLS, signed nonce, and audit logs | Proxy unreachable or removed means OmniSight fails closed; leaving Tier 3 requires fresh onboarding |

## 2. Tier 1 Envelope Onboarding

Customer steps:

1. Create the OmniSight tenant.
2. Add provider keys through the normal tenant settings flow.
3. Configure alert recipients for spend anomaly notifications.
4. Rotate provider keys through the provider console and OmniSight
   settings when a key is suspected of exposure.

OmniSight operator checks:

- Confirm tenant provider keys are stored as envelope JSON, not legacy
  single-Fernet carriers.
- Confirm decrypt audit rows appear as `ks.decryption`.
- Confirm spend anomaly alerts are configured and backed by Redis or an
  approved shared store.

Completion criteria:

- Customer can run one low-risk agent invocation.
- Customer can rotate a provider key.
- Audit export shows who decrypted which key for which request.

## 3. Tier 2 CMEK Onboarding

Customer prerequisites:

- One AWS KMS key, Google Cloud KMS CryptoKey, or Vault Transit key.
- Ability to grant OmniSight encrypt/decrypt access scoped to the
  tenant encryption context.
- Customer SIEM or audit-log destination when audit ingestion is
  required.

Customer steps:

1. Open the CMEK wizard and choose AWS KMS, Google Cloud KMS, or Vault
   Transit.
2. Copy the generated policy JSON or Vault policy.
3. Apply the policy in the customer cloud or Vault environment.
4. Paste the key id and principal back into the wizard.
5. Run the verify step and save the Tier 2 configuration.
6. Configure customer KMS audit ingestion using
   [`cmek_siem_ingest.md`](cmek_siem_ingest.md).

OmniSight operator checks:

- Store policy snapshot SHA-256, verify transcript, provider, key id,
  and principal in the evidence vault.
- Confirm Tier 1 -> Tier 2 rewrap succeeds.
- Confirm Tier 2 -> Tier 1 downgrade remains available.
- Confirm customer-side revoke produces graceful non-retryable 403
  within the 60-second detector contract.

Completion criteria:

- Customer can run one low-risk request after CMEK verify.
- Customer SIEM sees Encrypt, Decrypt, and key metadata read events.
- Disabling the key blocks new requests; restoring the same key
  recovers according to [`cmek_revoke_recovery.md`](cmek_revoke_recovery.md).

## 4. Tier 3 BYOG Proxy Onboarding

Customer prerequisites:

- Customer VPC, Kubernetes, VM, or container runtime for
  `omnisight-proxy`.
- Provider keys stored in `local_file`, customer KMS, or Vault.
- mTLS CA / client certificate material and signed-nonce key reference.
- Customer audit-log destination for full prompt/response records.

Customer steps:

1. Receive the canonical `omnisight-proxy` image tag and digest.
2. Mirror the image to the customer registry when required and preserve
   digest evidence.
3. Configure provider key sources and customer audit-log output.
4. Configure mTLS trust, pinned client certificate, and signed-nonce
   validation.
5. Expose the proxy endpoint to OmniSight according to customer network
   policy.
6. Run the proxy health check until OmniSight reports `connected=true`
   and `stale=false`.
7. Import provider keys from the sealed Tier 2 export bundle if
   migrating from CMEK.
8. Approve OmniSight SaaS-side key purge and confirm sampled decrypt of
   old credential ids fails as expected.

OmniSight operator checks:

- Follow [`tier2_to_tier3_byog_proxy_upgrade.md`](tier2_to_tier3_byog_proxy_upgrade.md).
- Record image digest, mirror digest, mTLS fingerprints, signed-nonce
  key reference, and health check transcript.
- Run one proxied request per provider, including streaming when
  supported.
- Run the proxy-unreachable no-fallback smoke.

Completion criteria:

- Customer proxy audit log contains full prompt/response records.
- OmniSight audit metadata contains time, model, token count, tenant,
  and request identifiers but no prompt or response payload.
- Blocking the proxy returns a BYOG error and no direct provider egress
  occurs.

## 5. Escalation

| Symptom | Customer action | OmniSight action |
|---|---|---|
| Tier 1 key suspected leaked | Rotate provider key in provider console and OmniSight | Confirm old key no longer decrypts and spend anomaly alert closed |
| Tier 2 requests return `cmek_revoked` | Restore the same KMS / Vault key and policy | Follow `cmek_revoke_recovery.md` and wait for detector health |
| Tier 2 customer wants BYOG | Approve proxy deployment plan and key export | Follow `tier2_to_tier3_byog_proxy_upgrade.md` |
| Tier 3 proxy unavailable | Restore proxy, mTLS, signed nonce, or network route | Keep fail-closed; do not route direct provider fallback |

## 6. Per-tier customer handoff packet

Create one customer handoff packet per tenant tier. The packet is the
customer-facing counterpart to the operator packet and should avoid raw
secrets, private keys, prompt/response payloads, exploit details, and
internal-only evidence-vault paths.

| Packet section | Tier 1 envelope | Tier 2 CMEK | Tier 3 BYOG proxy |
|---|---|---|---|
| Customer summary | OmniSight stores provider keys using tenant-bound envelope encryption | Customer KMS / Vault key wraps the tenant DEK | Customer proxy owns provider keys and prompt/response audit payloads |
| Customer-owned assets | provider account keys and rotation owner | KMS key, policy, principal, SIEM destination, revoke owner | proxy runtime, provider-key source, mTLS CA, signed-nonce key, audit sink |
| Launch checklist | first invocation, key rotation, audit export | wizard verify, SIEM event match, disable/restore drill | proxy health, proxied request, unreachable fail-closed drill |
| Exit checklist | rotate/delete keys in OmniSight | restore same key for recovery or downgrade to Tier 1 | keep proxy reachable until fresh onboarding completes |
| Evidence returned to customer | launch timestamp, audit export, key-rotation confirmation | verify transcript, KMS audit event ids, revoke drill result | proxy image digest, mTLS fingerprint, no-fallback drill result |

Minimum customer-facing packet checklist:

1. State the selected tier and the customer's ownership boundary.
2. List the customer-owned assets and named owners.
3. Include the launch checklist result and the exit / recovery behavior.
4. Include sanitized evidence ids or hashes; never include raw secrets or
   internal evidence-vault paths.
5. Record customer approval, approval timestamp, and the OmniSight
   operator who closed onboarding.

## 7. Production status

This onboarding guide does not deploy runtime code.

**Production status:** dev-only
**Next gate:** deployed-active - attach completed per-tier customer
onboarding packets to the private security evidence vault for the first
production tenant using each active tier.
