# KS.4.8 - GDPR / DSAR Alignment SOP

> Status: active SOP
> Scope: tenant data deletion, DEK purge evidence, audit metadata
> redaction, DSAR export workflow, and N10 ledger evidence.
> Ledger: [`upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)

This SOP aligns tenant teardown and data-subject requests with GDPR
Article 15, Article 17, and Article 20 without weakening OmniSight's
forensic audit posture. Runtime implementation remains owned by the
tenant deletion, envelope encryption, audit, and privacy endpoint
modules. This document defines the operational contract those paths must
meet before a deletion or export is treated as complete.

## 1. Decision summary

| Decision | Policy |
|---|---|
| Tenant deletion target | Purge tenant business data, filesystem data, and tenant DEK material; retain only minimal audit metadata needed for proof |
| DEK handling | Destroy or irreversibly delete wrapped DEKs so remaining ciphertext is cryptographically unrecoverable |
| Audit retention | Keep hash-chain metadata, actor, action, timestamps, entity ids, and request ids; delete or redact raw payloads, freeform notes, customer content, and direct PII |
| DSAR export | Produce a portable JSON export for the verified data subject, store the raw export outside git, and record only export hash and counts in the ledger |
| Evidence location | Private security evidence vault; git stores SOP, fingerprints, summary rows, and tests only |
| Completion gate | A request is complete only after deletion/export evidence is fingerprinted and the N10 DSAR evidence row is appended |

Hard deletion and redaction must be deliberate. If legal hold, fraud
review, or incident response freezes erasure, record the hold in the
private evidence vault and append a delayed N10 row instead of silently
closing the request.

## 2. Tenant deletion purge contract

Tenant deletion is complete only when all of these phases finish:

1. Confirm the requester is authorized for tenant deletion and the
   tenant id is not protected.
2. Capture a pre-delete inventory: row counts by table, filesystem byte
   estimate, active DEK references, and export/deletion request id.
3. Delete tenant-scoped business rows and filesystem roots.
4. Purge tenant DEKs by deleting wrapped DEK rows or destroying the KMS
   key version where the provider supports tenant-scoped key material.
5. Verify no decryptable ciphertext remains by attempting a sampled
   decrypt with the purged DEK references and expecting failure.
6. Redact retained audit payload fields: keep hash-chain continuity and
   metadata, but remove raw customer content, freeform notes, IP
   addresses, email addresses, and object payloads.
7. Append the deletion evidence fingerprint to the N10 DSAR evidence
   table.

The DEK purge phase is mandatory. Deleting data rows while leaving wrapped DEKs intact is not a completed tenant deletion, because backups or orphaned ciphertext could still become readable if the key material survives.

## 3. Audit metadata retention

Audit rows are split into retained metadata and redactable raw material.
Retained metadata proves that the deletion happened; redactable material
prevents the audit trail from becoming a secondary personal-data store.

| Field family | Retention rule |
|---|---|
| Hash-chain fields | Retain `hash`, `prev_hash`, row id, tenant id, action, actor, timestamp, entity kind, entity id, and request id |
| Completion metadata | Retain category counts, byte counts, DEK count, export hash, evidence hash, status, and operator id |
| Raw payloads | Delete or replace with a redaction marker for `before_json`, `after_json`, request body, response body, customer content, freeform notes, and stack traces |
| Direct PII | Delete or hash email, name, IP address, user-agent string, OAuth subject, and external account identifiers unless a legal hold applies |
| Secrets and keys | Never retain raw secrets, plaintext tokens, plaintext DEKs, wrapped DEK material after purge, or KMS response bodies |

Use deterministic SHA-256 hashes only for correlation and evidence
continuity. Do not hash low-entropy values alone; include a request id
or tenant id context so the hash is not a reusable lookup table.

## 4. DSAR export workflow

The DSAR export flow covers Article 15 access and Article 20
portability. It does not grant access to another tenant's records,
internal security evidence, trade secrets, model prompts owned by
OmniSight, or third-party data that would disclose another data subject.

1. Verify the requester's identity and tenant membership.
2. Create a `dsar_requests` row with request type `access` or
   `portability`, due date, and request metadata.
3. Build a whitelist-shaped export from account-owned and tenant-scoped
   data the requester is allowed to receive.
4. Exclude raw secrets, plaintext tokens, DEK material, internal audit
   hash-chain internals not needed by the data subject, and other users'
   personal data.
5. Store the raw export in the private evidence/export vault with
   time-bounded access; do not commit it to git or put it in the N10
   ledger.
6. Compute SHA-256 over the stored export and record category counts,
   byte size, schema version, request id, and completion time.
7. Deliver the export through the approved secure channel and record the
   delivery metadata in the `dsar_requests` result.
8. Append one N10 DSAR evidence row with only metadata and hashes.

The export schema should remain stable and explicit. Adding a new data
category requires updating the whitelist, the exclusion list, and the
contract tests that check the evidence shape.

## 5. DSAR erasure workflow

The erasure flow covers user-level GDPR Article 17 requests and tenant
termination deletion requests.

1. Verify identity, authority, and whether legal hold or incident hold
   applies.
2. Create a `dsar_requests` row with request type `erasure` and a due
   date.
3. Revoke sessions, API keys, OAuth tokens, and external account links.
4. Delete mutable user-owned rows where no audit or accounting retention
   obligation applies.
5. Redact retained profile rows by replacing direct PII with redaction
   hashes and disabling authentication.
6. For tenant-wide erasure, run the tenant deletion purge contract,
   including DEK purge and filesystem purge.
7. Redact audit raw payloads while retaining hash-chain metadata.
8. Append a N10 DSAR evidence row with request id, subject scope,
   evidence hash, DEK purge count, audit redaction count, and
   disposition.

If a subprocessor or connected OAuth provider must delete data, record
the outbound request id and provider receipt hash in the private evidence
vault. The N10 ledger stores only provider name, receipt hash, and final
disposition.

## 6. N10 ledger row template

Append one row to `## DSAR Evidence` when a DSAR request is received,
exported, erased, delayed, legally held, completed, or corrected:

```markdown
| 2026-05-03T12:00:00Z | dsar-erasure-123 | erasure | tenant:t-acme | <export-sha256-or-none> | <evidence-sha256> | 17 | 42 | completed | tenant deletion purge completed; raw audit payload redacted |
```

Do not edit previous rows. If a row is wrong, add a correction row with
`correction -> <request-id/evidence-sha256>` in Notes.

## 7. Evidence checklist

- [ ] Request identity / authority verified
- [ ] `dsar_requests` row exists with SLA due date
- [ ] Export whitelist and exclusion list applied
- [ ] Raw export stored outside git with SHA-256 fingerprint
- [ ] Tenant data rows and filesystem roots purged when tenant scope
      applies
- [ ] Wrapped DEKs purged or KMS key material destroyed
- [ ] Sampled decrypt after DEK purge fails as expected
- [ ] Audit raw payloads redacted while hash-chain metadata is retained
- [ ] Subprocessor deletion receipts stored in evidence vault when
      applicable
- [ ] N10 `DSAR Evidence` row appended

## 8. Production status

This SOP does not deploy runtime code. Production readiness is
operational: a DSAR or tenant deletion is complete only after the
runtime workflow has purged raw data and DEK material, retained only
permitted audit metadata, delivered or destroyed exports as applicable,
and appended a N10 DSAR evidence row.
