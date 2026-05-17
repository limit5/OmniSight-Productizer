# OP-1337 chatops refactor proposal

## Context

OP-1337 targets `backend/agents/chatops.py` for a small readability or coupling
refactor proposal. The required pre-flight check failed: this repository does
not contain `backend/agents/chatops.py`.

The ChatOps implementation appears to live in the existing R1 modules
`backend/chatops_bridge.py`, `backend/routers/chatops.py`, and
`backend/chatops/{discord,line,teams}.py`. Those files are plausible future
targets, but they are not the ticket's scope anchor, so this patch set abstains
from proposing implementation refactors against them.

## Proposal

Do not implement a backend refactor from OP-1337 as written. First retarget the
follow-up implementation ticket to the concrete ChatOps module that should be
reviewed:

1. `backend/chatops_bridge.py` if the intended target is the shared inbound /
   outbound dispatch and mirror buffer.
2. `backend/routers/chatops.py` if the intended target is FastAPI route shape
   and request handling.
3. `backend/chatops/{discord,line,teams}.py` if the intended target is adapter
   duplication across providers.

Once the target is corrected, the next pickup can read that module and identify
1-2 small behavior-preserving refactors with tests.

## Non-goals

- No code changes in this proposal patch set.
- No test changes in this proposal patch set.
- No refactor proposal for modules outside the stated `backend/agents/chatops.py`
  scope anchor.
- No db, devops, embedded, frontend, security, tests, tooling, or out-of-scope
  domain changes.

## Follow-up implementation ticket draft

Summary: Retarget and implement ChatOps module readability refactor

Areas: backend, tests

Tier: S

Description:

OP-1337 could not propose an implementation refactor because the requested
module, `backend/agents/chatops.py`, is absent. Pick the concrete ChatOps target
module before implementation:

- `backend/chatops_bridge.py` for dispatch, authorization, mirror buffer, and
  SSE/audit fan-out.
- `backend/routers/chatops.py` for route handlers and request/response flow.
- `backend/chatops/{discord,line,teams}.py` for provider adapter duplication.

After selecting the target, identify and implement 1-2 small behavior-preserving
refactors that reduce coupling or improve readability. Preserve public route,
adapter, and bridge behavior.

Acceptance criteria:

1. The implementation ticket names an existing ChatOps module path before code
   changes begin.
2. Public ChatOps route paths, adapter function names, and bridge entry points
   remain compatible.
3. Existing ChatOps behavior is preserved for outbound send, inbound dispatch,
   mirror/status endpoints, and authorized inject handling relevant to the
   chosen target.
4. The relevant ChatOps pytest file passes, selected from
   `backend/tests/test_chatops_bridge.py`, `backend/tests/test_chatops_router.py`,
   or `backend/tests/test_chatops_adapters.py`.

Filed follow-up: blocked by OP-1337 pickup write restriction. This pickup allows
programmatic JIRA writes only through `jira_dispatch.add_comment()` /
`transition_back_to_todo()`, and those helpers do not create new tickets.
