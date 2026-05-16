# Runner Comment Dedupe Policy

## 1. Purpose

This policy defines the runtime comment dedupe layer for runner-authored JIRA comments.

The implementation ticket is OP-1150.

The library is `backend/agents/runner_comment_dedupe.py`.

The target caller is runner code that emits structured `[runner-*]` comments.

The immediate problem is comment noise.

The incident that motivated this policy involved OP-1126 and OP-1124.

During the overnight run on 2026-05-15, those two tickets received 221 noisy comments.

The repeated template was a `[runner-capability-blocked]` style message.

The runner cycled through the same capability-blocked state.

Each cycle posted the same operator-facing note.

The ticket history became difficult to audit.

Operators had to distinguish signal from repeated runner churn.

The fix is a small runtime wrapper.

The wrapper sits between runner call sites and JIRA comment posting.

It suppresses repeated comments that share the same ticket and structured tag.

It does not delete historical comments.

It does not alter JIRA transitions.

It does not alter ticket assignment.

It does not alter runner pickup logic.

It only decides whether a specific comment should be posted now.

## 2. Scope

The policy applies to runner-generated comments.

The policy applies to comments whose body starts with or contains a bounded structured tag.

Examples include `[runner-capability-blocked]`.

Examples include `[runner-pushed-to-gerrit]`.

Examples include `[runner-dependency-blocked]`.

The policy does not apply to human comments.

The policy does not apply to Claude or Codex task notes unless they opt into this wrapper.

The policy does not apply to arbitrary backend JIRA comments by default.

Existing `jira_dispatch.add_comment` callers remain valid.

Existing client `post_comment` callers remain valid.

Callers must opt in by calling `maybe_post_comment`.

The intended replacement shape is simple.

Old call sites post directly.

New opted-in call sites call `maybe_post_comment(jira_client, ticket_id, tag, body)`.

The wrapper returns `True` when a real post happens.

The wrapper returns `False` when a duplicate is suppressed.

## 3. Contract

The public decision helper is `should_post`.

The public convenience helper is `maybe_post_comment`.

`should_post` accepts `ticket_id`.

`should_post` accepts `tag`.

`should_post` accepts `body`.

`should_post` accepts `window_min`.

`should_post` accepts an optional `jira_client`.

The default window is 5 minutes.

The dedupe key is `(ticket_id, tag)`.

The body is not part of the dedupe key.

The same tag on the same ticket within the window is suppressed.

A different tag on the same ticket is allowed.

The same tag on a different ticket is allowed.

A repeated tag after the trailing window expires is allowed.

`maybe_post_comment` uses `should_post` when dedupe is enabled.

`maybe_post_comment` posts without dedupe when the env flag is disabled.

This keeps rollout opt-in.

The feature flag is `OMNISIGHT_COMMENT_DEDUPE_ENABLED`.

The default is off.

Truthy values are `1`, `true`, `yes`, and `on`.

Any other value is treated as disabled.

## 4. Storage Decision

The chosen strategy is option 4.

Option 4 is hybrid storage.

It uses an in-memory cache for the fast path.

It uses a JIRA comment query on cache miss.

This satisfies the multi-instance requirement after one runner has landed a comment.

If `claude-1` posts a tagged comment, `claude-2` can see it through JIRA.

That property is not possible with in-memory-only storage.

That property is not possible with local SQLite-only storage.

Pure JIRA query would also be correct.

Pure JIRA query would cost one REST request for every decision.

The hybrid strategy avoids that cost for hot repeated loops in the same process.

The hybrid strategy still consults JIRA when local memory has no entry.

This is the minimum design that is both practical and multi-instance aware.

The JIRA comment history is the cross-host source of truth.

The local cache is only an optimization.

The local cache must never be treated as authoritative across hosts.

The local cache must never cause a long-term suppression after the window.

The local cache must be bounded.

The local cache must be safe to discard on restart.

Restarting a runner may lose the local fast path.

Restarting does not lose the JIRA-backed decision path.

## 5. Cache Eviction

The cache key is `(ticket_id, tag)`.

The cache value is the UTC timestamp of the last observed allowed or suppressed event.

Entries expire once they are outside the caller's trailing `window_min`.

The implementation prunes expired entries before each decision.

The implementation also caps cache size.

The cap is 1024 keys per process.

When the cap is exceeded, the oldest entry is evicted.

The eviction shape is oldest-entry eviction through an ordered dictionary.

This is intentionally simpler than a background sweeper.

Runner processes call the helper synchronously.

There is no need to add a thread.

There is no need to add a database.

There is no need to add a new service.

The cap bounds memory for long-lived runners.

The TTL bounds stale suppression.

The JIRA query covers cache misses after eviction.

## 6. JIRA Query

On cache miss, the wrapper fetches recent ticket comments.

The query scans a bounded recent page.

The implementation asks for the latest 50 comments.

The wrapper flattens JIRA ADF comment bodies into plain text.

The wrapper searches for the structured tag in the flattened text.

The wrapper parses the JIRA `created` timestamp.

If the timestamp is inside the trailing window, the wrapper suppresses.

If the timestamp is outside the trailing window, the wrapper allows.

If no matching tag is found, the wrapper allows.

Malformed comment bodies are ignored.

Malformed timestamps are ignored.

Ignoring malformed comments avoids blocking runner progress on one odd historical record.

## 7. Fail-Open Rule

JIRA API failure must not silence operator-visible events.

If the JIRA query fails, `should_post` returns `True`.

This is fail-open.

The duplicate comment risk is accepted during JIRA read failure.

The alternative would hide potentially important runner state.

Observability beats silence for this wrapper.

The helper logs the failed query.

The helper does not raise the JIRA query error to the runner.

The helper does not increment suppression metrics on fail-open.

Fail-open means no suppression happened.

## 8. Rollout Plan

The env flag is `OMNISIGHT_COMMENT_DEDUPE_ENABLED`.

The default state is off.

The initial merge only ships the library and policy.

Runner call sites opt in separately.

Operators should leave the flag off for one week of soak after merge.

During soak, import and unit tests prove the library is available.

Downstream tickets can wire one runner path at a time.

After one week, operators can flip the flag on for selected runner instances.

The go-live target is 7 working days after merge.

The staged rollout reduces risk in the comment path.

The staged rollout also makes it easy to compare comment volume before and after enablement.

If unexpected suppression appears, unset the env flag.

Unsetting the env flag restores direct post behavior in `maybe_post_comment`.

No database migration is needed.

No backfill is needed.

No cleanup job is needed.

## 9. Backward Compatibility

Direct callers of `jira_dispatch.add_comment` continue to work.

Direct client `post_comment` callers continue to work.

The wrapper does not monkeypatch JIRA dispatch.

The wrapper does not modify `backend/agents/jira_dispatch.py`.

Only callers that opt into `maybe_post_comment` get dedupe.

When the env flag is off, `maybe_post_comment` posts directly.

That default-off behavior preserves legacy semantics.

Return values are additive for new call sites.

Existing call sites that ignore return values are unaffected until they opt in.

The structured tag remains caller-owned.

The comment body remains caller-owned.

The wrapper does not rewrite comment text.

## 10. Observability

Suppression increments a Prometheus counter.

The metric name is `omnisight_runner_comment_suppressed_total`.

The labels are `ticket` and `tag`.

The `tag` label is bounded by runner code conventions.

The `ticket` label is unbounded over time.

That creates cardinality risk.

The immediate counter keeps OP-1150 evidence simple and operator-greppable.

Production dashboards should avoid long-retention high-cardinality panels by ticket.

Alerting should aggregate by tag first.

Ticket-level views should use short retention windows.

If suppression volume grows, introduce a cardinality cap.

The preferred cap is to retain raw `ticket` labels only for the top N most-suppressed active tickets.

All other tickets should be reported as `ticket="other"`.

An alternative is to use a stable hashed bucket label.

The top-N strategy is more operator-friendly during incidents.

The bucket strategy is more storage-friendly for fleet-wide trend charts.

Either cap belongs in a follow-up because OP-1150 defines the library surface.

The increment path must be wrapped in `try/except`.

Metric failure must not block runner comments.

This mirrors existing backend metrics caller practice.

## 11. Cross-Host Behavior

The local cache handles repeat attempts in one process.

The JIRA query handles repeat attempts after another process has posted.

Two runners on different hosts are expected to converge through JIRA.

Example: `claude-1` posts `[runner-capability-blocked]` on OP-1126.

One minute later, `claude-2` checks OP-1126 with the same tag.

`claude-2` misses its local cache.

`claude-2` queries JIRA.

`claude-2` sees the recent tagged comment.

`claude-2` suppresses its duplicate.

The design does not claim distributed locking.

If two hosts check before either post is visible in JIRA, both may post.

That race is acceptable for this library.

Eliminating that race would require a distributed lock or JIRA-side idempotency contract.

Those are outside this ticket.

For the OP-1126 and OP-1124 noise pattern, the repeated loop was trailing-window churn.

The JIRA-backed check addresses that pattern.

## 12. Operator Guidance

Use the wrapper for templated runner comments.

Do not use the wrapper for one-off human-authored details.

Choose a stable structured tag.

Keep the tag bounded.

Do not include ticket-specific data in the tag.

Do not include timestamps in the tag.

Put details in the body, not in the tag.

Use the default 5-minute window unless a ticket-specific reason exists.

Increase the window only for known high-churn paths.

Decrease the window when comments represent rapidly changing state.

When diagnosing suppression, inspect JIRA history first.

Then inspect `omnisight_runner_comment_suppressed_total`.

Then inspect runner logs for `runner_comment_dedupe.jira_query_failed`.

If the flag is off, suppression should not occur through `maybe_post_comment`.

If direct `add_comment` is used, suppression cannot occur.

## 13. Test Expectations

The first post on an empty state returns true.

The same tag on the same ticket inside the window returns false.

A different tag returns true.

A post after the window expires returns true.

A different ticket returns true.

A cross-instance scenario is tested through mocked JIRA comments.

The cross-instance test must not rely on local cache.

The JIRA failure test must return true.

The env flag default-off test must post twice.

The env flag enabled test must suppress the second post.

The module import test must succeed in the project virtualenv.

Existing `jira_dispatch` tests should remain green.

## 14. Non-Goals

This policy does not define a new database table.

This policy does not define a new queue.

This policy does not define a new JIRA workflow transition.

This policy does not define automatic deletion of old comments.

This policy does not define comment body similarity scoring.

This policy does not dedupe across different tags.

This policy does not dedupe across different tickets.

This policy does not require every runner comment to opt in immediately.

This policy does not replace OP-690 cleanup scripts.

The OP-690 script was a one-shot deletion tool.

This library is a runtime prevention tool.

That distinction matters for auditability.
