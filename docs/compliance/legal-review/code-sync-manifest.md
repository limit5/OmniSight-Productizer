# BP.D.10 Code Sync Manifest

This manifest binds the Phase D legal-review archive to the code and
documentation that create or constrain compliance claims.

## Canonical Review Packet

| Path | Role |
|---|---|
| `docs/compliance/legal-review/README.md` | Directory contract and update rules. |
| `docs/compliance/legal-review/2026-05-06-bp-d10-legal-review-report.md` | Non-secret legal-review report archive. |
| `docs/compliance/legal-review/code-sync-manifest.md` | This synchronization manifest. |

## Compliance Matrix Code

| Path | BP item | Sync reason |
|---|---|---|
| `backend/compliance_matrix/medical.py` | BP.D.1 / BP.D.5 | Medical auxiliary claims, standards, disclaimer, `_auxiliary_` API. |
| `backend/compliance_matrix/automotive.py` | BP.D.2 / BP.D.5 | Automotive auxiliary claims, standards, disclaimer, `_auxiliary_` API. |
| `backend/compliance_matrix/industrial.py` | BP.D.3 / BP.D.5 | Industrial auxiliary claims, standards, disclaimer, `_auxiliary_` API. |
| `backend/compliance_matrix/military.py` | BP.D.4 / BP.D.5 | Military / aerospace auxiliary claims, standards, disclaimer, `_auxiliary_` API. |
| `backend/routers/compliance_matrix.py` | BP.D.6 | REST schema pins `audit_type="advisory"` and `requires_human_signoff=true`. |
| `backend/pep_gateway.py` | BP.D.8 | `guild_id` policy dimension prevents inadmissible Guild x Tier inheritance. |

## Compliance Skills

| Path | BP item | Sync reason |
|---|---|---|
| `configs/skills/compliance-audit/SKILL.md` | BP.D.7 | Human-readable skill rules and forbidden final-decision language. |
| `configs/skills/compliance-audit/tasks.yaml` | BP.D.7 | Machine-readable auxiliary task IDs, standards, and human-signoff requirement. |
| `configs/skills/compliance-audit/scaffolds/README.md` | BP.D.7 | Skill-pack scaffold evidence. |
| `configs/skills/compliance-audit/docs/compliance_audit_integration_guide.md.j2` | BP.D.7 | Generated integration guide template. |

## Tests And Drift Guards

| Path | BP item | Sync reason |
|---|---|---|
| `backend/tests/test_compliance_matrix.py` | BP.D.9 | Matrix contract tests for advisory envelope, standards, claim source, and filtering. |
| `backend/tests/test_compliance_audit_skill.py` | BP.D.7 | Skill-pack contract tests for `_auxiliary` tasks and advisory output. |
| `backend/tests/test_pep_gateway.py` | BP.D.8 | Guild-aware PEP Gateway policy contract. |
| `backend/tests/test_bp_d10_legal_review_archive.py` | BP.D.10 | Archive drift guard for this legal-review packet. |

## Source Documents

| Path | Sync reason |
|---|---|
| `docs/design/blueprint-v2-implementation-plan.md` | Phase D scope, risk R1, and third-party legal-review commitment. |
| `docs/design/sandbox-tier-audit.md` | Source-of-truth engineering evidence cited by the compliance matrices. |
| `docs/design/pep-gateway-tier-policy.md` | Guild-aware policy inheritance and R12 disclaimer. |
| `docs/security/r12-gvisor-cost-weight-only.md` | Canonical warning that gVisor is nominal until Phase U. |

## Update Rule

When any listed path changes in a way that affects compliance claim
wording, standards coverage, advisory output, human-signoff gating, or
Guild x Tier inheritance, update this manifest and the legal-review
report in the same change.

No signed external legal opinion, reviewer identity, privileged comments,
or private evidence-vault URL belongs in this repository.
