"""RPG.W15.2 — coverage + signature-annotation drift guard for
``backend/agents/debuff_registry.py``.

ADR-0008 keeps the W15 debuff registry a pure, deterministic surface:
immutable definitions plus stateless helpers. Two things must stay
true over time:

1. The public API behaves deterministically for the documented
   inputs (Burnout streak threshold, Stale Memory idle window, the
   combined XP / routing-weight multipliers, and the error paths
   for unknown / malformed debuff ids).

2. Every function signature in the module remains fully annotated.
   The module is currently at 100% parameter + return coverage; this
   test pins that so a future edit that drops an annotation fails CI
   instead of slipping through silently. This is the equivalent of
   the mypy-or-basic-checks step asked for by OP-1219 (mypy is not
   in the runner image, so an AST walk is the portable substitute).

A failure here means either the registry behaviour drifted, or a new
``def`` landed without complete type hints. The fix is to restore the
behaviour or add the missing annotation — never to relax the guard.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from backend.agents import debuff_registry
from backend.agents.debuff_registry import (
    BURNOUT_DEBUFF_ID,
    BURNOUT_FAILURE_COUNT,
    BURNOUT_XP_MULTIPLIER,
    DEBUFF_DEFINITIONS,
    STALE_MEMORY_DEBUFF_ID,
    STALE_MEMORY_IDLE_SECONDS,
    STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER,
    DebuffContext,
    DebuffDefinition,
    UnknownDebuffError,
    active_debuff_ids_for_context,
    active_debuffs_for_context,
    get_debuff_definition,
    list_debuff_definitions,
    routing_weight_multiplier_for_context,
    routing_weight_multiplier_for_last_retrained_at,
    xp_multiplier_for_context,
    xp_multiplier_for_debuff_ids,
)


# ── registry shape ──────────────────────────────────────────────────


def test_registry_is_immutable_mapping_proxy() -> None:
    assert isinstance(DEBUFF_DEFINITIONS, MappingProxyType)
    with pytest.raises(TypeError):
        DEBUFF_DEFINITIONS["new"] = DebuffDefinition(  # type: ignore[index]
            debuff_id="new",
            display_name="New",
            kind="xp",
            multiplier=1.0,
            summary="",
        )


def test_registry_contains_documented_debuffs() -> None:
    assert set(DEBUFF_DEFINITIONS) == {BURNOUT_DEBUFF_ID, STALE_MEMORY_DEBUFF_ID}


def test_public_exports_remain_stable() -> None:
    assert debuff_registry.__all__ == [
        "BURNOUT_DEBUFF_ID",
        "BURNOUT_FAILURE_COUNT",
        "BURNOUT_XP_MULTIPLIER",
        "DEBUFF_DEFINITIONS",
        "STALE_MEMORY_DEBUFF_ID",
        "STALE_MEMORY_IDLE_SECONDS",
        "STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER",
        "DebuffContext",
        "DebuffDefinition",
        "DebuffKind",
        "DebuffOutcomeStatus",
        "UnknownDebuffError",
        "active_debuff_ids_for_context",
        "active_debuffs_for_context",
        "get_debuff_definition",
        "list_debuff_definitions",
        "routing_weight_multiplier_for_context",
        "routing_weight_multiplier_for_last_retrained_at",
        "xp_multiplier_for_context",
        "xp_multiplier_for_debuff_ids",
    ]


def test_list_debuff_definitions_preserves_registry_order() -> None:
    listed = list_debuff_definitions()
    assert tuple(d.debuff_id for d in listed) == tuple(DEBUFF_DEFINITIONS)


def test_get_debuff_definition_returns_registry_entry() -> None:
    burnout = get_debuff_definition(BURNOUT_DEBUFF_ID)
    assert burnout is DEBUFF_DEFINITIONS[BURNOUT_DEBUFF_ID]
    assert burnout.kind == "xp"
    assert burnout.multiplier == BURNOUT_XP_MULTIPLIER

    stale = get_debuff_definition(STALE_MEMORY_DEBUFF_ID)
    assert stale.kind == "routing_weight"
    assert stale.multiplier == STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER


def test_get_debuff_definition_trims_whitespace() -> None:
    assert (
        get_debuff_definition(f"  {BURNOUT_DEBUFF_ID}  ")
        is DEBUFF_DEFINITIONS[BURNOUT_DEBUFF_ID]
    )


def test_get_debuff_definition_unknown_id_raises_unknown_debuff_error() -> None:
    with pytest.raises(UnknownDebuffError):
        get_debuff_definition("does-not-exist")


def test_get_debuff_definition_rejects_empty_id() -> None:
    with pytest.raises(ValueError):
        get_debuff_definition("   ")


def test_get_debuff_definition_rejects_non_string_id() -> None:
    with pytest.raises(TypeError):
        get_debuff_definition(123)  # type: ignore[arg-type]


# ── burnout behaviour ───────────────────────────────────────────────


def test_burnout_inactive_below_failure_threshold() -> None:
    context = DebuffContext(consecutive_failures=BURNOUT_FAILURE_COUNT - 1)
    assert active_debuff_ids_for_context(context) == ()
    assert xp_multiplier_for_context(context) == 1.0


def test_burnout_active_at_failure_threshold() -> None:
    context = DebuffContext(consecutive_failures=BURNOUT_FAILURE_COUNT)
    assert BURNOUT_DEBUFF_ID in active_debuff_ids_for_context(context)
    assert xp_multiplier_for_context(context) == pytest.approx(BURNOUT_XP_MULTIPLIER)


def test_burnout_cleared_when_streak_reset() -> None:
    context = DebuffContext(consecutive_failures=0)
    assert active_debuffs_for_context(context) == ()


def test_validate_context_rejects_negative_failure_streak() -> None:
    with pytest.raises(ValueError):
        active_debuffs_for_context(DebuffContext(consecutive_failures=-1))


# ── stale memory behaviour ──────────────────────────────────────────


_NOW = datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_stale_memory_requires_both_now_and_last_retrained_at() -> None:
    assert active_debuffs_for_context(DebuffContext(now=_NOW)) == ()
    assert (
        active_debuffs_for_context(DebuffContext(last_retrained_at=_NOW)) == ()
    )


def test_stale_memory_inactive_before_idle_window() -> None:
    recently_retrained = _NOW - timedelta(seconds=STALE_MEMORY_IDLE_SECONDS - 1)
    context = DebuffContext(now=_NOW, last_retrained_at=recently_retrained)
    assert active_debuff_ids_for_context(context) == ()
    assert routing_weight_multiplier_for_context(context) == 1.0


def test_stale_memory_active_at_idle_threshold() -> None:
    stale_retrained = _NOW - timedelta(seconds=STALE_MEMORY_IDLE_SECONDS)
    context = DebuffContext(now=_NOW, last_retrained_at=stale_retrained)
    assert STALE_MEMORY_DEBUFF_ID in active_debuff_ids_for_context(context)
    assert routing_weight_multiplier_for_context(context) == pytest.approx(
        STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER
    )


def test_routing_weight_helper_matches_context_path() -> None:
    stale_retrained = _NOW - timedelta(seconds=STALE_MEMORY_IDLE_SECONDS)
    assert routing_weight_multiplier_for_last_retrained_at(
        _NOW, stale_retrained
    ) == routing_weight_multiplier_for_context(
        DebuffContext(now=_NOW, last_retrained_at=stale_retrained)
    )


def test_routing_weight_helper_handles_naive_datetimes() -> None:
    naive_now = datetime(2026, 5, 17)
    naive_old = naive_now - timedelta(seconds=STALE_MEMORY_IDLE_SECONDS)
    assert routing_weight_multiplier_for_last_retrained_at(
        naive_now, naive_old
    ) == pytest.approx(STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER)


def test_routing_weight_helper_no_retraining_history() -> None:
    assert routing_weight_multiplier_for_last_retrained_at(_NOW, None) == 1.0


# ── combined multipliers ────────────────────────────────────────────


def test_xp_multiplier_for_debuff_ids_combines_xp_kind_only() -> None:
    assert xp_multiplier_for_debuff_ids(()) == 1.0
    assert xp_multiplier_for_debuff_ids(
        (BURNOUT_DEBUFF_ID,)
    ) == pytest.approx(BURNOUT_XP_MULTIPLIER)
    # routing_weight kind must not contribute to the XP multiplier
    assert xp_multiplier_for_debuff_ids((STALE_MEMORY_DEBUFF_ID,)) == 1.0


def test_combined_burnout_and_stale_memory_keep_separate_axes() -> None:
    stale_retrained = _NOW - timedelta(seconds=STALE_MEMORY_IDLE_SECONDS)
    context = DebuffContext(
        consecutive_failures=BURNOUT_FAILURE_COUNT,
        now=_NOW,
        last_retrained_at=stale_retrained,
    )
    active_ids = active_debuff_ids_for_context(context)
    assert BURNOUT_DEBUFF_ID in active_ids
    assert STALE_MEMORY_DEBUFF_ID in active_ids
    assert xp_multiplier_for_context(context) == pytest.approx(BURNOUT_XP_MULTIPLIER)
    assert routing_weight_multiplier_for_context(context) == pytest.approx(
        STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER
    )


def test_active_debuff_rule_order_is_burnout_then_stale_memory() -> None:
    stale_retrained = _NOW - timedelta(seconds=STALE_MEMORY_IDLE_SECONDS)
    context = DebuffContext(
        consecutive_failures=BURNOUT_FAILURE_COUNT,
        now=_NOW,
        last_retrained_at=stale_retrained,
    )

    assert active_debuff_ids_for_context(context) == (
        BURNOUT_DEBUFF_ID,
        STALE_MEMORY_DEBUFF_ID,
    )


@pytest.mark.parametrize(
    ("debuff_ids", "expected_xp", "expected_routing_weight"),
    [
        ((), 1.0, 1.0),
        ((BURNOUT_DEBUFF_ID,), BURNOUT_XP_MULTIPLIER, 1.0),
        ((STALE_MEMORY_DEBUFF_ID,), 1.0, STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER),
        (
            (BURNOUT_DEBUFF_ID, STALE_MEMORY_DEBUFF_ID),
            BURNOUT_XP_MULTIPLIER,
            STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER,
        ),
    ],
)
def test_shared_debuff_multiplier_preserves_empty_known_and_mixed_kind_inputs(
    debuff_ids: tuple[str, ...],
    expected_xp: float,
    expected_routing_weight: float,
) -> None:
    assert xp_multiplier_for_debuff_ids(debuff_ids) == pytest.approx(expected_xp)
    assert debuff_registry._multiplier_for_debuff_ids(  # noqa: SLF001
        debuff_ids, "routing_weight"
    ) == pytest.approx(expected_routing_weight)


@pytest.mark.parametrize("kind", ["xp", "routing_weight"])
def test_shared_debuff_multiplier_preserves_unknown_id_validation(
    kind: debuff_registry.DebuffKind,
) -> None:
    with pytest.raises(UnknownDebuffError):
        debuff_registry._multiplier_for_debuff_ids(  # noqa: SLF001
            ("does-not-exist",), kind
        )


# ── signature annotation drift guard ────────────────────────────────
#
# OP-1219 asked for a type-hint audit + "mypy or basic checks". mypy
# is not installed in the runner image, so this test substitutes a
# portable AST-based completeness check. Pins the module at 100%
# parameter + return annotation coverage so future edits that drop
# an annotation fail CI instead of slipping through silently.


def _module_source_path() -> Path:
    source = inspect.getsourcefile(debuff_registry)
    assert source is not None, "debuff_registry has no source file"
    return Path(source)


def _function_nodes() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(_module_source_path().read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_every_function_signature_is_fully_annotated() -> None:
    unannotated: list[str] = []
    for node in _function_nodes():
        args = node.args
        params = (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
            + ([args.vararg] if args.vararg else [])
            + ([args.kwarg] if args.kwarg else [])
        )
        for arg in params:
            if arg.arg == "self" or arg.arg == "cls":
                continue
            if arg.annotation is None:
                unannotated.append(f"{node.name}({arg.arg}) at L{node.lineno}")
        if node.returns is None:
            unannotated.append(f"{node.name} -> ? at L{node.lineno}")
    assert not unannotated, (
        "debuff_registry must keep 100% signature annotation coverage; "
        f"missing: {unannotated}"
    )


def test_every_dataclass_field_is_annotated() -> None:
    # Both registry dataclasses declare every field with a type hint;
    # the frozen-dataclass machinery itself requires this, but pinning
    # the invariant catches accidental ``field = value`` assignments
    # that bypass annotations.
    for cls in (DebuffDefinition, DebuffContext):
        hints = getattr(cls, "__annotations__", {})
        public_attrs = [
            name
            for name, value in vars(cls).items()
            if not name.startswith("_") and not callable(value)
        ]
        for attr in public_attrs:
            assert attr in hints, (
                f"{cls.__name__}.{attr} is declared without a type hint"
            )
