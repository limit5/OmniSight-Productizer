"""T11 readiness gate: false-allow=0 corpus for runner_sdk:code_write under ENFORCE.

Proves the classifier is safe to flip shadow->enforce for (runner_sdk, code_write): every MUTATING or evasive
code_write tool call is BLOCKED (proceed=False -> requires_grant challenge), and the ONLY calls that proceed are
genuine reads (str_replace_based_edit_tool command=view -- the single allowlisted looser refinement, a real read in
TextEditorHandler._cmd_view).  A "false-allow" is a mutation-capable op that proceeds; this asserts the count is 0.

This is the T11 evidence artifact for the enforce flip.  It runs the FULL guard (guard_tool_dispatch) under the real
enforce config, mirroring backend/tests/test_action_guard_enforce_refine.py's harness.

SCOPE: runner_sdk:code_write=enforce governs ONLY the code_write family.  Other runner_sdk mutating families
(memory_write, delegation, task_write, external_comms, skill_exec, ...) stay SHADOW under this key and still proceed --
they are governed only when THEIR family is separately flipped (T11 is family-by-family).  See
test_enforce_scope_is_code_write_only_not_adapter_wide.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.agents import action_canonicalize
from backend.agents import action_guard
from backend.agents import canonicalize_code_write_file
from backend.agents import shadow_root_registry
from backend.agents.action_guard import GuardOutcome
from backend.agents.action_guard import guard_tool_dispatch
from backend.agents.execution_context import ExecutionContext
from backend.agents.execution_context import for_service
from backend.agents.execution_context import for_unbound

_ADAPTER = "runner_sdk"
_SCHEMA = "v1"
_WRITE_TOOLS = ("Write", "Edit", "str_replace_based_edit_tool")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore every process-local registry + mode, and ENFORCE runner_sdk:code_write for the whole module."""
    canonical_state = action_canonicalize._snapshot_state_for_tests()
    bootstrap_ok = action_guard._CANONICAL_BOOTSTRAP_OK
    action_canonicalize.reset_for_tests()
    canonicalize_code_write_file.register_code_write_file_canonicalizers()
    action_canonicalize.freeze_registry()
    shadow_root_registry.reset_for_tests()
    action_guard._CANONICAL_BOOTSTRAP_OK = True
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", "runner_sdk:code_write=enforce")
    action_guard.reload_mode_matrix_for_tests()
    try:
        yield
    finally:
        monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
        action_guard.reload_mode_matrix_for_tests()
        shadow_root_registry.reset_for_tests()
        action_canonicalize._restore_state_for_tests(canonical_state)
        action_guard._CANONICAL_BOOTSTRAP_OK = bootstrap_ok


def _bound_context() -> ExecutionContext:
    return for_service(
        service_name="runner-launcher",
        tenant_id="t-default",
        request_id="r1",
        roles=("runner",),
        authorization_source="server_launcher",
    )


def _publish_roots(root: Path) -> None:
    for tool in _WRITE_TOOLS:
        shadow_root_registry.register_shadow_root(_ADAPTER, tool, _SCHEMA, workspace_root=str(root))
    shadow_root_registry.freeze_registry()


def _guard(root: Path, tool_name: str, raw_args: dict) -> GuardOutcome:
    return guard_tool_dispatch(
        adapter_namespace=_ADAPTER,
        tool_name=tool_name,
        schema_version=_SCHEMA,
        raw_args=raw_args,
        execution_context=_bound_context(),
    )


def _mutated(outcome: GuardOutcome) -> bool:
    """A mutation would be permitted iff the guard let the call proceed (an allow / sealed dispatch)."""
    return outcome.proceed is True


# ── The corpus: (label, tool, args-factory(root), is_genuine_read) ──────────────────────────────────────────────
# is_genuine_read=True  => must proceed (a real read; the sole allowlisted looser refinement).
# is_genuine_read=False => must BLOCK (mutation-capable / evasive / malformed); proceeding is a FALSE-ALLOW.

def _legit_reads(root: Path):
    v = str(root / "view.py")
    return [
        ("view_plain", "str_replace_based_edit_tool", {"command": "view", "path": v}),
        ("view_with_range", "str_replace_based_edit_tool", {"command": "view", "path": v, "view_range": [1, 5]}),
        # A view carrying mutating-LOOKING extra keys is STILL a read (the real handler only reads); not a false-allow.
        ("view_with_decoy_mutate_keys", "str_replace_based_edit_tool",
         {"command": "view", "path": v, "old_str": "x", "new_str": "y", "file_text": "z"}),
    ]


def _legit_mutations(root: Path):
    f = str(root / "m.py")
    return [
        ("write", "Write", {"file_path": f, "content": "print(1)\n"}),
        ("edit", "Edit", {"file_path": f, "old_string": "a", "new_string": "b"}),
        ("sr_create", "str_replace_based_edit_tool", {"command": "create", "path": f, "file_text": "x\n"}),
        ("sr_replace", "str_replace_based_edit_tool",
         {"command": "str_replace", "path": f, "old_str": "a", "new_str": "b"}),
        ("sr_insert", "str_replace_based_edit_tool",
         {"command": "insert", "path": f, "insert_line": 1, "insert_text": "y\n"}),
    ]


def _adversarial(root: Path):
    inside = str(root / "a.py")
    outside_abs = "/etc/passwd"
    outside_rel = str(root / ".." / ".." / "etc" / "passwd")
    big = "x" * (300_000)
    return [
        # Bash has NO canonicalizer -> name-only mutating -> blocked; even mimicking a "view" command cannot refine it.
        ("bash_plain", "Bash", {"command": "ls"}),
        ("bash_mimics_view", "Bash", {"command": "view", "path": inside}),
        # str_replace evasions: undo_edit / unknown / case / whitespace commands must NOT reach the view refinement.
        ("sr_undo_edit", "str_replace_based_edit_tool", {"command": "undo_edit", "path": inside}),
        ("sr_unknown_cmd", "str_replace_based_edit_tool", {"command": "delete", "path": inside}),
        ("sr_view_uppercase", "str_replace_based_edit_tool", {"command": "VIEW", "path": inside}),
        ("sr_view_trailing_space", "str_replace_based_edit_tool", {"command": "view ", "path": inside}),
        ("sr_view_leading_space", "str_replace_based_edit_tool", {"command": " view", "path": inside}),
        ("sr_view_tab", "str_replace_based_edit_tool", {"command": "view\t", "path": inside}),
        ("sr_view_newline", "str_replace_based_edit_tool", {"command": "view\n", "path": inside}),
        ("sr_view_nfkc_fullwidth", "str_replace_based_edit_tool", {"command": "ｖiew", "path": inside}),  # ｖiew
        ("sr_view_cyrillic_homoglyph", "str_replace_based_edit_tool", {"command": "viеw", "path": inside}),  # viеw
        ("sr_view_nul", "str_replace_based_edit_tool", {"command": "view\x00", "path": inside}),
        ("sr_command_nonstring", "str_replace_based_edit_tool", {"command": ["view"], "path": inside}),
        ("sr_command_none", "str_replace_based_edit_tool", {"command": None, "path": inside}),
        ("sr_command_missing", "str_replace_based_edit_tool", {"path": inside}),
        # Path escapes on a mutating command -> canonicalization_rejected (never a proceed).
        ("write_abs_escape", "Write", {"file_path": outside_abs, "content": "pwn"}),
        ("write_rel_escape", "Write", {"file_path": outside_rel, "content": "pwn"}),
        ("sr_create_escape", "str_replace_based_edit_tool",
         {"command": "create", "path": outside_abs, "file_text": "x"}),
        # A read (view) that escapes the workspace must also block (fail-closed before the read refinement seals).
        ("view_abs_escape", "str_replace_based_edit_tool", {"command": "view", "path": outside_abs}),
        # Malformed / missing / hostile args on mutating tools -> fail closed.
        ("write_empty_path", "Write", {"file_path": "", "content": "x"}),
        ("write_missing_content", "Write", {"file_path": inside}),
        ("write_nonstring_content", "Write", {"file_path": inside, "content": 123}),
        ("write_surrogate_content", "Write", {"file_path": inside, "content": "\ud800"}),
        ("write_oversized", "Write", {"file_path": inside, "content": big}),
        ("edit_noop", "Edit", {"file_path": inside, "old_string": "a", "new_string": "a"}),
        # Unknown tool for the adapter -> __unknown_deny__ -> enforce block.
        ("unknown_tool", "DeleteEverything", {"path": inside}),
    ]


def test_enforce_corpus_false_allow_is_zero(tmp_path: Path) -> None:
    _publish_roots(tmp_path)
    reads = _legit_reads(tmp_path)
    mutations = _legit_mutations(tmp_path)
    adversarial = _adversarial(tmp_path)

    false_allows: list[str] = []
    false_blocks: list[str] = []

    # 1. Genuine reads MUST proceed (allowed as read_only); a block here is a false-BLOCK (over-refusal, not a security
    #    hole, but tracked so the flip does not needlessly break real view reads).
    for label, tool, args in reads:
        outcome = _guard(tmp_path, tool, args)
        if not outcome.proceed:
            false_blocks.append(label)
        else:
            assert outcome.decision is not None and outcome.decision.verdict == "allow", label
            assert outcome.family == "read_only", label
            assert outcome.mode == "enforce", label

    # 2. Every mutation-capable / evasive / malformed call MUST block.  A proceed here is a FALSE-ALLOW (the security
    #    property under test).  Legit mutations additionally carry a challenge (requires_grant); adversarial ones may
    #    block via requires_grant OR canonicalization_rejected -- either is a correct refusal.
    for label, tool, args in mutations:
        outcome = _guard(tmp_path, tool, args)
        if _mutated(outcome):
            false_allows.append(label)
        else:
            assert outcome.decision is None or outcome.decision.verdict in {"requires_grant", "deny"}, label
    for label, tool, args in adversarial:
        outcome = _guard(tmp_path, tool, args)
        if _mutated(outcome):
            false_allows.append(label)

    assert false_allows == [], f"FALSE-ALLOW (mutating op proceeded under enforce): {false_allows}"
    assert false_blocks == [], f"false-block (genuine view read refused): {false_blocks}"


def test_legit_mutations_produce_a_challenge_not_a_silent_drop(tmp_path: Path) -> None:
    # A blocked legit mutation must carry the sealed challenge (so a human can confirm), not vanish.
    _publish_roots(tmp_path)
    for label, tool, args in _legit_mutations(tmp_path):
        outcome = _guard(tmp_path, tool, args)
        assert outcome.proceed is False, label
        assert outcome.decision is not None and outcome.decision.verdict == "requires_grant", label
        assert outcome.challenge_prepared is not None, label
        assert outcome.family == "code_write", label


def test_a_stage1_exception_fails_closed_not_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The highest-value safety edge: if the kernel authorize path RAISES, the guard must fail CLOSED (proceed=False)
    # under enforce -- never fail open. resolve_mode(adapter, "__error__") returns enforce for runner_sdk (it has an
    # enforce entry), so both a would-be read (view) and a mutation block.
    _publish_roots(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("injected kernel fault")

    monkeypatch.setattr(action_guard, "authorize_action", _boom)
    for tool, args in (
        ("str_replace_based_edit_tool", {"command": "view", "path": str(tmp_path / "v.py")}),
        ("Write", {"file_path": str(tmp_path / "m.py"), "content": "x"}),
    ):
        outcome = _guard(tmp_path, tool, args)
        assert outcome.proceed is False, tool
        assert outcome.family == "__error__", tool
        assert outcome.mode == "enforce", tool


def test_unbound_principal_denies_view_and_mutation(tmp_path: Path) -> None:
    # Without a bound principal, even the read refinement must not proceed: the name-stage verdict is deny
    # (mutating + unbound), and the enforce refinement only runs on a requires_grant verdict -- so an unbound view
    # never reaches the read_only allow.
    _publish_roots(tmp_path)
    for tool, args in (
        ("str_replace_based_edit_tool", {"command": "view", "path": str(tmp_path / "v.py")}),
        ("Write", {"file_path": str(tmp_path / "m.py"), "content": "x"}),
    ):
        outcome = guard_tool_dispatch(
            adapter_namespace=_ADAPTER,
            tool_name=tool,
            schema_version=_SCHEMA,
            raw_args=args,
            execution_context=for_unbound(),
        )
        assert outcome.proceed is False, tool


def test_enforce_scope_is_code_write_only_not_adapter_wide(tmp_path: Path) -> None:
    # SCOPE CAVEAT (documented, intentional): runner_sdk:code_write=enforce governs ONLY the code_write family.
    # Other runner_sdk mutating families (memory_write, delegation, task_write, ...) stay SHADOW under this key and
    # still PROCEED -- they are governed only when THEIR family is separately flipped (T11 is family-by-family). This
    # test pins that the flip is narrow, so it is never mistaken for adapter-wide coverage.
    _publish_roots(tmp_path)
    for tool in ("memory", "Agent", "create_task"):
        outcome = _guard(tmp_path, tool, {"anything": "x"})
        assert outcome.proceed is True, f"{tool} should stay shadow (not enforced by the code_write key)"
        assert outcome.mode == "shadow", tool
