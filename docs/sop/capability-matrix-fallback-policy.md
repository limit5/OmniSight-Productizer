# Capability Matrix Fallback Policy

Status: proposed operating policy for OP-1148.

Owner: runner operators.

Applies to: `backend/agents/capability_matrix.py`.

Applies to: `config/capability_matrix.yaml`.

Applies to: runner pickup capability resolution.

Does not apply to: runtime tool authorization after pickup.

Does not apply to: human JIRA edits.

Does not apply to: Gerrit review scoring.

## 1. Purpose

The capability matrix controls what a runner may do for a ticket.

It maps a JIRA issue type, area, and tier to a capability set.

The runner then enforces that capability set at tool-dispatch boundaries.

The matrix intentionally has a safe default.

The safe default is read-only.

The safe default exists to prevent accidental writes when metadata is unknown.

The safe default is not meant to hide matrix coverage gaps.

OP-1124 and OP-1143 showed a recurring failure mode.

A ticket looked like normal implementation work.

The matrix failed to match a richer row.

The runner received the read-only default.

The agent still did useful work inside the checkout.

The final push was blocked because `gerrit_push` was absent.

The ticket then had to be reverted by the runner.

The missing signal was not the block itself.

The missing signal was why the safe default was selected.

This policy separates legitimate fallback from unexpected miss.

## 2. Terms

`safe default` means `read_only_default` from the YAML.

The current safe default is `mcp_search` plus `memory_recall`.

`resolved capabilities` means the final set after matrix lookup.

`label override` means `capability:enable=` or `capability:disable=`.

`expected combo` means a `(issuetype, area, tier)` that policy says should
have an intentional matrix row.

`unexpected miss` means an expected combo that returns the safe default because
the row was absent.

`legitimate fallback` means the safe default was selected for a combo that is
not expected to have a richer row.

`strict mode` means `OMNISIGHT_CAPABILITY_FALLBACK_STRICT=1`.

`warn mode` means the default mode where the runner continues but emits
observability.

## 3. Existing Lookup Behavior

The lookup starts with the incoming JIRA issue type.

The issue type is normalized through `canonical_issuetype`.

This handles known localized names.

The normalized issue type is used as the first YAML key.

The area is used as the second YAML key.

The tier is used as the third YAML key.

If all three keys exist, lookup returns the listed capabilities.

If any key is absent, lookup returns no row to the caller.

The public resolver applies label overrides.

In non-strict mode, the resolver returns `read_only_default`.

In existing explicit strict mode, the resolver raises
`CapabilityMatrixMissingEntry`.

OP-1148 adds a second strict path for expected misses.

## 4. Legitimate Safe-Default Cases

Unknown areas may use the safe default.

Unknown areas include labels outside the runner-recognized area vocabulary.

Unknown areas may come from manual triage labels.

Unknown areas may come from future components before policy is updated.

Unknown issue types may use the safe default.

Unknown issue types include non-runner work items.

Unknown issue types include exploratory JIRA shapes.

Meta-only tickets may use the safe default.

A meta-only ticket is a ticket whose purpose is reading, classification, or
coordination without repository mutation.

Observability-only tickets may use the safe default.

Examples include reading dashboards, gathering logs, and posting findings.

Discovery tickets may use the safe default.

Examples include tickets that ask the agent to inspect existing behavior and
report a dependency.

Surrender tickets may use the safe default.

Examples include tickets that intentionally test §11 dependency detection.

Tickets with no known implementation area may use the safe default.

Tickets whose labels are contradictory may use the safe default until a human
clarifies metadata.

Tickets with area labels that are deliberately excluded from the runner may use
the safe default.

Tickets owned by another automation lane may use the safe default.

Tickets whose safest action is to avoid writes may use the safe default.

The common property is that no richer capability row is expected.

## 5. Unexpected Miss Definition

An unexpected miss is not just any missing row.

An unexpected miss requires policy intent.

The combo must be declared expected by YAML metadata.

The resolver must return the safe default because lookup missed.

The ticket type must be the normalized issue type.

The area must be the runner area label.

The tier must be the runner tier label.

The returned capability set must be the read-only fallback before label
overrides.

When these conditions are true, fallback is observable debt.

When these conditions are false, fallback is normal defensive behavior.

Story tickets in recognized implementation areas are usually expected.

Backend implementation tickets are expected.

Frontend implementation tickets are expected.

Tests implementation tickets are expected.

Docs implementation tickets are expected when they require commits.

Tooling implementation tickets are expected.

DB implementation tickets are expected, but DB work is outside OP-1148 scope.

Devops implementation tickets are expected, but devops work is outside
OP-1148 scope.

Security implementation tickets are expected, but security work is outside
OP-1148 scope.

Embedded implementation tickets are expected, but embedded work is outside
OP-1148 scope.

The expected flag is the durable source of truth.

Heuristics may suggest what should be expected.

Heuristics must not silently replace explicit metadata.

## 6. Why Missing Keys Matter

The emitted event includes `missing_keys`.

If `ticket_type` is missing, the normalized issue type is absent.

If `area` is missing, the issue type exists but the area key is absent.

If `tier` is missing, the issue type and area exist but the tier is absent.

This distinction matters for repair.

A ticket-type miss may mean JIRA localization drift.

An area miss may mean a new area label was introduced.

A tier miss may mean a tier was omitted during YAML expansion.

Operators should fix the narrowest missing key.

Operators should avoid adding broad fallback rows to hide the symptom.

## 7. Schema Extension Proposal

The matrix should support row-local expected metadata.

The row-local form is:

```yaml
matrix:
  Story:
    backend:
      M:
        capabilities:
          - code_edit
          - run_tests
          - run_lint
          - gerrit_push
          - jira_update
          - mcp_search
          - memory_recall
        expected: true
```

The legacy list form remains valid.

Legacy list rows are treated as `expected: true`.

Rows can opt out with `expected: false`.

The opt-out should be rare.

The opt-out is useful for deliberately read-only rows.

The opt-out is useful for temporary observation rows.

The opt-out should include an adjacent YAML comment explaining why.

The matrix should also support explicit expected coverage metadata.

The expected coverage form is:

```yaml
expected_coverage:
  Story:
    backend: [S, M, L]
    tooling: [S, M, L]
```

This form declares combos that should have rows.

It lets the loader detect a missing row before the row exists.

It also lets tests model hypothetical missing entries.

Both forms can coexist.

The union is the expected combo set.

The expected combo set is not a capability grant.

The expected combo set is only an observability contract.

## 8. Cardinality Policy

The metric labels are `area`, `tier`, and `issuetype`.

Each label must stay bounded.

The per-label value limit is ten.

This limit matches the runner-defense cardinality discipline.

The expected combo set should be validated at load time.

If more than ten areas are marked expected, loading should fail.

If more than ten tiers are marked expected, loading should fail.

If more than ten issue types are marked expected, loading should fail.

Ticket IDs must not be Prometheus labels.

Ticket IDs may appear in structured logs.

Ticket IDs are high-cardinality.

High-cardinality values belong in logs, not counters.

## 9. Runtime Behavior

On legitimate fallback, return the safe default.

On legitimate fallback, keep the existing `missing_entry` warning behavior.

On legitimate fallback, do not increment the unexpected fallback counter.

On unexpected miss in warn mode, return the safe default.

On unexpected miss in warn mode, emit `capability_matrix.fallback_used`.

On unexpected miss in warn mode, increment the Prometheus counter.

On unexpected miss in strict mode, emit the same warning.

On unexpected miss in strict mode, increment the same counter.

On unexpected miss in strict mode, raise `CapabilityMatrixUnexpectedMiss`.

Strict mode must be reversible.

Turning the env var off must restore warn-mode behavior.

Strict mode must not mutate the matrix.

Strict mode must not suppress label validation.

Strict mode must not affect successful lookups.

## 10. Structured Warning Contract

The event name is `capability_matrix.fallback_used`.

The log level is warning.

The fields are `area`, `tier`, `issuetype`, `missing_keys`, and `ticket_id`.

The `area` field is the runner area label.

The `tier` field is the runner tier label.

The `issuetype` field is the normalized issue type.

The `missing_keys` field is an array.

The `ticket_id` field may be null.

Callers should pass the JIRA key when they have it.

The matrix module should still work when no ticket key is available.

The warning must be emitted before any strict-mode exception.

This keeps strict-mode failures visible in logs.

## 11. Metric Contract

The counter is `omnisight_capability_matrix_unexpected_fallback_total`.

The labels are `area`, `tier`, and `issuetype`.

The counter increments once per unexpected fallback lookup.

Repeated misses for the same combo increment the same series.

Different combos use separate series.

The counter is exposed by the existing `/metrics` endpoint.

The counter must be registered in the shared metrics registry.

The counter must no-op when `prometheus_client` is unavailable.

The counter must not block runner pickup.

Metric publication failures must be debug-level only.

## 12. Rollout Plan

Phase 1 is forward-only instrumentation.

Phase 1 keeps strict mode off by default.

Phase 1 lands the logger event and metric.

Phase 1 lands tests using synthetic expected misses.

Phase 1 does not retroactively alert on all historical safe defaults.

Phase 2 adds expected metadata to the shipped YAML deliberately.

Phase 2 should be a separate small change.

Phase 2 should review every expected combo by area owner.

Phase 2 should not include unrelated capability grants.

Phase 3 enables dashboards and Discord alerts.

Phase 3 can alert when the counter increases.

Phase 3 should include the area, tier, and issue type.

Phase 3 should link this SOP.

Phase 4 enables strict mode in canary runner lanes.

Phase 4 should use `OMNISIGHT_CAPABILITY_FALLBACK_STRICT=1`.

Phase 4 should verify that §11 reverts contain the exception reason.

Phase 5 considers enabling strict mode more broadly.

Strict mode should not be enabled globally until the expected set is stable.

## 13. Retroactivity Decision

Existing safe-default fallbacks should not all emit new alerts retroactively.

Retroactive alerting would mix real misses with intentional read-only work.

Retroactive alerting would produce noisy historical debt.

The preferred path is forward-only.

Forward-only means only explicitly expected combos trigger the new signal.

Forward-only means the rollout can be reviewed in small YAML changes.

Forward-only means operators can tune alerts before enforcing strict mode.

The env flag is the enforcement boundary.

The default remains compatible with current runner pickup.

## 14. Operator Response

When the counter increments, inspect the structured log.

Identify the missing key.

If `tier` is missing, add the missing tier row.

If `area` is missing, add the missing area block.

If `ticket_type` is missing, verify JIRA issue type normalization.

If the fallback was actually legitimate, mark the combo `expected: false`.

If the expected coverage metadata was too broad, narrow it.

Do not grant `gerrit_push` with a label as the permanent fix.

Labels are temporary escape hatches.

The durable fix belongs in the matrix or expected metadata.

## 15. Ticket Authoring Guidance

Ticket descriptions should carry accurate area labels.

Ticket descriptions should carry accurate tiers.

META tickets should distinguish implementation work from read-only planning.

If a META ticket has implementation children, the child tickets should carry
the implementation areas.

If a META ticket is read-only, it should not be marked as expected coverage.

If a ticket crosses areas, the runner should use multi-area union resolution.

If one area is mapped and one area is missing, the runner may proceed with the
union while warning about partial missing coverage.

If all declared areas miss and the combo is expected, the unexpected fallback
path applies.

## 16. Test Expectations

Tests must include a legitimate fallback.

Tests must include an unexpected miss.

Tests must include strict mode.

Tests must include counter label behavior.

Tests must verify shared series increments.

Tests must verify different combos create different series.

Tests must verify label value cardinality stays bounded.

Tests should use synthetic YAML.

Synthetic YAML keeps production matrix policy separate from test fixtures.

Tests should not depend on live JIRA.

Tests should not depend on a running Prometheus server.

Tests may use the in-process Prometheus registry.

## 17. Non-Goals

This policy does not decide every future capability grant.

This policy does not replace human review.

This policy does not bypass Gerrit.

This policy does not change the capability vocabulary.

This policy does not make ticket IDs metric labels.

This policy does not require database schema changes.

This policy does not require devops deployment changes.

This policy does not change frontend dashboards directly.

This policy only prepares the metric for dashboard and Discord wiring.

## 18. Example Legitimate Fallback

Input issue type: `Story`.

Input area: `unknown-area`.

Input tier: `S`.

Expected metadata: no matching combo.

Lookup result: no row.

Returned capabilities: `mcp_search`, `memory_recall`.

Warning: existing missing-entry warning only.

Metric: no increment.

Strict exception: none from OP-1148 unexpected-miss path.

## 19. Example Unexpected Miss

Input issue type: `Story`.

Input area: `backend`.

Input tier: `M`.

Expected metadata: `Story.backend.M`.

Lookup result: no row.

Returned capabilities in warn mode: `mcp_search`, `memory_recall`.

Warning: `capability_matrix.fallback_used`.

Metric: counter increments with `area=backend`, `tier=M`, `issuetype=Story`.

Strict exception: `CapabilityMatrixUnexpectedMiss`.

## 20. Done Definition

The policy document exists.

The matrix module emits a warning for expected misses.

The matrix module increments the counter for expected misses.

The strict env flag raises `CapabilityMatrixUnexpectedMiss`.

Legitimate fallback remains quiet for the new event.

Existing capability matrix tests remain green.

The metric appears in the in-process exposition.

Runner forward transitions remain owned by the runner.

