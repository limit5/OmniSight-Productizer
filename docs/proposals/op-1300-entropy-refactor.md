# OP-1300 entropy refactor proposal

## Context

OP-1300 names `backend/agents/entropy.py` as the primary refactor target.
Pre-flight verification found that path is absent in this checkout. The tracked
entropy modules are:

- `backend/semantic_entropy.py`
- `backend/routers/entropy.py`
- `backend/tests/test_semantic_entropy.py`

Because the ticket explicitly scopes the proposal to
`backend/agents/entropy.py`, this pickup should not silently retarget the
design review to either existing module. Doing so would risk proposing changes
against a different ownership boundary than the ticket requested.

## Proposal

Abstain from a behavior or structure refactor proposal until the scope anchor is
corrected. The smallest safe follow-up is a scope-correction ticket that asks an
owner to choose one of these intended targets:

1. `backend/semantic_entropy.py`, if OP-1300 meant the semantic entropy monitor.
2. `backend/routers/entropy.py`, if OP-1300 meant the REST boundary.
3. A future `backend/agents/entropy.py`, if the missing agent module is expected
   to be introduced by another change first.

Once the intended module is confirmed, a follow-up implementation ticket can
reuse the OP-1282/OP-1286 proposal pattern: read the confirmed module, identify
one or two private-helper extractions, preserve public imports and runtime
contracts, and add focused tests only for behavior touched by the refactor.

## Non-goals

- No code changes in this patch set.
- No tests are added or modified while the target module is missing.
- No database, devops, embedded, frontend, security, tooling, or test-domain
  changes.
- No retargeting from `backend/agents/entropy.py` to another module without an
  explicit ticket update.

## Follow-up implementation ticket draft

Summary: Clarify OP-1300 entropy refactor target before implementation

Areas: backend, docs

Tier: S

Description:

Resolve the OP-1300 scope mismatch before any entropy refactor work:

- Confirm whether the intended target is `backend/semantic_entropy.py`,
  `backend/routers/entropy.py`, or a future `backend/agents/entropy.py`.
- If the intended target is an existing module, update the refactor ticket with
  the corrected scope anchor and acceptance criteria.
- If the intended target is a future module, link the prerequisite ticket that
  introduces it before dispatching the refactor.
- Keep the correction docs/backend-scoped; do not introduce code changes as part
  of the clarification ticket.

Acceptance criteria:

1. The corrected ticket names a tracked module path verified by `git ls-tree`.
2. The corrected ticket states whether implementation tests are in scope.
3. The corrected ticket preserves OP-1300's no-code-change proposal constraint
   or explicitly supersedes it with a new implementation ticket.
4. The original OP-1300 ticket is updated with the clarification outcome.

Filed follow-up: pending operator/JIRA creation. This pickup is restricted to
`jira_dispatch.add_comment()` for programmatic JIRA writes.
