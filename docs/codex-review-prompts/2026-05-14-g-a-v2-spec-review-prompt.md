# Codex Review Prompt — Sprint S12.G G.A-v2 Runtime Defense Contract spec (v1.1)

**For**: codex independent review cycle (mirrors G.A-v1 v1→v2 amendment pattern; G.A-v1 was reviewed per `/tmp/phase31{e,g,h,i,j}-codex-review-final.txt` precedent)
**Target output file**: `/tmp/g-a-v2-codex-review-2026-05-14.txt`
**Reviewer role**: independent technical reviewer — you did not write this spec; you are scrutinising it for gaps, contradictions, hidden assumptions, decomposition quality, and dog-food compliance.

---

## Documents to read (in order)

1. **The spec itself**: `docs/sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md` (v1.1, ~400 lines)
2. **Trigger event**: `docs/retrospectives/2026-05-14-host-reboot-image-db-drift.md` — the incident that surfaced the 5 gaps (⑤⑥⑦⑧⑨)
3. **Strategic context**: `docs/architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md` — the meta-reframe that says G.A-v2 defense_contract belongs to Layer 1 Platform primitives, not runner-specifically
4. **Parent sprint spec**: `docs/sprint-s12/sprint-s12g-governance-engine-spec.md` — G.A-v0 + G.A-v1 + G.B/C/D context this slots into
5. **Decomposition discipline**: `docs/sop/sprint-s12-ticket-decomposition-rules.md` — atomic decomposition rules every Sprint S12 ticket must conform to
6. **Cross-references inside spec**: ADR-0023 (Foundation Rebuild), ADR-0033 (Governance Engine), ADR-0034 (Override Lifecycle), ADR-0035 (Runner FSM)

## Review focus — 7 specific questions

Answer each explicitly in your review output; do not skip any. Cite spec section + line range for every claim.

### Q-A. Completeness of the 5 gap families (⑤⑥⑦⑧⑨)

For each of ⑤ shipped-not-deployed / ⑥ image-vs-DB drift / ⑦ allowlist single-source / ⑧ shutdown contract / ⑨ optional auxiliary:

- Are there failure modes mentioned in the retrospective that aren't covered by the family's tickets?
- Are there ADJACENT failure modes (same class but different surface) that should be in the family?
- Are recovery paths (D4) genuinely deterministic, or are some still hand-wavy?

### Q-B. Atomic decomposition compliance (per SOP §1-§3)

The v1.1 spec is at OVERVIEW-table level, not per-child detailed-spec level. Yesterday's G.A-v1 batch (OP-1079..1097) had per-child specs ~300-500 LOC each (citing file paths, function names, type annotations, label sets).

- Identify which v2-* tickets, when expanded to G.A-v1's level of detail, would still be tier:S (atomic = doable in one bounded pickup), and which would inevitably split further.
- Specifically scrutinise: `v2-A3` (11 validators?), `v2-⑥-RescueCLI` (CLI + override + audit), `v2-⑨-1bc` (probe + gauge + flag — split candidate?), `v2-⑧-2a` (systemd refactor — operator-window-tight?). List others you suspect are too coarse.
- Tier check: integration tickets MUST be tier:M or higher per SOP §3.2. Verify v2-⑦-Integration, v2-⑨-Integration, v2-A7-Integration, v2-AlertBridge-AMMigrationTest tier assignment.

### Q-C. Boundaries declaration completeness

Each ticket in G.A-v1 carries a `boundaries:` block (mutex_with, destructive_op_*, scope_components, external_side_effect, runtime_capability, evidence_class, tag_type). The v1.1 spec describes ticket purposes but NOT per-ticket boundaries.

- Identify ≥3 tickets where boundary fields are ambiguous from the title alone and need explicit declaration.
- For each, propose what boundaries you'd assign and why.
- Highlight any ticket whose scope_components would likely cross area boundaries (per SOP §2 path → area rule) — those will fail filing-time validation unless split.

### Q-D. SOP §2 file-path → area-label audit

The v1.1 spec does NOT list which files each ticket modifies. SOP §2 requires this to be reverse-auditable at filing time.

- Pick 5 v2-* tickets and infer their likely file-touch set + required area labels.
- Identify any ticket whose implied area labels would conflict with its declared `class:` or `prefer:`.
- Suggest a per-ticket `required_paths:` field that filing-time hook can check.

### Q-E. Cross-phase dependency correctness

§6 lists cross-phase externals: 31.G-LibsGate-external (AlertRule blockers), 31.C-Integration-external (Discord routing), 31.H-StabilityCheckpoint-external (BackupDRDrill), 31.I-2bc (reverse-direction — 31.I waits on us).

- Are any of these reversed (we should block them, but spec says they block us)?
- Are any missing? Specifically: does v2-⑥ depend on 31.H canary-runner? Does v2-⑤ depend on 31.E CI Wave 2 image manifest? Does v2-⑦ depend on anything in 31.B runner governance?
- Is the G.A-v2 → G.B → 31.B sequencing tight enough that one ship slipping doesn't cascade?

### Q-F. Hidden-assumption surface

The spec assumes:
- `/readyz` migration probe is canonical drift detection (vs filesystem scan / image manifest comparison / cron-based audit — alternatives are not discussed)
- Postgres-backed coordination substrate is the right substrate (vs Redis, etcd, ZooKeeper — alternatives are not discussed)
- Filing-time hook (OP-1042 extension) is the right enforcement point for SD-1 (vs post-filing audit, vs CI pre-merge — alternatives are not discussed)
- The 3-layer architecture (Platform / dev-runner / user-agent) will land — but G.A-v2 ships before the strategic doc's Q1/Q2 close

List ≥5 OTHER implicit assumptions that, if wrong, would invalidate the spec. For each, suggest how to test the assumption before committing to it.

### Q-G. SD-1 self-compliance (dog-food check)

The v1.1 spec ADDED rule SD-1 (`novelty-claim-requires-evidence`) BECAUSE the strategic doc (companion file) violated it. Does the spec ITSELF satisfy SD-1?

- Scan the spec for novelty phrases (`first surfaced`, `new`, `never before`, `不曾`, etc.).
- For each, verify there's a corresponding `evidence_grep_artifact` or equivalent justification.
- If the spec fails its own SD-1 rule, that's a P0 review finding — report it loudly.

## Output structure

Mirror G.A-v1 codex review format (`/tmp/phase31*-codex-review-final.txt`):

```
## §0. Bottom-line recommendation
[ship-as-is | ship-with-amendments | hold-and-rework | reject]

## §1. P0 BLOCKING findings (must fix before file)
[per-finding: section + line range + concrete fix]

## §2. P1 STRUCTURAL findings (should fix in v2)
[same format]

## §3. P2 POLISH findings (nice-to-have)
[same format]

## §4. Per-question answers (Q-A through Q-G above)
[explicit answer to each question with cited evidence]

## §5. Open questions for operator
[anything you need operator clarification on; not findings]

## §6. SOP discipline check
- Atomic decomposition: [✓ / partial / fail]
- Integration tier:M+ rule: [✓ / partial / fail]
- File-path → area label: [✓ / partial / fail]
- SD-1 self-compliance: [✓ / partial / fail]
- Cross-phase blocker shape: [✓ / partial / fail]
```

## Tone / scope reminders

- This is an INDEPENDENT review. Disagree with the author when warranted. The G.A-v1 review cycle produced 7 P0 findings per phase; we expect similar density here.
- Be concrete: every finding cites file:section:line and proposes a specific fix.
- Don't repeat the spec back to us; only call out divergence or gaps.
- The spec is v1.1; expect a v2 amendment cycle after your review. Frame findings as v2 inputs.
- 4-week soft-fail SD-1 pilot means SD-1 violations in this v1.1 spec are NOT blocking, but you should still flag them so v2 amendment can address.

## Estimated review duration

~2-3 hours for a thorough pass. G.A-v1 reviews took ~1-2 hours per phase doc (each was 400-700 LOC); G.A-v2 v1.1 is ~400 LOC + ~5 referenced docs.
