# COMPLIANCE-AUDIT - BP.D.7

Auxiliary audit skill pack for Phase D compliance matrices.

This skill pack is advisory only. AI-assisted output MUST be reviewed by
a human certified engineer. It does not certify a product, replace legal
review, replace accredited lab work, or waive third-party signoff.

## Scope

Use these skills when an Auditor Guild task needs a structured evidence
check for the Phase D compliance matrices:

- Medical: IEC 62304, ISO 13485, HIPAA
- Automotive: ISO 26262, MISRA C, AUTOSAR
- Industrial: IEC 61508, SIL claim evidence
- Military / aerospace: DO-178C, MIL-STD-882E

Every result must include:

```json
{
  "audit_type": "advisory",
  "requires_human_signoff": true
}
```

Module-global state audit: this is markdown and static YAML only. Each
worker reads the same committed files; no singleton, cache, env knob, DB
writer, or read-after-write timing path is introduced.

## Auxiliary Audit Skills

| Skill | Matrix | Purpose |
|---|---|---|
| `audit_iec62304_traceability_auxiliary` | medical | Check whether requirements, BDD cases, code changes, tests, and release evidence form an IEC 62304 traceability chain. |
| `scan_phi_data_leakage_auxiliary` | medical | Check logs, reports, screenshots, fixtures, and exports for possible PHI exposure before human HIPAA review. |
| `audit_iso13485_design_controls_auxiliary` | medical | Check design-input, design-output, verification, validation, and change-control evidence for ISO 13485 design-control review. |
| `scan_misra_c_strict_auxiliary` | automotive | Check C/C++ diffs for MISRA C/C++ rule-risk patterns and require tool-backed findings before any claim. |
| `verify_asil_d_redundancy_auxiliary` | automotive | Check whether ASIL-D critical paths list redundancy, independence, diagnostic coverage, and fault reaction evidence. |
| `audit_autosar_interface_contract_auxiliary` | automotive | Check AUTOSAR interface descriptions, RTE boundaries, signal contracts, and generated artefact drift. |
| `analyze_state_machine_deadlocks_auxiliary` | industrial | Check IEC 61508 state machines for terminal traps, unreachable transitions, unsafe defaults, and missing recovery paths. |
| `verify_watchdog_pet_timing_auxiliary` | industrial | Check watchdog servicing windows, fault injection evidence, and reset escalation timing for industrial controllers. |
| `audit_sil_claim_evidence_auxiliary` | industrial | Check that SIL claims cite hazard analysis, target SIL, diagnostic coverage, validation evidence, and third-party review status. |
| `verify_mcdc_100_percent_auxiliary` | military | Check MC/DC coverage reports for 100 percent decision and condition evidence before DO-178C human review. |
| `run_formal_verification_proof_auxiliary` | military | Check formal-method proof artefacts, assumptions, solver logs, and proof-to-requirement mapping. |
| `audit_mil_std_882e_hazard_log_auxiliary` | military | Check hazard severity, probability, mitigation, residual risk acceptance, and traceability for MIL-STD-882E review. |

## Output Rules

For every skill invocation, produce a structured advisory report:

- `skill_name`: one of the names above
- `audit_type`: always `advisory`
- `requires_human_signoff`: always `true`
- `standard`: the named standard being checked
- `evidence_inputs`: files, reports, tool outputs, and requirement IDs examined
- `claims`: advisory observations only, each with source evidence
- `gaps`: missing evidence or unanswered questions
- `human_review_note`: repeat the auxiliary disclaimer

Never output `certified`, `approved`, `compliant`, or `safe` as a final
decision. Use `ready_for_human_review` only when evidence is complete
enough for the responsible certified reviewer to decide.

## Trigger Condition

Load this skill when a task mentions Phase D compliance audit skills,
IEC 62304, ISO 13485, HIPAA, ISO 26262, MISRA, AUTOSAR, IEC 61508, SIL,
DO-178C, MIL-STD-882E, MC/DC, formal verification, watchdog timing, or
certified human signoff.
