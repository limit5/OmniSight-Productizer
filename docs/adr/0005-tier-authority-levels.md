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

Gerrit O7 enforcement (per [ADR 0003](0003-gerrit-code-review.md)) is implemented as **declarative `[submit-requirement "..."]` blocks** in `project.config` on `refs/meta/config` (the original Prolog `rules.pl` plan was abandoned in OP-697 — Gerrit 3.13 rejects new `rules.pl` uploads). The Phase 3 tier-aware authority rule will extend the existing dual-+2 set with per-tier conditional gates:

```ini
# Tier S — AI self-+2 sufficient. Applies when tier label = "s".
[submit-requirement "Tier-S-AI-Self-Plus-2"]
    description = Tier S allows any AI +2 to satisfy review (24h revert window backstops misclassification)
    applicableIf = label:Tier=s
    submittableIf = label:Code-Review=+2,group=ai-reviewer-bots
    canOverrideInChildProjects = false

# Tier M — AI +1 plus human +1 (relaxed from default human +2).
[submit-requirement "Tier-M-Mixed-Plus-2"]
    description = Tier M needs at least 1 AI +1 AND at least 1 human +1
    applicableIf = label:Tier=m
    submittableIf = label:Code-Review=MAX,group=ai-reviewer-bots AND label:Code-Review=MAX,group=non-ai-reviewer
    canOverrideInChildProjects = false

# Tier L — default human-+2 hard gate (no relaxation).
# (Phase 3 leaves the existing Human-Plus-2 / Merger-Plus-2 / No-Veto blocks in place;
#  Tier L changes get them as-is.)

# Tier X — human +2 plus mandatory architectural-reviewer +1.
[submit-requirement "Tier-X-Architecture-Review"]
    description = Tier X requires +2 from non-ai-reviewer AND +1 from architecture-reviewer subgroup
    applicableIf = label:Tier=x
    submittableIf = label:Code-Review=+2,group=non-ai-reviewer AND label:Code-Review>=+1,group=architecture-reviewer
    canOverrideInChildProjects = false
```

The `Tier` label itself comes from a separate `[label "Tier"]` block (values `s`/`m`/`l`/`x`) populated automatically by a server-side hook from the path-glob map (`configs/governance/tier-paths.yaml` — Phase 3 deliverable). Contributors cannot manually downgrade the tier label; the hook overwrites any user-set value with the computed minimum on every patchset upload.

Note: `applicableIf` chains can be combined with `AND` / `OR`, so non-conflict-free Tier S changes can still drop into the strict path. The Phase 3 spec must define the exact override matrix (e.g., what happens when a Tier S change touches the `Merge-Conflict-Resolved` hashtag — which gate wins).

**Consequences**

Positive:
- AI throughput on trivial work (Tier S) increases ~10× (no human gate)
- Critical paths (auth / crypto / deploy / schema) keep strong human gates
- Misclassification protection has 4 independent layers (single-layer breach insufficient to bypass)
- Path-based rule is auditable + testable in isolation (declarative submit-requirements + path map = pure functions; Gerrit's `?o=SUBMIT_REQUIREMENTS` REST query exposes per-rule status for any change)
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
