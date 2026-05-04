# ADR 0005 — Tier S/M/L/X AI authority levels with 4-layer protection

**Status**: Accepted (2026-05-04)

**Context**

[ADR 0003](0003-gerrit-code-review.md) establishes the default rule: AI reviewer max +1, human +2 required for merge. Applied uniformly, this means every change — from a `ruff --fix` cleanup to a database schema change — needs a human in the loop.

Two opposite forces apply:

1. **Throughput**. Trivial changes shouldn't block on human attention. Half of FX-series fixes are mechanical (ruff sweeps, missing downgrades, unused imports). Forcing human +2 on those wastes attention and slows the AI runner pipeline by ~10×.
2. **Safety**. Some changes have huge blast radius (auth, crypto, schema, deploy infra). Even `+2` from one human is insufficient — these need architectural review or independent verification.

The dual-+2-by-human-only rule is too coarse: it both over-gates trivial work and under-gates extreme work. Need a graduated authority model.

We also need to defend against AI **misclassification** — an AI agent that labels a high-risk change as low-risk to bypass the gate.

**Decision**

Four-tier authority model:

| Tier | AI authority | Examples (path / change type) |
|---|---|---|
| **S (Safe)** | AI may self-+2; 24h revert window | `ruff --fix` only; `backend/tests/` test additions only; `*.md` doc fixes; `messages/*.json` locale strings; openapi auto-generated stubs |
| **M (Medium)** | AI +1 + Human +1 = dual-+1 = mergeable | Single-file bug fix (`backend/<module>.py` non-security); pip dep minor bump; alembic schema (additive column / index) |
| **L (Large)** | AI +1 advisory + Human +2 mandatory | New module; new schema (table create / drop); auth / security / crypto code; public API surface change |
| **X (Extreme)** | Human +2 + architectural review (RFC / ADR) | Branch structure; CI/CD pipeline; license; build infrastructure; reversibility-impacting operations (data migration, key rotation) |

**Tier assignment is path-based, then change-type-based**

Step 1 (path force-upgrade): If any touched path matches a force-upgrade rule, the change's tier is bumped to the rule's minimum.

| Path glob | Force-min tier |
|---|---|
| `backend/security/**` | L |
| `backend/auth*.py` | L |
| `backend/alembic/versions/*.py` (any new file) | L |
| `deploy/**` | X |
| `scripts/deploy-*.sh` | X |
| `.github/workflows/**`, `.gitlab-ci.yml` | X |
| `requirements*.txt`, `package.json`, `pyproject.toml` | M (security advisory blocks Tier S even for whitelisted paths) |

Step 2 (Tier S whitelist): If no path force-upgrade matched, check if all touched paths match the Tier S whitelist:

| Tier S whitelist (path glob) |
|---|
| `backend/tests/**` (test additions only, no production code) |
| `*.md` (docs) |
| `messages/*.json` (i18n locale strings) |
| `openapi/*.generated.json` (auto-gen stubs) |

If all paths whitelist → Tier S. Otherwise → Tier M (default for un-classified work).

**4-layer protection against misclassification**

1. **Path-based force upgrade** (rule-driven, contributor cannot downgrade): if `backend/security/**` matches, change is Tier L+ — no AI reasoning can override.
2. **Tier S whitelist-only**: paths outside the explicit whitelist auto-default to Tier M (deny-by-default for the most permissive tier).
3. **Reviewer monotonicity**: a reviewer can promote a tier (S → M → L → X) but cannot demote. Once a Tier L marker is in the change, no reviewer can flip it back to M.
4. **Audit + cooldown**: every tier classification + override is logged. If an agent misclassifies > 3 times in 30 days, that agent enters 30-day cooldown forcing all its work to Tier M+. Repeat → 90-day. Persistent misuse → revoke.

**Submit-rule integration**

Gerrit O7 submit-rule (per [ADR 0003](0003-gerrit-code-review.md)) reads tier label from change metadata:

```prolog
submit_rule(submit(ok(R))) :-
  change_tier(Tier),
  ( Tier = 's' -> ai_self_plus_2_allowed
  ; Tier = 'm' -> ai_plus_1_and_human_plus_1
  ; Tier = 'l' -> ai_plus_1_and_human_plus_2
  ; Tier = 'x' -> human_plus_2_and_architectural_review
  ),
  R = approved.
```

Tier label is computed automatically from path map + change-type keywords; contributor cannot manually set it lower than the computed minimum (only higher).

**Consequences**

Positive:
- AI throughput on trivial work (Tier S) increases ~10× (no human gate)
- Critical paths (auth / crypto / deploy / schema) keep strong human gates
- Misclassification protection has 4 independent layers (single-layer breach insufficient to bypass)
- Path-based rule is auditable + testable in isolation (Prolog rules + path map = pure functions)
- Compatible with Gerrit submit-rule infrastructure already planned

Negative:
- Path map maintenance overhead (any new sensitive directory must be added)
- 4-layer protection adds complexity to submit logic + audit infrastructure
- 24h revert window for Tier S means short-term flux (auto-fixes can be reverted by human)
- Tier classification ambiguity in edge cases (e.g. test that imports security module — path is test/ but reads sensitive code path → still Tier S? unclear)

Neutral:
- Existing manual review process continues for Tier L+ — not a regression
- Tier S whitelist is conservative by design — most "I want to ship fast" cases will fall to Tier M, which is a feature not a bug

**Initial whitelist + path map (2026-05-04)**

Stored in `configs/governance/tier-paths.yaml` (created during Phase 3 alongside Gerrit submit-rule). Until Phase 3, tier model is *advisory only* — manual reviewer judgement applies the rules informally.

**Related**

- CLAUDE.md L1 — base review rules (this ADR provides the gradation; never overrides)
- [ADR 0001 — Git Flow](0001-five-branch-gitflow.md) — defines branch protection independent of tier
- [ADR 0003 — Gerrit code review](0003-gerrit-code-review.md) — submit-rule integration point
- `project_governance_migration_plan.md` (memory) — 4-layer protection rationale + Phase 3 rollout
