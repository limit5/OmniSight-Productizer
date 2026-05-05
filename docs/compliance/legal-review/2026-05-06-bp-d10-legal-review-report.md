---
audience: legal + architect + auditor
task_id: BP.D.10
status: archived-for-third-party-review
date: 2026-05-06
scope: Phase D auxiliary compliance matrices
related:
  - backend/compliance_matrix/medical.py
  - backend/compliance_matrix/automotive.py
  - backend/compliance_matrix/industrial.py
  - backend/compliance_matrix/military.py
  - backend/routers/compliance_matrix.py
  - configs/skills/compliance-audit/SKILL.md
  - docs/design/sandbox-tier-audit.md
  - docs/security/r12-gvisor-cost-weight-only.md
---

# BP.D.10 Third-Party Legal Review Report Archive

> **Archive scope:** This file records the non-secret legal-review packet
> for the Phase D auxiliary compliance-matrix work. It does **not** claim
> that OmniSight is certified under IEC 62304, ISO 13485, HIPAA, ISO
> 26262, MISRA C, AUTOSAR, IEC 61508, DO-178C, or MIL-STD-882E. It is a
> synchronized evidence archive for third-party review.

---

## 1. Review Boundary

The reviewed implementation surface is BP.D.1-BP.D.9:

| Item | Surface | Legal-review question |
|---|---|---|
| BP.D.1 | `backend/compliance_matrix/medical.py` | Does the medical matrix avoid certification language and keep IEC 62304 / ISO 13485 / HIPAA output advisory? |
| BP.D.2 | `backend/compliance_matrix/automotive.py` | Does the automotive matrix avoid ASIL, MISRA, and AUTOSAR conformance claims? |
| BP.D.3 | `backend/compliance_matrix/industrial.py` | Does the industrial matrix avoid assigning SIL or IEC 61508 certification status? |
| BP.D.4 | `backend/compliance_matrix/military.py` | Does the military matrix avoid DO-178C / MIL-STD-882E approval language? |
| BP.D.5 | Module headers and `_auxiliary_` functions | Are all claim surfaces explicitly auxiliary and human-review gated? |
| BP.D.6 | `backend/routers/compliance_matrix.py` | Does the API schema force `audit_type="advisory"` and `requires_human_signoff=true`? |
| BP.D.7 | `configs/skills/compliance-audit/` | Do all audit skills end in `_auxiliary` and output advisory reports only? |
| BP.D.8 | `backend/pep_gateway.py` + `docs/design/pep-gateway-tier-policy.md` | Does `guild_id` policy narrowing prevent inadmissible Guild x Tier claim inheritance? |
| BP.D.9 | `backend/tests/test_compliance_matrix.py` | Do tests pin the advisory envelope and claim sources? |

The synchronized file list is maintained in
`docs/compliance/legal-review/code-sync-manifest.md`.

## 2. Third-Party Review Disposition

| Field | Value |
|---|---|
| Review packet status | Archived for third-party legal review |
| Public repository artefact | This report plus `code-sync-manifest.md` |
| Private evidence vault artefact | External reviewer identity, contract metadata, signed report, and any privileged legal comments |
| Repository legal conclusion | Advisory archive only; no privileged legal opinion stored in source |
| Required downstream gate | Human operator must attach the signed external report in the private evidence vault before using Phase D outputs in customer-facing compliance claims |

No reviewer identity, contract number, privileged legal advice, or
private evidence-vault URL is stored in this repository.

## 3. Archived Findings

| Finding | Disposition |
|---|---|
| Phase D language is auxiliary, not certification language. | Accepted for archive: module headers, API schema, and skill output rules repeat the human-review disclaimer. |
| API consumers receive the advisory envelope at the schema boundary. | Accepted for archive: `ComplianceMatrixResponse` and `ComplianceMatrixListResponse` pin `audit_type="advisory"` and `requires_human_signoff=true`. |
| The compliance matrices cite engineering evidence from `sandbox-tier-audit.md`, not independent legal authority. | Accepted for archive: claim sources point to the audit document, and claim summaries avoid final approval language. |
| R12 gVisor risk must remain visible to legal reviewers. | Accepted for archive: the R12 document and sandbox-tier audit disclaimer are part of the synchronized packet. |
| Guild x Tier policy inheritance must not expand claims beyond admitted tiers. | Accepted for archive: BP.D.8 `guild_id` handling is included in the synchronized packet. |

## 4. Non-Claims

The Phase D archive must not be read as any of the following:

1. OmniSight is certified for any named medical, automotive, industrial,
   military, avionics, privacy, or quality-management standard.
2. AI output can replace a human certified engineer, accredited auditor,
   third-party legal reviewer, or lab certification process.
3. Sandbox Tier admission alone discharges a regulatory clause.
4. gVisor isolation is active in production. R12 remains open until the
   Phase U runtime adoption work lands.
5. A private legal opinion is stored in this repository.

## 5. Acceptance Checklist

- [x] Legal-review archive directory exists at `docs/compliance/legal-review/`.
- [x] Review report references BP.D.1-BP.D.9 implementation surfaces.
- [x] Code sync manifest lists code, tests, skills, and source documents.
- [x] Advisory envelope is recorded: `audit_type="advisory"` and
      `requires_human_signoff=true`.
- [x] Report explicitly avoids certification / approval claims.
- [x] Private legal-review artefacts are excluded from source.

## 6. Change Control

Any future change that alters a compliance claim, standard list, API
envelope, skill output rule, or Guild x Tier policy inheritance must
update:

1. the affected code or source document;
2. `docs/compliance/legal-review/code-sync-manifest.md`;
3. this report, if the legal-review boundary or disposition changes;
4. the relevant drift guard test.

## 7. SOP Notes

Module-global state audit: this is static markdown. Every worker reads
the same committed file contents; no singleton, cache, env knob, DB
writer, or read-after-write timing path is introduced.

Read-after-write audit: this archive changes no runtime write path and
does not alter test timing assumptions.
