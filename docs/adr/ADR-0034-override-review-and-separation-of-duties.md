---
id: ADR-0034
title: Override Review Lifecycle + Separation of Duties
status: Proposed
date: 2026-05-13
relates_to:
  - ADR-0033 (Governance Engine + Operator Authority Hierarchy)
  - Sprint S12.G — Governance Engine (this ADR addendum-style refines ADR-0033)
  - Codex S12.G review Q5/Q6 (2026-05-13)
---

# ADR-0034 — Override Review Lifecycle + Separation of Duties

## Status

Proposed (2026-05-13). Companion to ADR-0033.

## Context

ADR-0033 introduced 3-level operator authority (L1 / L2 / L3) and L1-exclusive `override path` for cross-phase preflight failures. Codex S12.G review Q5 flagged that "L1 fingerprint match against roster" is NOT sufficient audit trail:

> "Override should create a review artifact that G.D/G.E can display. Override decisions should be reviewed later by the operator or a designated governance review ticket, not by the same deputy who benefited from the override."

Additionally, Q4 flagged missing `separation_of_duties` field — without it, the same identity can author, approve, and override sensitive work.

ADR-0034 codifies override review lifecycle + separation-of-duties as enforceable policy.

## Decision

### 1. Override audit trail (full content)

Every override decision MUST record:

```yaml
override_record:
  override_id: str (UUID)
  ticket_key: str (the ticket being filed-with-override)
  override_timestamp: ISO8601
  override_expires_at: ISO8601 | null   # optional; bounded validity
  failed_rule_codes: [str, ...]         # which specific rules were waived
  failed_preflight_full: dict           # entire failed preflight result
  override_reason: str                  # operator's free-form justification
  adr_citation: str                     # MUST reference ADR or SOP authorizing this override
  l1_identity: str                      # GPG fingerprint of L1 who authorized
  roster_version_at_override: str       # which roster version was active
  affected_downstream_tickets: [str, ...]  # what would otherwise be blocked
  post_override_reviewer: str           # who reviews; CANNOT be ticket author or L1 who overrode
  review_deadline: ISO8601              # by when review must complete
  review_status: pending | approved | rejected | expired
```

Storage: `~/.config/omnisight/governance-overrides/<override_id>.yaml` (git-tracked OR signed) + JIRA comment on overridden ticket.

### 2. Separation of duties enforcement

For tickets carrying ANY of:
- `external_payload_class: key-material | credential-material`
- `external_side_effect: meta-closure | discord | email | network-production`
- `destructive_op_classes: git-ref-rewrite | systemd-control | env-edit` AND `destructive_op_scope: system`
- `l1_exclusive_reason: <any>`

The following identities MUST be distinct:
- `author` (filed the ticket)
- `approver` (operator who confirmed at gate)
- `executor` (who ran the operation if operator-window)
- `override_authorizer` (if override invoked)
- `post_override_reviewer` (if override invoked)

If filing tries to set 2+ of these to same identity → reject + escalate to operator.

For `l1_exclusive_reason: roster-mutation` specifically: 4-eye principle MANDATORY — author + approver + reviewer + executor all distinct (signed by 2 L1+L2 + reviewed by another L1+L2 + executed by third).

### 3. Override review lifecycle

- **Trigger**: override invoked at G.C filing-time
- **Review owner assignment**: filing-time hook auto-assigns reviewer from roster (excludes author + override-authorizer)
- **Review window**: 7 days while S12.G is active; expanded to 14 days post-S12 GA
- **Weekly review batch**: while S12.G active, all pending overrides reviewed by reviewer-of-the-week (rotation across L1 + designated L2)
- **Mandatory pre-31.K-closure review**: before 31.K-Integration closes Sprint S12 META, ALL S12-era overrides MUST have reviewed = approved | rejected (no pending)
- **Rejected overrides**: filing reverted; ticket re-filed without violation OR escalated to L1 for re-decision

### 4. Override review artifact format

Per overridden ticket, review produces:

```yaml
override_review:
  override_id: <ref>
  reviewer_identity: str
  reviewer_authority_level: L1 | L2
  review_timestamp: ISO8601
  decision: approved | rejected
  rationale: str
  follow_up_actions: [str, ...]    # e.g., "tighten rule X to prevent reuse"; "file ticket Y for missing field"
  artifact_archive_path: str       # where override + review lives long-term (post-S12)
```

Reviews are visible in G.E governance dashboard (read-only) and routed to 31.C P3 daily digest (volume).

### 5. Forbidden combinations enforced by SoD

In addition to ADR-0033 §3 forbidden combinations, ADR-0034 adds:

```yaml
sod_forbidden_combinations:
  - same identity in author + approver  (any L1-sensitive ticket)
  - same identity in approver + executor  (any operator-window ticket)
  - same identity in author + override_authorizer
  - same identity in override_authorizer + post_override_reviewer  (CRITICAL — reviewer must be independent)
  - L1 acting as own override_authorizer + post_override_reviewer  (even within L1 tier)
  - L1 acting as own author + executor + override_authorizer  (3-role conflict)
```

### 6. Escalation rules

If sufficient reviewer pool unavailable (e.g., only 1 active L1 + 1 active L2; L1 needs to override + L2 reviewer is also author):
- L1 may DELAY override until a non-conflicted reviewer is available
- L1 may NOT bypass review by self-reviewing
- Long-running unresolved escalations (>14d) auto-emit P1 alert via 31.C governance event class

## Consequences

**Positive**:
- Mechanically enforced separation of duties (NOT prose discipline)
- Override is auditable + reviewable + revocable
- Post-override review forces continuous-improvement loop (rules tightened based on real overrides)
- Aligns with codex Q5 recommendation; closes governance loophole

**Negative / Tradeoffs**:
- Small-team operations (1 L1 + 1 L2) constrained — separation may force schedule delays
- 7-day review window may slow filing in pinch
- Adds 4-5 fields to override audit trail (paperwork tax)

**Risks**:
- "Reviewer fatigue" — if many overrides happen, reviewer rubber-stamps; mitigation via weekly batch review + governance dashboard surfacing
- Single L1 + single L2 = forbidden combinations may block legitimate work; mitigation via documented escalation rule
- Override expiration (bounded validity) not yet defined; left for G.C implementation tuning

## Alternatives Considered

- **Same identity allowed if explicit ADR cite**: rejected — undermines whole SoD point
- **Auto-approve overrides after 7d** if no reviewer: rejected — risk of silent governance erosion
- **L1-only review of L1-overrides**: accepted as default but with non-self constraint
