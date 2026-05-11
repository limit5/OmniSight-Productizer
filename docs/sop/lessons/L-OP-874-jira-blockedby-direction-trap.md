---
id: L-OP-874
ticket: OP-874
title: REST parameter names that look semantic are not — wrap with intent-named helpers
date: 2026-05-11
tags: [jira, rest-api, operator-tooling, runner, fault-tolerance, governance]
related_tickets: [OP-858, OP-866, OP-867, OP-852, OP-856, OP-843]
---

# REST parameter names that look semantic are not — wrap with intent-named helpers

**Situation**: Gerrit #387 (OP-858, 2026-05-11) failed to apply against
``develop`` after the runner picked up the ticket and built it against a
stale base. Investigation showed every wire script that had filed
Sprint C / Phase 24 tickets (``/tmp/wire_phase24.py``,
``/tmp/wire_sprint_c.py``) was POSTing ``/issueLink`` with
``inwardIssue=blocked, outwardIssue=blocker`` — the *opposite* of the
Atlassian-documented mapping.

The codebase's own ``backend/agents/file_coordinator.py::jira_create_issue_link``
documented the correct direction in its docstring ("``inward`` blocks
``outward``") but its call-time signature took ``inward=`` / ``outward=``
parameter names — and those names read both ways:

| Reading                                                                | Implied mapping            |
|------------------------------------------------------------------------|----------------------------|
| "inward = the issue going *inward* to the relation"                    | inward = blocked, outward = blocker |
| "inward = the issue described by the *inward direction name* (``is blocked by``)" | inward = blocker, outward = blocked |

The wire scripts picked the first reading. Both readings are plausible
until you read Atlassian's REST docs carefully — POST
``{inwardIssue: A, outwardIssue: B, type: Blocks}`` creates the link
``A blocks B``, so the second reading is the correct one. The first
reading is wrong but ships clean code review because the variable names
look semantic.

With direction inverted, the runner's pre-pickup gate
(``has_unresolved_blockedby``) walked the blocker side of the link and
found a "blocker" already published, so it cleared OP-858 for pickup
against stale develop. Five tickets carried the inverted direction at
incident time (OP-852/856/858/866/867); the first three were already
merged (harmless), the last two were pickable and had to be fixed by
hand.

**Fix**:

1. **Intent-named helper** ``backend/agents/file_coordinator.py::add_blocked_by(client,
   blocked_key, blocker_key, *, reason=None)`` — operator code can no
   longer get the direction wrong because the parameter names *are* the
   semantics. Wraps the deprecated low-level helper with
   ``inward=blocker_key, outward=blocked_key`` exactly once, in a place
   pinned by tests. Idempotent + self-link defensive. Emits a
   ``[blocked-by-link]`` comment that captures operator intent on the
   blocked ticket.

2. **Audit script** ``scripts/audit_blockedby_directions.py`` — walks
   every open OP ticket's ``Blocks`` links, recovers operator intent
   from the ``[blocked-by-link]`` / ``[file-coordinator]`` comment
   markers, and flags any link whose API direction contradicts intent.
   ``--fix`` deletes + recreates the inverted link, with a rollback JSON
   so the operator can mirror-restore the original direction if a
   downstream system relied on it.

3. **Lint rule** ``scripts/check_issuelink_post_callers.py`` — fails the
   build when any new code outside ``backend/agents/file_coordinator.py``
   POSTs ``/issueLink`` directly. CI drift guard so the trap cannot be
   re-introduced by the next operator script.

4. **Fail-closed env knob** ``OMNISIGHT_BLOCKEDBY_FAIL_CLOSED`` —
   independent of the helper, but cuts blast radius if any inversion
   slips past the lint. Set to ``1`` and a wedged JIRA refuses pickups
   instead of silently bypassing the gate. Default OFF so prod
   behaviour is unchanged until the operator opts in.

5. **Docs** §19 of ``docs/sop/jira-ticket-conventions.md`` — concrete
   example + anti-pattern callout so the next operator finds the helper
   before reaching for the raw schema.

**Verification**:

* ``backend/tests/test_blockedby_helper.py`` — 10 test cases pin the
  helper's API direction, idempotency, self-link guard, audit-mode
  detection of inverted links, ``--fix`` correction + rollback emission,
  the fail-closed env knob behaviour on both exception branches, the
  lint rule firing on a fresh non-allowlisted caller, the lint rule's
  ``file_coordinator.py`` allowlist, and the deprecation notice in
  ``jira_create_issue_link``'s docstring.

* ``scripts/check_issuelink_post_callers.py --verbose`` runs clean on
  the current tree (no raw callers outside ``file_coordinator.py``).

* ``scripts/audit_blockedby_directions.py`` produces a parseable report
  at ``docs/audit/blockedby-direction-audit-<date>.md``; the five
  historical inverted links are either already merged (harmless) or
  were fixed by hand in 2026-05-11.

**Generalisation**:

1. **REST API parameter names that look semantic are not.** Any external
   API where two parameters take symmetric values (``inward``/``outward``,
   ``source``/``target``, ``parent``/``child``, ``from``/``to``) carries
   this trap. The author of the calling code has to *remember* which
   reading the API uses; reviewers can't tell from the call site. The
   bug surfaces months later via a silent-failure mode (here: a pre-
   pickup gate that walks the wrong side).

2. **Wrap with intent-named helpers, then deprecate the raw call site.**
   The discipline: every place that has to spell out the ambiguous
   parameter names exists exactly once in the codebase, and is pinned by
   a test that asserts the spelling. All other operator code is forced
   through an intent-named wrapper (``add_blocked_by(blocked_key,
   blocker_key)`` here). A lint rule catches new raw callers at PR
   time. This pattern generalises: the same shape fits Atlassian
   ``issueLink``, GitHub ``ref/object``, Gerrit ``base/branch``, any
   graph-edge or directed-relation API.

3. **Audit *before* trusting the planning layer.** A gate that reads
   from a graph is only as trustworthy as the graph's direction
   invariant. Before assuming the gate works, run an audit that re-
   derives the invariant from a different source (here: comment markers
   capturing operator intent). The audit is the only thing that catches
   wrong-direction links that the gate happily walks past.

4. **Fail-closed knobs are cheap to ship and disabled by default.** A
   fail-closed mode for any "skipped because the upstream errored" path
   is a one-line guard that lets operators flip prod into safe mode
   during an incident without redeploying. Ship the knob OFF so
   behaviour is unchanged; the operator turns it on once they trust the
   audit + helper. (Same pattern shape as L-OP-742, where test-impact
   analysis ships fail-closed-on-the-conservative-side.)

5. **Cross-reference**: L-OP-843 (vendor-claim disambiguation
   discipline) is the sister lesson on the *vendor* side of "parameter
   names that look semantic are not". Both ask the operator to read the
   actual contract before acting on the surface reading; both ship a
   forcing function (independent audit / intent-named wrapper) so the
   discipline survives staff turnover.

Per CLAUDE.md L1, this lesson lands as a per-file entry under
``docs/sop/lessons/`` rather than a one-line cell in a tickets file;
the discipline it codifies — *wrap ambiguous external-API parameter
names with intent-named helpers before exposing them to operator code*
— is reusable across every future REST-graph integration.
