# KS.DOD - Priority I Multi-Tenancy Readiness Checklist

> Status: active readiness gate
> Scope: repository and operator evidence required before Priority I
> multi-tenancy foundation starts.
> Ledger: [`upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)
> ADR: [`../security/ks-multi-tenant-secret-management.md`](../security/ks-multi-tenant-secret-management.md)

This checklist is the hand-off gate between KS Phase 1 and Priority I.
Priority I may start only after the security owner records a
`ready-to-start` row in the N10 "Priority I Readiness" table with
evidence references for every gate below.

The checklist does not implement Phase 2 CMEK, Phase 3 BYOG, or new
tenant features. It proves that the Tier 1 envelope foundation is ready
enough that Priority I can build multi-tenant product flows without
falling back to single-Fernet secret management.

## 1. Decision summary

| Decision | Policy |
|---|---|
| Launch decision | Priority I is blocked until every gate in section 3 is green |
| Secret baseline | Tier 1 envelope encryption is mandatory for every tenant secret path |
| Legacy fallback | Single-Fernet reads and rollback writes remain deprecated; do not re-enable them for Priority I |
| Production proof | CI-green is not enough; attach production image, env, live smoke, and 24h observation evidence |
| Evidence location | Full evidence lives in the private security evidence vault; git stores only fingerprints, test names, and ledger rows |
| Owner | Security owner signs off; Priority I owner consumes the signed-off gate |

## 2. Evidence packet

Store one evidence packet in the private security evidence vault before
sign-off. The packet must contain:

- KS.1 acceptance test transcript for:
  `backend/tests/test_ks113_envelope_security_integration.py`,
  `backend/tests/test_security_kms_adapters.py`,
  `backend/tests/test_security_envelope.py`,
  `backend/tests/test_decryption_audit.py`,
  `backend/tests/test_spend_anomaly.py`,
  `backend/tests/test_security_secret_filter.py`, and
  `backend/tests/test_backup_dlp_scan.py`.
- Four KMS adapter evidence rows: AWS KMS, GCP KMS, Vault Transit, and
  LocalFernet. Sandbox live rows may be skipped only when the ledger row
  says which `OMNISIGHT_TEST_*` secret is missing and who owns enabling
  it before production activation.
- Legacy Fernet deprecation evidence: AS Token Vault, tenant secrets,
  and bootstrap LLM provider secrets all write envelope JSON; legacy
  single-Fernet rows fail closed.
- Decryption audit evidence: every plaintext recovery path used by
  OAuth refresh, OAuth revoke, tenant secrets, and bootstrap/provider
  credentials emits `audit_log.action = "ks.decryption"` into the N10
  hash chain.
- Spend anomaly evidence: detector threshold, alert sink, Redis or
  production shared store configuration, and a 60-second alert smoke.
- Log / secret scan / backup DLP evidence: logger scrubber sample,
  required CI gitleaks or equivalent pre-commit job, and backup DLP
  fail-closed transcript.
- Production image evidence: backend image digest, requirements lock
  hash, and `docker run --rm <image> python3 -c "from backend.security import envelope, kms_adapters, token_vault"`.
- Runtime env evidence: `OMNISIGHT_KS_ENVELOPE_ENABLED` is not set to a
  rollback value, `OMNISIGHT_REDIS_URL` or approved shared store is set
  for spend anomaly state, and KMS provider env is wired for the target
  deployment.
- Multi-tenant isolation smoke evidence: two tenants can write, read,
  rotate, and delete their own secrets; cross-tenant reads return
  authorization failure and do not decrypt.
- Tenant isolation smoke canonical proof: two tenants can write, read, rotate, and delete their own secrets.
- Cross-tenant denial canonical proof: cross-tenant reads return authorization failure.
- Rollback evidence: CMEK and BYOG knobs may stay disabled, but
  disabling them must not disable Tier 1 envelope writes.
- 24h observation evidence: no legacy-Fernet writes, no unaudited
  decrypts, no scrubber/DLP bypasses, and no spend-anomaly store errors.
  Canonical gate phrase: no unaudited decrypts.

## 3. Start gates

All gates are required. A gate may not be waived by changing the
checkbox; if the business accepts risk, record `Disposition =
risk-accepted` in the N10 row and link the security-owner approval in
the private evidence vault.

| Gate | Required proof | Disposition |
|---|---|---|
| KS.1 local acceptance green | Test transcript covers envelope, KMS adapter contract, decrypt audit, spend anomaly, log scrubber, gitleaks, and backup DLP | `ready-to-start` only when green |
| Live KMS evidence | AWS / GCP / Vault / LocalFernet rows recorded, or missing sandbox secret explicitly assigned before activation | `ready-to-start` only when no unknown skip remains |
| Legacy Fernet deprecated | No writer can create a single-Fernet carrier; legacy reads fail closed | `ready-to-start` only when all three secret surfaces are covered |
| N10 decryption audit complete | Every plaintext recovery path emits `ks.decryption` and includes tenant/user/key/request metadata | `ready-to-start` only when source guard and runtime smoke are green |
| Spend anomaly shared state | Production uses Redis or approved shared store; 60-second alert smoke reaches sink | `ready-to-start` only when multi-worker state is shared |
| Log, CI gitleaks, backup DLP green | CI secret scanner hard gate and backup DLP fail-closed evidence attached | `ready-to-start` only when both fail closed |
| Production image ready | Image digest and import smoke attached for the exact deploy candidate | `ready-to-start` only for the target image digest |
| Env knobs wired | Envelope enabled, CMEK/BYOG knobs orthogonal, no Tier 1 rollback knob set | `ready-to-start` only when operator env is captured |
| Tenant isolation smoke | Cross-tenant secret access is denied without plaintext decrypt | `ready-to-start` only when staging smoke is green |
| 24h observation clean | Metrics / logs show no legacy writes, unaudited decrypts, scrubber failures, DLP failures, or anomaly store errors | `ready-to-start` only after observation window |

## 4. Operator sequence

1. Build the production backend image from the target commit.
2. Run the KS.1 acceptance test shard in CI or staging.
3. Attach live KMS evidence for AWS, GCP, Vault, and LocalFernet.
4. Start staging with the target production env and image digest.
5. Run the tenant isolation smoke for two real tenant ids.
6. Run OAuth refresh/revoke, tenant secret, and bootstrap/provider
   credential smokes and confirm `ks.decryption` audit row counts match
   plaintext recovery counts.
7. Trigger a 60-second spend anomaly alert using the staging alert sink.
8. Run log scrubber and backup DLP samples and confirm fail-closed
   behavior.
9. Observe 24 hours of staging or production-equivalent traffic.
10. Append one row to the N10 "Priority I Readiness" table.
11. Security owner signs off; Priority I owner may begin.

## 5. N10 ledger row template

Append one row to `## Priority I Readiness`:

```markdown
| 2026-05-06 | <commit-sha> | <image-digest> | <ks1-evidence-sha256> | <kms-evidence-sha256> | <tenant-smoke-sha256> | <24h-window> | ready-to-start | Security owner approved Priority I start |
```

Do not edit previous rows. If a row is wrong, add a correction row with
`correction -> <commit-sha/evidence-sha256>` in Notes.

## 6. Evidence checklist

- [ ] KS.1 acceptance shard green
- [ ] AWS KMS evidence attached
- [ ] GCP KMS evidence attached
- [ ] Vault Transit evidence attached
- [ ] LocalFernet evidence attached
- [ ] Legacy Fernet deprecation evidence attached
- [ ] N10 `ks.decryption` row-count evidence attached
- [ ] Spend anomaly 60-second alert evidence attached
- [ ] CI gitleaks hard gate evidence attached
- [ ] Backup DLP fail-closed evidence attached
- [ ] Production image import smoke attached
- [ ] Runtime env knob snapshot attached
- [ ] Multi-tenant isolation smoke attached
- [ ] Rollback/no-fallback evidence attached
- [ ] 24h observation window clean
- [ ] N10 `Priority I Readiness` row appended
- [ ] Security owner sign-off stored in the private evidence vault

## 7. Production status

This checklist does not deploy runtime code. Production readiness is
operational: Priority I is ready to start only when the N10
`Priority I Readiness` row has `Disposition = ready-to-start` or an
explicit security-owner `risk-accepted` disposition.
