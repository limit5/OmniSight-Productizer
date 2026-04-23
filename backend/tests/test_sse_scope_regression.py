"""Q.4 #298 checkbox 4 — SSE scope regression guards.

This file owns the two "belt and braces" regression tests named in
``docs/design/sse-event-scope-policy.md`` §4.3:

* :func:`test_event_scope_declared` — AST-walks every ``.py`` under
  ``backend/`` (production code only) and pins the count of
  ``emit_*(...)`` call sites that still pass no ``broadcast_scope=``
  keyword argument. The Q.4 sweep has not yet migrated all 163
  missing-scope call sites (per §6.1 of
  ``docs/design/multi-device-state-sync.md``), so the test runs as a
  **baseline ratchet** today: any new unscoped emit regresses the
  count, and every sweep PR must decrement the baseline in lock-step.
  When the baseline hits zero this becomes a strict "no unscoped emit
  anywhere" lint.
* :func:`test_user_scope_does_not_leak_across_users` — spins up two
  SSE subscribers bound to different users (same tenant), publishes a
  ``broadcast_scope="user"`` event addressed to user ``u1``, and
  asserts that subscriber ``u2`` never sees the frame. The assertion
  is the actual security contract (the lint above is the proxy).
  Today the test is marked ``xfail(strict=True)`` — ``_deliver_local``
  currently only enforces ``"tenant"`` filtering (``backend/events.py``
  line ~161), so the frame leaks across users at the bus layer. When
  §4.2 of the scope policy lands and the user filter starts dropping
  cross-user frames, this test will XPASS; strict-mode then forces the
  maintainer to strip the xfail marker, promoting the contract to a
  hard gate.

References:
* ``docs/design/sse-event-scope-policy.md`` §4.3 — normative spec.
* ``docs/design/multi-device-state-sync.md`` §6.1 — per-event scope
  audit that produced the ``_BASELINE`` below.
* ``docs/design/multi-device-state-sync.md`` §6.3 — Q.4 sweep
  acceptance hook that demands both guards.
* ``backend/tests/test_emit_scope_enforcement.py`` — the sibling
  checkbox-2 contract test (helper signatures + warn-once + strict
  env). This file is the complementary callsite / delivery layer.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest


# ─── Shared: the 26 emit_* helpers under Q.4 enforcement ─────────────

_EMIT_HELPERS: frozenset[str] = frozenset({
    # backend/events.py
    "emit_agent_entropy",
    "emit_agent_scratchpad_saved",
    "emit_agent_token_continuation",
    "emit_agent_update",
    "emit_chat_message",
    "emit_container",
    "emit_debug_finding",
    "emit_integration_settings_updated",
    "emit_invoke",
    "emit_new_device_login",
    "emit_notification_read",
    "emit_pipeline_phase",
    "emit_preferences_updated",
    "emit_simulation",
    "emit_task_update",
    "emit_token_warning",
    "emit_tool_progress",
    "emit_workflow_updated",
    "emit_workspace",
    # backend/orchestration_observability.py
    "emit_change_awaiting_human",
    "emit_lock_acquired",
    "emit_lock_released",
    "emit_merger_voted",
    "emit_queue_tick",
    # backend/ui_sandbox_sse.py
    "emit_ui_sandbox_error_event",
    "emit_ui_sandbox_screenshot_event",
})


# ─────────────────────────────────────────────────────────────────────
# Test 1 — AST lint (ratchet baseline)
# ─────────────────────────────────────────────────────────────────────

# Known violations as of 2026-04-24 (Q.4 #298 checkbox 4 landing).
#
# Each entry is ``(relative-path-from-repo-root, helper-name) -> count``.
# Any new unscoped emit_*() in backend/ prod code → new entry or bumped
# count → test red. Every Q.4 sweep PR that migrates a call site must
# decrement the matching entry (or remove it when it hits zero).
# When the whole dict is empty, the guard becomes the strict "no
# unscoped emit anywhere" lint spec'd in §6.3 of the multi-device
# sync design doc.
_BASELINE: dict[tuple[str, str], int] = {
    ("backend/agent_hints.py", "emit_debug_finding"): 1,
    ("backend/agents/llm.py", "emit_token_warning"): 1,
    ("backend/agents/nodes.py", "emit_debug_finding"): 3,
    ("backend/agents/nodes.py", "emit_pipeline_phase"): 25,
    ("backend/agents/nodes.py", "emit_token_warning"): 4,
    ("backend/agents/nodes.py", "emit_tool_progress"): 5,
    ("backend/agents/tools.py", "emit_simulation"): 4,
    ("backend/agents/tools.py", "emit_task_update"): 1,
    ("backend/container.py", "emit_agent_update"): 3,
    ("backend/container.py", "emit_container"): 6,
    ("backend/container.py", "emit_pipeline_phase"): 6,
    ("backend/dist_lock.py", "emit_lock_acquired"): 1,
    ("backend/dist_lock.py", "emit_lock_released"): 1,
    ("backend/intent_bridge.py", "emit_invoke"): 2,
    ("backend/merge_arbiter.py", "emit_invoke"): 1,
    ("backend/merger_agent.py", "emit_invoke"): 1,
    ("backend/merger_agent.py", "emit_merger_voted"): 1,
    ("backend/model_router.py", "emit_pipeline_phase"): 2,
    ("backend/orchestration_mode.py", "emit_invoke"): 1,
    ("backend/orchestrator_gateway.py", "emit_invoke"): 1,
    ("backend/orchestrator_gateway.py", "emit_token_warning"): 1,
    ("backend/pipeline.py", "emit_invoke"): 2,
    ("backend/pipeline.py", "emit_pipeline_phase"): 10,
    ("backend/pipeline.py", "emit_task_update"): 1,
    ("backend/pipeline.py", "emit_token_warning"): 1,
    ("backend/routers/agents.py", "emit_agent_update"): 5,
    ("backend/routers/agents.py", "emit_token_warning"): 1,
    ("backend/routers/chat.py", "emit_chat_message"): 1,
    ("backend/routers/chat.py", "emit_pipeline_phase"): 3,
    ("backend/routers/integration.py", "emit_integration_settings_updated"): 1,
    ("backend/routers/integration.py", "emit_invoke"): 1,
    ("backend/routers/invoke.py", "emit_agent_update"): 4,
    ("backend/routers/invoke.py", "emit_invoke"): 9,
    ("backend/routers/invoke.py", "emit_token_warning"): 1,
    ("backend/routers/orchestration_observability.py", "emit_queue_tick"): 1,
    ("backend/routers/preferences.py", "emit_preferences_updated"): 1,
    ("backend/routers/providers.py", "emit_invoke"): 1,
    ("backend/routers/system.py", "emit_notification_read"): 1,
    ("backend/routers/system.py", "emit_token_warning"): 7,
    ("backend/routers/tasks.py", "emit_task_update"): 3,
    ("backend/routers/webhooks.py", "emit_agent_update"): 2,
    ("backend/routers/webhooks.py", "emit_invoke"): 6,
    ("backend/routers/webhooks.py", "emit_task_update"): 2,
    ("backend/scratchpad.py", "emit_agent_scratchpad_saved"): 1,
    ("backend/scratchpad.py", "emit_agent_token_continuation"): 1,
    ("backend/sdk_provisioner.py", "emit_pipeline_phase"): 6,
    ("backend/semantic_entropy.py", "emit_agent_entropy"): 1,
    ("backend/semantic_entropy.py", "emit_debug_finding"): 1,
    ("backend/ssh_runner.py", "emit_pipeline_phase"): 7,
    ("backend/workflow.py", "emit_workflow_updated"): 2,
    ("backend/workspace.py", "emit_agent_update"): 2,
    ("backend/workspace.py", "emit_pipeline_phase"): 3,
    ("backend/workspace.py", "emit_token_warning"): 1,
    ("backend/workspace.py", "emit_workspace"): 3,
}


def _repo_root() -> pathlib.Path:
    """The project root — two ``.parent`` hops up from this file
    (``backend/tests/test_sse_scope_regression.py`` → repo root)."""

    return pathlib.Path(__file__).resolve().parent.parent.parent


def _called_name(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _scan_missing_scope_callsites() -> dict[tuple[str, str], int]:
    """Return ``{(rel_path, helper_name): count}`` for every ``emit_*(...)``
    callsite in ``backend/`` prod code that lacks a ``broadcast_scope=``
    kwarg. Excludes:

    * ``backend/tests/`` — tests are allowed to pass whatever they need.
    * ``def emit_*`` bodies — the helper implementations themselves
      (which call ``bus.publish`` after running through ``_resolve_scope``)
      are not callsites of the helper and have their own Q.4 contract
      covered by ``test_emit_scope_enforcement.py``.
    """

    root = _repo_root()
    backend = root / "backend"
    results: dict[tuple[str, str], int] = {}

    for path in backend.rglob("*.py"):
        rel = str(path.relative_to(root)).replace("\\", "/")
        if rel.startswith("backend/tests/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            # A prod file with a syntax error is a bigger problem than
            # this lint — let the real tests yell about it.
            continue

        # Collect line ranges of emit_* function-def bodies so we can
        # skip self-calls / internal references at the def-site.
        helper_def_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in _EMIT_HELPERS:
                    helper_def_ranges.append(
                        (node.lineno, node.end_lineno or node.lineno)
                    )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name not in _EMIT_HELPERS:
                continue
            # Skip anything inside a ``def emit_*`` body.
            if any(s < node.lineno <= e for s, e in helper_def_ranges):
                continue
            has_scope = any(kw.arg == "broadcast_scope" for kw in node.keywords)
            if has_scope:
                continue
            results[(rel, name)] = results.get((rel, name), 0) + 1

    return results


def test_event_scope_declared():
    """Ratchet: the set of ``emit_*()`` callsites lacking
    ``broadcast_scope=`` must exactly match the 2026-04-24 Q.4 baseline.

    A new unscoped emit (or a bumped count for an existing file/helper
    pair) flags a **regression** — the PR author must either pass
    ``broadcast_scope=`` or, if the call site is genuinely within the
    legacy sweep backlog, update ``_BASELINE`` here alongside a
    reference in the commit message.

    A disappeared entry (or a dropped count) means a legacy call site
    was migrated to pass scope explicitly — great! — and the baseline
    must be decremented to reflect reality, preventing future
    regressions from hiding under stale slack.

    Both directions force the baseline to stay synchronised with the
    code, which is the whole point of a ratchet.
    """

    current = _scan_missing_scope_callsites()

    if current == _BASELINE:
        return

    added: dict[tuple[str, str], int] = {}
    removed: dict[tuple[str, str], int] = {}
    bumped: dict[tuple[str, str], tuple[int, int]] = {}
    dropped: dict[tuple[str, str], tuple[int, int]] = {}

    for key, count in current.items():
        if key not in _BASELINE:
            added[key] = count
        elif count > _BASELINE[key]:
            bumped[key] = (_BASELINE[key], count)
        elif count < _BASELINE[key]:
            dropped[key] = (_BASELINE[key], count)
    for key, count in _BASELINE.items():
        if key not in current:
            removed[key] = count

    def _fmt_entry(key: tuple[str, str], count: int) -> str:
        rel, helper = key
        return f"  ({rel!r}, {helper!r}): {count},"

    lines: list[str] = [
        "emit_*() broadcast_scope= baseline drift (Q.4 #298 §4.3):",
    ]
    if added:
        lines.append("")
        lines.append(f"REGRESSION — {len(added)} new unscoped call site(s):")
        for key, count in sorted(added.items()):
            lines.append(_fmt_entry(key, count))
        lines.append(
            "  → add broadcast_scope= per "
            "docs/design/sse-event-scope-policy.md §2 rubric, "
            "OR append to _BASELINE if the site genuinely belongs to the "
            "backlog."
        )
    if bumped:
        lines.append("")
        lines.append(f"REGRESSION — {len(bumped)} bumped count(s):")
        for key, (old, new) in sorted(bumped.items()):
            lines.append(f"  {key}: {old} → {new}")
    if dropped:
        lines.append("")
        lines.append(
            f"Progress — {len(dropped)} call site(s) gained scope; "
            "decrement baseline:"
        )
        for key, (old, new) in sorted(dropped.items()):
            lines.append(f"  {key}: {old} → {new}")
    if removed:
        lines.append("")
        lines.append(
            f"Progress — {len(removed)} file/helper pair(s) fully "
            "migrated; remove from baseline:"
        )
        for key, count in sorted(removed.items()):
            lines.append(_fmt_entry(key, count))

    raise AssertionError("\n".join(lines))


def test_event_scope_baseline_entries_are_canonical():
    """The baseline keys must reference real, scannable prod paths.

    Guards against typos / stale paths in ``_BASELINE`` that would let
    a genuine regression sneak past the ratchet (a typo'd key counts as
    zero on both sides of the equality check).
    """

    root = _repo_root()
    for rel, helper in _BASELINE:
        path = root / rel
        assert path.is_file(), f"_BASELINE path does not exist: {rel}"
        assert helper in _EMIT_HELPERS, (
            f"_BASELINE helper not in _EMIT_HELPERS set: {helper!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Test 2 — Cross-user delivery isolation
# ─────────────────────────────────────────────────────────────────────

# Strict xfail: the assertion below is the Q.4 §4.3 security contract
# for user-scoped SSE delivery. Today `EventBus._deliver_local` only
# enforces the "tenant" filter (events.py:161), so a `broadcast_scope=
# "user"` frame addressed to u1 is still fanned out to u2's SSE queue.
# Once §4.2 lands — `subscribe(user_id=...)` + a user filter mirroring
# the tenant one — this test flips to XPASS, strict-mode turns the
# suite red, and the maintainer must remove the xfail marker as the
# final step of that landing. That flip is the whole point of the
# regression guard: it promotes the contract to a hard gate at exactly
# the right moment.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "_deliver_local() currently only filters 'tenant'; user-scoped "
        "frames still leak to every subscriber. Remove this marker when "
        "§4.2 of docs/design/sse-event-scope-policy.md lands "
        "(subscribe(user_id=...) + matching user filter in "
        "EventBus._deliver_local)."
    ),
)
def test_user_scope_does_not_leak_across_users():
    """User ``u1``'s event must not be delivered to user ``u2``'s SSE.

    Scenario:
      * Two SSE subscribers, same tenant ``t-acme``, different users
        (``u1`` and ``u2``). Both legitimately connected — e.g. two
        operators in the same org on separate browsers.
      * Backend emits a ``chat.message`` frame addressed to user
        ``u1`` (same pattern as :func:`backend.events.emit_chat_message`).
      * Server-side delivery must drop the frame for ``u2``'s queue.
        Advisory payload-side filtering in the frontend is insufficient
        because the content (chat tokens, notification bodies,
        preference values) is already readable before the browser
        checks ``data.user_id``.
    """

    from backend.events import EventBus

    bus = EventBus()

    # Aspirational API — ``user_id=`` is not yet accepted by
    # ``subscribe`` today. TypeError on the next line is expected
    # until the filter lands; it is caught by the xfail marker.
    q_u1 = bus.subscribe(tenant_id="t-acme", user_id="u1")
    q_u2 = bus.subscribe(tenant_id="t-acme", user_id="u2")

    bus.publish(
        "chat.message",
        {
            "id": "m-1",
            "user_id": "u1",
            "role": "user",
            "content": "private to u1",
            "ts": "2026-04-24T00:00:00+00:00",
        },
        broadcast_scope="user",
        tenant_id="t-acme",
        user_id="u1",
    )

    # u1: sees own frame.
    assert not q_u1.empty(), "u1 did not receive its own user-scoped frame"
    msg_u1 = q_u1.get_nowait()
    payload_u1 = json.loads(msg_u1["data"])
    assert payload_u1["id"] == "m-1"
    assert payload_u1["user_id"] == "u1"
    assert payload_u1["_broadcast_scope"] == "user"

    # u2: must NOT see u1's frame. This is the actual cross-user
    # isolation contract.
    assert q_u2.empty(), (
        "cross-user leak: u2 received u1's user-scoped frame — "
        "EventBus._deliver_local must drop frames where "
        "broadcast_scope='user' and data.user_id != subscriber.user_id"
    )

    bus.unsubscribe(q_u1)
    bus.unsubscribe(q_u2)
