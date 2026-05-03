# KS.4.7 - SOC 2 Type II Readiness Checklist

> Status: readiness checklist
> Scope: SOC 2 Type II control mapping, evidence collection, third-party
> auditor evaluation, and N10 ledger evidence.
> Ledger: [`upgrade_rollback_ledger.md`](upgrade_rollback_ledger.md)

This checklist prepares OmniSight for a SOC 2 Type II examination. It
does not replace the auditor's judgement. The security owner uses it to
map controls to the AICPA Trust Services Criteria, collect operating
evidence over the observation window, and select an independent CPA firm
before the Type II period starts.

## 1. Decision summary

Default recommendation: prepare for Security, Availability, and
Confidentiality first; add Processing Integrity and Privacy only after
the auditor confirms the system description and customer commitments
require them.

| Decision | Policy |
|---|---|
| Report type | SOC 2 Type II; Type I may be used only as an earlier readiness milestone |
| Criteria baseline | AICPA 2017 Trust Services Criteria with revised points of focus - 2022, plus 2018 Description Criteria revised implementation guidance - 2022 |
| Observation window | Minimum 3 months; prefer 6 months for the first customer-facing report unless sales timing requires a shorter window |
| Control owner | One named owner per control; shared ownership is allowed only with one accountable owner |
| Evidence source | Private security evidence vault or selected GRC platform; git stores only checklist, mappings, fingerprints, and summary rows |
| Auditor | Independent CPA firm selected before the observation window; readiness automation vendor must not be treated as the auditor |

SOC 2 Type II tests operating effectiveness over time. Do not begin the
observation window until control owners, evidence cadence, exception
tracking, and auditor access model are all agreed.

## 2. Control mapping

Maintain a living control matrix in the private evidence vault. The
matrix must map each in-scope Trust Services Criteria item to one or
more OmniSight controls, evidence sources, owners, test frequency, and
exception handling.

| TSC area | Baseline mapping | Existing OmniSight evidence |
|---|---|---|
| Security - common criteria | Access control, change management, vulnerability management, incident response, risk assessment, monitoring, vendor management | Auth baseline, CI security scanners, dependency governance, quarterly pentest SOP, incident response runbook, audit ledger |
| Availability | Backup / restore, disaster recovery, SLO monitoring, deploy rollback, capacity and incident procedures | Backup DLP pipeline, DR runbooks, observability runbook, blue-green gate, rollback ledger |
| Confidentiality | Secret management, encryption, DLP, data handling, backup encryption, restricted evidence vault | KS envelope encryption, secret scrubber, DLP backup scan, key-management docs, no-secret evidence policy |
| Processing Integrity | Input validation, workflow integrity, idempotency, auditability, error handling | API contract tests, orchestrator audit trails, retry/idempotency docs, deployment smoke gates |
| Privacy | Customer data handling, retention, deletion, DSAR, notification, subprocessor inventory | GDPR / DSAR follow-up, incident notification runbook, vendor evidence collection |

Required matrix columns:

- `TSC ID`
- `Point of focus`
- `OmniSight control ID`
- `Control description`
- `Control owner`
- `Evidence source`
- `Evidence cadence`
- `Auditor test method`
- `Exception owner`
- `Last evidence collected`
- `N10 / ticket reference`

Use stable control IDs such as `SOC2-SEC-001`, `SOC2-AVL-001`, and
`SOC2-CON-001`. If a control maps to multiple criteria, keep one owner
and one evidence source of record. Avoid screenshot-only evidence when a
log export, policy acceptance report, ticket export, or read-only
integration can prove the same control.

## 3. Evidence collection

Evidence must prove both design and operation. Collect evidence at the
same cadence the auditor will test, and track exceptions immediately.

| Evidence family | Minimum evidence | Cadence |
|---|---|---|
| Governance | Security policies, risk register, control owner roster, board / leadership security review notes | Quarterly or on material change |
| Access | User access review, privileged access review, MFA enforcement, joiner / mover / leaver tickets | Monthly for privileged access; quarterly for standard access |
| Change management | PR approvals, CI results, deploy approvals, rollback records, emergency change approvals | Per change; sample monthly |
| Vulnerability management | Dependency scans, container scans, SAST / DAST / secret scan results, remediation tickets | Continuous scan; monthly evidence export |
| Incident response | Tabletop exercise, incident tickets, postmortems, alert routing tests, rotation drill evidence | Quarterly tabletop; per incident |
| Backup / recovery | Encrypted backup proof, DLP scan results, restore test, off-site immutable backup settings | Monthly backup evidence; quarterly restore test |
| Availability | SLO dashboards, on-call schedule, alert history, blue-green deploy smoke, DR drill | Monthly dashboard export; per deploy / drill |
| Confidentiality | Encryption configuration, key rotation evidence, DLP exceptions, evidence vault access review | Monthly control export; per rotation |
| Vendor management | Vendor inventory, risk reviews, SOC reports, DPA / subprocessor records | Quarterly or before onboarding |
| People controls | Security training, policy acceptance, background check attestation where applicable | On hire and annually |

Evidence handling rules:

1. Store full artefacts in the private security evidence vault or the GRC
   platform. Do not commit screenshots, customer data, raw logs,
   secrets, export files, auditor requests, or platform dumps.
2. Record SHA-256 fingerprints for high-value exports when provenance
   matters.
3. Keep each evidence item tied to a control ID, owner, date range,
   source system, and retention date.
4. Open an exception ticket within 2 business days when evidence is
   missing, late, manually overridden, or shows a failed control.
5. Close exceptions only with remediation evidence or explicit
   risk-acceptance by the security owner.
6. Freeze evidence deletion during the observation window and until the
   final report is issued.

## 4. GRC platform evaluation

A GRC platform is optional but recommended for the first Type II report
because it reduces manual evidence chasing and gives auditors a bounded
review workspace. Evaluate at least Vanta, Drata, and Secureframe before
procurement.

| Dimension | Vanta | Drata | Secureframe | OmniSight requirement |
|---|---|---|---|---|
| Evidence collection | Read-only integrations, automatic evidence pulls, control mapping, auditor portal | Automated evidence collection, continuous control monitoring, auditor view with scoped access | Automated evidence collection, cloud scanning, policy publication, vendor evidence support | Must support read-only cloud, identity, code, ticketing, and device evidence |
| Control mapping | Cross-framework mapping and SOC 2 workflow support | Pre-mapped controls, custom controls, audit workspace | SOC 2 readiness workflow and policy library | Must export a control matrix and evidence index |
| Auditor access | Vetted independent auditor introductions and dedicated auditor portal | Auditor-only view and partner auditor introductions | Audit workflow and readiness assessment support | Auditor access must be least-privilege and time-bounded |
| Data exposure | Platform receives compliance evidence and integration metadata | Platform receives compliance evidence and control telemetry | Platform receives compliance evidence and vendor / cloud metadata | Security owner must approve connected systems and data classes before enabling integrations |
| Procurement fit | Quote-based SOC 2 package | Quote-based SOC 2 package | Quote-based SOC 2 package | Compare platform fee, auditor fee, support model, integrations, export rights, and termination data return |

The platform is not the auditor. The final attestation must come from an
independent CPA firm, and auditor independence must be confirmed in the
engagement letter.

## 5. Third-party auditor evaluation

Select the auditor before the Type II observation window starts. Use the
same process even if the GRC platform introduces candidate firms.

| Criterion | Requirement |
|---|---|
| Independence | CPA firm confirms independence, no prohibited management responsibility, and no conflict with the GRC platform arrangement |
| SaaS / AI experience | Prior SOC 2 Type II work for SaaS, API, multi-tenant, cloud, and AI / agentic workflow companies |
| Scope alignment | Written agreement on in-scope services, Trust Services Categories, carve-outs, subservice organizations, and observation period |
| Evidence workflow | Auditor accepts the chosen evidence repository, naming convention, sampling model, and request SLA |
| Timeline | Readiness review, gap remediation window, observation start, interim testing, final fieldwork, draft report, and final report dates documented |
| Commercials | Fixed fees, retest / rework fees, rush fees, report reissue fees, and platform-access fees known before signing |
| Report quality | Will provide management assertion requirements, system description guidance, exception wording review, and bridge letter template when needed |

Minimum evaluation steps:

1. Shortlist at least two independent CPA firms.
2. Send the same system description, target criteria, observation window,
   and expected evidence repository to every candidate.
3. Ask each candidate to identify likely scope gaps before contracting.
4. Confirm the auditor will not design or operate OmniSight controls.
5. Store the engagement letter, auditor independence confirmation,
   readiness notes, and final scorecard in the private evidence vault.
6. Append one row to the N10 "SOC 2 Readiness" table with provider,
   auditor, observation window, criteria, readiness disposition, and
   evidence index fingerprint.

## 6. Readiness gates

Do not start the Type II observation window until all gates are true:

1. In-scope product / service boundaries are documented.
2. Trust Services Categories are selected and approved by security,
   product, legal, and sales owner.
3. Control mapping covers all in-scope criteria.
4. Evidence owners and collection cadence are assigned.
5. Evidence vault or GRC platform access model is approved.
6. Exception tracker exists and has severity / due-date rules.
7. Incident response runbook and quarterly pentest cadence are active.
8. Backup restore test and access review evidence exist for the prior
   month.
9. Auditor shortlist and independence review are complete.
10. N10 "SOC 2 Readiness" row exists with `Disposition = planned` or
    `Disposition = ready-for-observation`.

If any gate fails, do not silently narrow the report. Record the gap,
owner, due date, and risk decision in the private evidence vault.

## 7. N10 ledger row template

Append one row to `## SOC 2 Readiness` when the program is planned,
ready for observation, observation starts, observation ends, draft report
is received, final report is issued, delayed, or materially rescoped:

```markdown
| 2026-Q3 | Vanta | <CPA firm> | Security/Availability/Confidentiality | 2026-07-01 -> 2026-12-31 | <evidence-index-sha256> | ready-for-observation | Type II window approved |
```

Do not edit previous rows. If a row is wrong, add a correction row with
`correction -> <quarter/auditor/evidence-index-sha256>` in Notes.

## 8. Evidence checklist

- [ ] Trust Services Categories selected
- [ ] System description draft created
- [ ] Control matrix completed with owner, cadence, evidence source, and
      auditor test method
- [ ] Evidence vault or GRC platform selected
- [ ] GRC platform comparison stored in evidence vault
- [ ] Auditor shortlist completed with at least two CPA firms
- [ ] Auditor independence confirmation stored in evidence vault
- [ ] Engagement letter approved
- [ ] Access review, vulnerability management, change management,
      backup, incident response, and vendor management evidence collected
- [ ] Exception tracker active
- [ ] Observation window start date approved
- [ ] N10 `SOC 2 Readiness` row appended

## 9. References

- AICPA SOC 2 Trust Services Criteria resources:
  <https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2>
- Vanta SOC 2 product page:
  <https://www.vanta.com/products/soc-2>
- Drata SOC 2 product page:
  <https://drata.com/product/soc-2>
- Secureframe SOC 2 product page:
  <https://secureframe.com/soc2>

## 10. Production status

This checklist does not deploy runtime code. Production readiness is
operational: SOC 2 Type II observation may start only after the control
matrix, evidence collection cadence, exception tracker, and independent
auditor engagement are ready.
