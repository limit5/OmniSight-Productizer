# KS Cross-Cutting Evidence Index

> Status: evidence index
> Scope: KS Definition of Done cross-cutting gates: R46-R50 mitigation,
> incident response runbook, first external pentest evidence gate, and
> SOC 2 Type II readiness checklist.
> Source ADR:
> [`ks-multi-tenant-secret-management.md`](../security/ks-multi-tenant-secret-management.md)

This index consolidates the KS cross-cutting evidence that is not owned
by a single secret-management tier. It does not replace the source
documents or runtime tests. It points reviewers to the artifacts that
prove each mitigation has a code, test, runbook, or ledger guard.

## 1. Scope boundary

This index covers the `Cross-cutting` KS Definition of Done row only:

- R46-R50 mitigation evidence
- Incident response runbook ship evidence
- First external pentest gate evidence
- SOC 2 Type II readiness checklist evidence

It does not cover Phase 1 envelope. It does not cover Phase 2 CMEK. It
does not cover Phase 3 BYOG proxy GA. It does not cover the final all-KS
three-knob rollout except where those artifacts are the mitigation
source for a cross-cutting risk.

Current status is `dev-only`. The source artifacts and drift guards are
in the repository; operational gates such as vendor pentest execution,
finding remediation, SOC 2 auditor engagement, and production evidence
vault uploads remain `deployed-active` / `deployed-observed` work.

## 2. R46-R50 mitigation matrix

| Risk | Required mitigation | Landing evidence | Drift guard |
|---|---|---|---|
| R46 Master KEK compromise | KEK quarterly rotation; IAM least-privilege; KMS admin dual-control; KMS audit log to N10. | `backend/security/token_vault.py` derives the quarterly master-KEK epoch from UTC dates; `backend/security/envelope.py` enforces per-tenant DEK + KMS-wrapped KEK references; `backend/security/cmek_wizard.py` emits least-privilege AWS/GCP/Vault policies scoped to tenant encryption context or key resource; `docs/ops/cmek_siem_ingest.md` pins provider KMS audit events; `docs/ops/upgrade_rollback_ledger.md` includes the N10 `KS KMS Control Reviews` table for two-person admin approval, policy snapshot fingerprints, and KMS audit export fingerprints; `docs/ops/priority_i_multi_tenancy_readiness.md` requires live KMS evidence before Priority I starts. | `backend/tests/test_token_vault.py`, `backend/tests/test_security_envelope.py`, `backend/tests/test_cmek_wizard.py`, `backend/tests/test_cmek_siem.py`, `backend/tests/test_ks_priority_i_readiness.py` |
| R47 KMS vendor lock-in | Multi-adapter abstraction; cloud-neutral Vault fallback; tenant rewrap path between providers. | AWS KMS, Google Cloud KMS, HashiCorp Vault Transit, and LocalFernet adapters share one `KMSAdapter` contract; Tier 1 <-> Tier 2 rewrap helpers accept source and target adapters so tenants can move across supported providers. | `backend/tests/test_security_kms_adapters.py`, `backend/tests/test_cmek_upgrade.py`, `backend/tests/test_cmek_phase2_regression.py` |
| R48 CMEK revoke degrades poorly | In-flight work may finish; new requests detect revoke within 60 seconds and return friendly non-retryable 403; restore path is documented. | `backend/security/cmek_revoke_detector.py` keeps the 60 second detection contract; `backend/security/cmek_graceful_degrade.py` turns revoked snapshots into friendly non-retryable `403` payloads; `docs/ops/cmek_revoke_recovery.md` documents restore and retry boundaries. | `backend/tests/test_cmek_revoke_detector.py`, `backend/tests/test_cmek_graceful_degrade.py`, `backend/tests/test_cmek_phase2_regression.py` |
| R49 BYOG proxy MITM | mTLS; certificate pinning; signed nonce; handshake fail closes without fallback. | The proxy auth package verifies mTLS, pinned client certificates, expiry, signed nonce freshness, and replay rejection; SaaS-side BYOG client fails closed and does not fall back to direct provider egress. | `omnisight-proxy/internal/auth/auth_test.go`, `backend/tests/test_byog_proxy_fail_fast.py`, `backend/tests/test_ks_phase3_byog_proxy_ga.py` |
| R50 Audit log integrity tampering | N10 hash-chain integration; append-only evidence; off-site immutable backup using S3 Object Lock / Glacier. | `backend/audit.py` maintains per-tenant hash chains under `pg_advisory_xact_lock`; `ks.decryption` events land in that chain; N10 operational ledgers are append-only and store only fingerprints / metadata for security evidence; `scripts/backup_prod_db.sh` uploads encrypted backups with Object Lock compliance retention and `GLACIER_IR` storage after mandatory DLP scan. | `backend/tests/test_audit.py`, `backend/tests/test_decryption_audit.py`, `backend/tests/test_backup_dlp_scan.py`, `backend/tests/test_ks44_quarterly_pentest_sop.py`, `backend/tests/test_ks47_soc2_type2_readiness_checklist.py` |

## 3. Incident response runbook

The incident response runbook is shipped at
[`docs/security/incident-response-runbook.md`](../security/incident-response-runbook.md).
It covers the first 24 hours: detect and declare, contain, rotate and
verify, customer notification decision, forensics and recovery, and a
blameless postmortem within 5 business days.

The drift guard is `backend/tests/test_ks46_incident_response_runbook.py`.
Production readiness is operational: the runbook becomes active when the
on-call roster has incident commander, engineering, forensics,
communications, and scribe coverage, and the private security evidence
vault is reachable during a tabletop drill.

## 4. External pentest gate

The external pentest cadence is shipped at
[`docs/ops/quarterly_pentest_sop.md`](quarterly_pentest_sop.md). The N10
ledger has a `Pentest Reports` table in
[`docs/ops/upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md).

Repository evidence deliberately does not claim that the first vendor
assessment has already run. The first external pentest is complete only
when all of these are true:

1. A signed vendor MSA / SOW and rules of engagement are stored in the
   private security evidence vault.
2. The vendor executes one application and infrastructure assessment
   against production-equivalent staging.
3. The final report is stored in the private security evidence vault and
   its SHA-256 fingerprint is recorded in the N10 `Pentest Reports`
   table.
4. Critical / high findings are fixed and retested, or explicitly
   risk-accepted by the security owner.
5. The ledger row disposition is `closed` or `risk-accepted`.

The drift guard is `backend/tests/test_ks44_quarterly_pentest_sop.py`.

## 5. SOC 2 Type II readiness

The readiness checklist is shipped at
[`docs/ops/soc2_type2_readiness_checklist.md`](soc2_type2_readiness_checklist.md).
It covers AICPA Trust Services Criteria mapping, evidence collection,
exception handling, Vanta / Drata / Secureframe evaluation, independent
CPA auditor selection, and the N10 `SOC 2 Readiness` evidence row.

The drift guard is
`backend/tests/test_ks47_soc2_type2_readiness_checklist.py`.
Production readiness is operational: the Type II observation window
cannot start until the control matrix, evidence cadence, exception
tracker, evidence vault or GRC platform, and independent auditor
engagement are ready.

## 6. Production status

This index does not deploy runtime code.

**Production status:** dev-only
**Next gate:** deployed-active - security owner executes the first
external pentest, stores the report in the private security evidence
vault, fixes/retests or risk-accepts findings, appends a closed N10
`Pentest Reports` row, runs one incident response tabletop using the
runbook, and completes SOC 2 readiness owner/auditor/evidence decisions.
