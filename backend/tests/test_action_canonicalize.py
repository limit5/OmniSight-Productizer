"""OP-2630/OP-2648/OP-2649 canonicalization hardening tests (offline)."""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys
import threading
from collections.abc import Iterator, Mapping

import pytest

from backend.agents import action_canonicalize
from backend.agents.action_canonicalize import (
    CanonOutcome,
    CanonicalizationContext,
    CanonicalizationError,
    CanonicalizerSpec,
    PreparedAction,
    Refinement,
    _registry_restore,
    _registry_snapshot,
    canonicalize,
    classify_refinement,
    is_uncovered,
    register_canonicalizer,
    register_canonicalizers_atomic,
)
from backend.agents.tool_registry import OperationDescriptor, resolve


_TOOL_NAME = "write_file"
_CONTEXT = CanonicalizationContext(
    workspace_id="workspace-1",
    workspace_root="/workspace",
    adapter_namespace="builtin",
)


@pytest.fixture(autouse=True)
def restore_canonicalizer_registry() -> Iterator[None]:
    """Prevent registrations in one test from leaking into another."""
    snapshot = _registry_snapshot()
    try:
        yield
    finally:
        _registry_restore(snapshot)


def _prepared_action(
    *,
    operation_descriptor: OperationDescriptor | None = None,
    executable_args: Mapping[str, object] | None = None,
    human_rendering: Mapping[str, object] | None = None,
) -> PreparedAction:
    return PreparedAction(
        operation_descriptor=operation_descriptor or resolve(_TOOL_NAME),
        canonical_target="/workspace/input.txt",
        executable_args=(
            executable_args
            if executable_args is not None
            else {"path": "/workspace/input.txt", "encoding": "utf-8"}
        ),
        human_rendering=(
            human_rendering
            if human_rendering is not None
            else {"summary": "Write /workspace/input.txt"}
        ),
    )


def test_canonicalize_dispatches_and_returns_prepared_action() -> None:
    raw_args: dict[str, object] = {"path": "input.txt"}

    def fake_canonicalizer(
        context: CanonicalizationContext,
        args: Mapping[str, object],
    ) -> PreparedAction:
        assert context is _CONTEXT
        assert args is raw_args
        return _prepared_action()

    register_canonicalizer("builtin", _TOOL_NAME, "v1", fake_canonicalizer)

    result = canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", raw_args)

    assert result.operation_descriptor == resolve(_TOOL_NAME)
    assert result.operation_descriptor.effect == "mutating"
    assert result.operation_descriptor.family == "code_write"
    assert result.executable_args == {
        "path": "/workspace/input.txt",
        "encoding": "utf-8",
    }
    assert result.executable_args is not raw_args
    assert raw_args == {"path": "input.txt"}


def test_canonicalize_admits_registered_descriptor_refinement() -> None:
    refined_descriptor = dataclasses.replace(
        resolve(_TOOL_NAME),
        effect="read_only",
        family="read_only",
    )

    def refined_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action(operation_descriptor=refined_descriptor)

    register_canonicalizers_atomic(
        [
            CanonicalizerSpec(
                "builtin",
                _TOOL_NAME,
                "v1",
                refined_canonicalizer,
                refinements=(
                    Refinement(
                        descriptor=refined_descriptor,
                        review_note="Read-only operation shape was reviewed.",
                    ),
                ),
            )
        ]
    )

    result = canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert result.operation_descriptor == refined_descriptor


def test_canonicalize_unregistered_key_fails_closed() -> None:
    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", "unknown", "v1", {})

    assert caught.value.reason.startswith("no_canonicalizer:")
    assert caught.value.reason == "no_canonicalizer:builtin/unknown@v1"
    assert caught.value.category is CanonOutcome.UNREGISTERED
    assert is_uncovered(caught.value)


def test_canonicalize_wraps_canonicalizer_exception_with_cause() -> None:
    class RawCanonicalizerError(Exception):
        pass

    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise RawCanonicalizerError("sensitive raw failure")

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/write_file@v1"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR
    assert isinstance(caught.value.__cause__, RawCanonicalizerError)


def test_canonicalize_maps_plain_value_error_to_internal_error() -> None:
    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise ValueError("leaf bug")

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/write_file@v1"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR
    assert isinstance(caught.value.__cause__, ValueError)


def test_canonicalize_propagates_canonicalization_error() -> None:
    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise CanonicalizationError("leaf_rejected")

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "leaf_rejected"
    assert caught.value.category is CanonOutcome.REJECTED
    assert type(caught.value) is CanonicalizationError
    assert isinstance(caught.value.__cause__, CanonicalizationError)
    assert caught.value is not caught.value.__cause__


def test_leaf_exact_error_cannot_forge_unregistered_outcome() -> None:
    forged = CanonicalizationError(
        "leaf_forged_unregistered",
        category=CanonOutcome.UNREGISTERED,
    )

    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise forged

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert type(caught.value) is CanonicalizationError
    assert caught.value.reason == "leaf_forged_unregistered"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR
    assert caught.value.__cause__ is forged
    assert not is_uncovered(caught.value)


def test_leaf_subclass_toggling_category_cannot_forge_unregistered() -> None:
    class TogglingCategoryError(CanonicalizationError):
        def __init__(self) -> None:
            self._category_reads = 0
            super().__init__("toggling_category")

        @property
        def category(self) -> CanonOutcome:
            self._category_reads += 1
            if self._category_reads == 1:
                return CanonOutcome.REJECTED
            return CanonOutcome.UNREGISTERED

        @category.setter
        def category(self, _value: CanonOutcome) -> None:
            pass

    forged = TogglingCategoryError()

    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise forged

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert type(caught.value) is CanonicalizationError
    assert caught.value.category is CanonOutcome.REJECTED
    assert forged.category is CanonOutcome.UNREGISTERED
    assert not is_uncovered(caught.value)


def test_leaf_subclass_raising_category_is_normalized_to_internal_error() -> None:
    class RaisingCategoryError(CanonicalizationError):
        @property
        def category(self) -> CanonOutcome:
            raise RuntimeError("hostile category accessor")

        @category.setter
        def category(self, _value: CanonOutcome) -> None:
            pass

    forged = RaisingCategoryError("raising_category")

    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise forged

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert type(caught.value) is CanonicalizationError
    assert caught.value.reason == "raising_category"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR
    assert caught.value.__cause__ is forged
    assert not is_uncovered(caught.value)


def test_leaf_deleted_or_dict_mutated_error_is_normalized_to_internal_error(
) -> None:
    deleted = CanonicalizationError("deleted_category")
    del deleted.category
    dict_mutated = CanonicalizationError("dict_mutated_category")
    dict_mutated.__dict__["category"] = "unregistered"
    current = [deleted]

    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise current[0]

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    for forged in (deleted, dict_mutated):
        current[0] = forged
        with pytest.raises(CanonicalizationError) as caught:
            canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

        assert type(caught.value) is CanonicalizationError
        assert caught.value.reason == forged.reason
        assert caught.value.category is CanonOutcome.INTERNAL_ERROR
        assert caught.value.__cause__ is forged
        assert not is_uncovered(caught.value)


def test_is_uncovered_fail_closed_table_requires_exact_framework_miss() -> None:
    with pytest.raises(CanonicalizationError) as registry_miss:
        canonicalize(_CONTEXT, "builtin", "missing", "v1", {})

    class ForgedSubclass(CanonicalizationError):
        pass

    fail_closed = [
        CanonicalizationError("rejected"),
        CanonicalizationError(
            "refinement",
            category=CanonOutcome.REFINEMENT_UNREGISTERED,
        ),
        CanonicalizationError(
            "internal",
            category=CanonOutcome.INTERNAL_ERROR,
        ),
        ValueError("not a canonicalization error"),
        ForgedSubclass("forged", category=CanonOutcome.UNREGISTERED),
    ]

    assert is_uncovered(registry_miss.value)
    assert all(not is_uncovered(exc) for exc in fail_closed)


def test_canonicalize_invalid_dispatch_key_is_total() -> None:
    class UnhashableStr(str):
        __hash__ = None  # type: ignore[assignment]

    for invalid_namespace in (None, UnhashableStr("builtin")):
        with pytest.raises(CanonicalizationError) as caught:
            canonicalize(
                _CONTEXT,
                invalid_namespace,  # type: ignore[arg-type]
                _TOOL_NAME,
                "v1",
                {},
            )

        assert caught.value.reason == "invalid_dispatch_key"
        assert caught.value.category is CanonOutcome.INTERNAL_ERROR
        assert caught.value.__cause__ is None
        assert not is_uncovered(caught.value)


def test_canonicalize_requires_exact_registry_key() -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v2", {})

    assert caught.value.reason == "no_canonicalizer:builtin/write_file@v2"


def test_register_canonicalizer_rejects_conflicting_duplicate_key() -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(),
    )

    with pytest.raises(
        ValueError,
        match="conflicting_canonicalizer_registration",
    ):
        register_canonicalizer(
            "builtin",
            _TOOL_NAME,
            "v1",
            lambda _context, _args: _prepared_action(),
        )


def test_register_canonicalizer_identical_republish_is_noop() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    key = ("builtin", _TOOL_NAME, "v1")
    register_canonicalizer(*key, canonicalizer)

    register_canonicalizer(*key, canonicalizer)

    snapshot = _registry_snapshot()
    assert sum(registered == key for registered in snapshot) == 1
    assert snapshot[key].fn is canonicalizer


def _assert_refinement_registration_rejected(
    spec: CanonicalizerSpec,
    reason: str,
) -> None:
    before = _registry_snapshot()

    with pytest.raises(ValueError, match=reason):
        register_canonicalizers_atomic([spec])

    assert _registry_snapshot() == before


def test_register_refinement_rejects_unknown_base_atomically() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            "unknown_refinement_base",
            "v1",
            canonicalizer,
            refinements=(
                Refinement(
                    OperationDescriptor(
                        "unknown_refinement_base",
                        "read_only",
                        "read_only",
                    ),
                    "Reviewed test refinement.",
                ),
            ),
        ),
        "refinement_of_unknown_base",
    )


def test_register_refinement_rejects_tool_mismatch_atomically() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            _TOOL_NAME,
            "v1",
            canonicalizer,
            refinements=(
                Refinement(
                    resolve("read_file"),
                    "Reviewed test refinement.",
                ),
            ),
        ),
        "refinement_tool_mismatch",
    )


def test_register_refinement_rejects_identity_atomically() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            _TOOL_NAME,
            "v1",
            canonicalizer,
            refinements=(
                Refinement(
                    resolve(_TOOL_NAME),
                    "Reviewed test refinement.",
                ),
            ),
        ),
        "refinement_is_identity",
    )


@pytest.mark.parametrize(
    "descriptor",
    [
        OperationDescriptor(_TOOL_NAME, "read_only", "deploy"),
        OperationDescriptor(_TOOL_NAME, "mutating", "deploy_typo"),
    ],
    ids=["incoherent-pair", "typo-family"],
)
def test_register_refinement_rejects_unknown_operation_class_atomically(
    descriptor: OperationDescriptor,
) -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            _TOOL_NAME,
            "v1",
            canonicalizer,
            refinements=(
                Refinement(descriptor, "Reviewed test refinement."),
            ),
        ),
        "refinement_unknown_operation_class",
    )


def test_register_refinement_rejects_same_effect_family_change_atomically(
) -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            _TOOL_NAME,
            "v1",
            canonicalizer,
            refinements=(
                Refinement(
                    OperationDescriptor(_TOOL_NAME, "mutating", "deploy"),
                    "Reviewed test refinement.",
                ),
            ),
        ),
        "refinement_same_effect_family_change_forbidden",
    )


def test_register_refinement_requires_nonempty_review_note_atomically() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            _TOOL_NAME,
            "v1",
            canonicalizer,
            refinements=(
                Refinement(
                    OperationDescriptor(
                        _TOOL_NAME,
                        "read_only",
                        "read_only",
                    ),
                    "   ",
                ),
            ),
        ),
        "refinement_needs_review_note",
    )


def test_register_refinement_rejects_duplicate_descriptor_atomically() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    descriptor = OperationDescriptor(
        _TOOL_NAME,
        "read_only",
        "read_only",
    )
    _assert_refinement_registration_rejected(
        CanonicalizerSpec(
            "builtin",
            _TOOL_NAME,
            "v1",
            canonicalizer,
            refinements=(
                Refinement(descriptor, "First reviewed refinement."),
                Refinement(descriptor, "Second reviewed refinement."),
            ),
        ),
        "duplicate_refinement_descriptor",
    )


def test_register_canonicalizers_atomic_conflict_is_all_or_nothing() -> None:
    def existing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    def new_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    def conflicting_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    existing_key = ("builtin", "existing_writer", "v1")
    new_key = ("builtin", "new_writer", "v1")
    register_canonicalizers_atomic(
        [CanonicalizerSpec(*existing_key, existing_canonicalizer)]
    )
    before = _registry_snapshot()

    with pytest.raises(
        ValueError,
        match="conflicting_canonicalizer_registration",
    ):
        register_canonicalizers_atomic(
            [
                CanonicalizerSpec(*new_key, new_canonicalizer),
                CanonicalizerSpec(*existing_key, conflicting_canonicalizer),
            ]
        )

    assert _registry_snapshot() == before
    assert new_key not in _registry_snapshot()


def test_register_canonicalizers_atomic_rejects_same_batch_duplicate() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    key = ("builtin", "duplicate_writer", "v1")
    before = _registry_snapshot()

    with pytest.raises(ValueError, match="duplicate_key_in_batch"):
        register_canonicalizers_atomic(
            [
                CanonicalizerSpec(*key, canonicalizer),
                CanonicalizerSpec(*key, canonicalizer),
            ]
        )

    assert _registry_snapshot() == before


def test_register_canonicalizers_atomic_rejects_reload_conflict() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    def reloaded_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    key = ("builtin", "reload_writer", "v1")
    register_canonicalizers_atomic([CanonicalizerSpec(*key, canonicalizer)])
    before = _registry_snapshot()

    with pytest.raises(
        ValueError,
        match="conflicting_canonicalizer_registration",
    ):
        register_canonicalizers_atomic(
            [CanonicalizerSpec(*key, reloaded_canonicalizer)]
        )

    assert _registry_snapshot() == before
    assert _registry_snapshot()[key].fn is canonicalizer


def test_register_canonicalizers_atomic_identical_republish_is_noop() -> None:
    def first_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    def second_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    specs = [
        CanonicalizerSpec("builtin", "first_writer", "v1", first_canonicalizer),
        CanonicalizerSpec("builtin", "second_writer", "v1", second_canonicalizer),
    ]
    register_canonicalizers_atomic(specs)
    before = _registry_snapshot()

    register_canonicalizers_atomic(specs)

    assert _registry_snapshot() == before
    assert _registry_snapshot()[
        ("builtin", "first_writer", "v1")
    ].fn is first_canonicalizer
    assert _registry_snapshot()[
        ("builtin", "second_writer", "v1")
    ].fn is second_canonicalizer


def test_register_canonicalizers_atomic_identical_nonempty_refinement_is_noop(
) -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    descriptor = OperationDescriptor(
        _TOOL_NAME,
        "read_only",
        "read_only",
    )
    spec = CanonicalizerSpec(
        "builtin",
        _TOOL_NAME,
        "v1",
        canonicalizer,
        refinements=(
            Refinement(descriptor, "Reviewed read-only refinement."),
        ),
    )
    register_canonicalizers_atomic([spec])
    before = _registry_snapshot()

    register_canonicalizers_atomic([spec])

    assert _registry_snapshot() == before
    assert before[("builtin", _TOOL_NAME, "v1")].refinements == frozenset(
        {descriptor}
    )


def test_register_canonicalizers_atomic_different_refinement_set_conflicts(
) -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    key = ("builtin", "read_file", "v1")
    code_write_descriptor = OperationDescriptor(
        "read_file",
        "mutating",
        "code_write",
    )
    deploy_descriptor = OperationDescriptor(
        "read_file",
        "mutating",
        "deploy",
    )
    register_canonicalizers_atomic(
        [
            CanonicalizerSpec(
                *key,
                canonicalizer,
                refinements=(
                    Refinement(
                        code_write_descriptor,
                        "Reviewed code-write refinement.",
                    ),
                ),
            )
        ]
    )
    before = _registry_snapshot()

    with pytest.raises(
        ValueError,
        match="conflicting_canonicalizer_registration",
    ):
        register_canonicalizers_atomic(
            [
                CanonicalizerSpec(
                    *key,
                    canonicalizer,
                    refinements=(
                        Refinement(
                            deploy_descriptor,
                            "Reviewed deploy refinement.",
                        ),
                    ),
                )
            ]
        )

    assert _registry_snapshot() == before


def test_registry_snapshot_restore_preserves_nonempty_refinement_set() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    key = ("builtin", "read_file", "v1")
    descriptor = OperationDescriptor(
        "read_file",
        "mutating",
        "code_write",
    )
    register_canonicalizers_atomic(
        [
            CanonicalizerSpec(
                *key,
                canonicalizer,
                refinements=(
                    Refinement(descriptor, "Reviewed mutating refinement."),
                ),
            )
        ]
    )
    snapshot = _registry_snapshot()
    register_canonicalizer(
        "builtin",
        "snapshot_restore_probe",
        "v1",
        canonicalizer,
    )

    _registry_restore(snapshot)

    restored = _registry_snapshot()
    assert restored == snapshot
    assert restored[key].refinements == frozenset({descriptor})
    assert ("builtin", "snapshot_restore_probe", "v1") not in restored


def test_register_canonicalizers_atomic_late_invalid_refinement_publishes_none(
) -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    valid_key = ("late_refinement_batch", _TOOL_NAME, "v1")
    invalid_tool_name = "unknown_late_refinement_base"
    before = _registry_snapshot()

    with pytest.raises(ValueError, match="refinement_of_unknown_base"):
        register_canonicalizers_atomic(
            [
                CanonicalizerSpec(
                    *valid_key,
                    canonicalizer,
                    refinements=(
                        Refinement(
                            OperationDescriptor(
                                _TOOL_NAME,
                                "read_only",
                                "read_only",
                            ),
                            "Reviewed read-only refinement.",
                        ),
                    ),
                ),
                CanonicalizerSpec(
                    "late_refinement_batch",
                    invalid_tool_name,
                    "v1",
                    canonicalizer,
                    refinements=(
                        Refinement(
                            OperationDescriptor(
                                invalid_tool_name,
                                "read_only",
                                "read_only",
                            ),
                            "Reviewed test refinement.",
                        ),
                    ),
                ),
            ]
        )

    assert _registry_snapshot() == before
    assert valid_key not in _registry_snapshot()


def test_registry_snapshot_restore_preserves_whole_entries() -> None:
    def canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return _prepared_action()

    specs = [
        CanonicalizerSpec("builtin", f"writer_{index}", "v1", canonicalizer)
        for index in range(5)
    ]
    register_canonicalizers_atomic(specs)
    snapshot = _registry_snapshot()
    sixth_key = ("builtin", "writer_5", "v1")
    register_canonicalizers_atomic(
        [CanonicalizerSpec(*sixth_key, canonicalizer)]
    )

    _registry_restore(snapshot)

    restored = _registry_snapshot()
    assert restored == snapshot
    assert sixth_key not in restored
    for spec in specs:
        entry = restored[
            (spec.adapter_namespace, spec.tool_name, spec.schema_version)
        ]
        assert entry.fn is canonicalizer
        assert entry.refinements == frozenset()


def test_register_canonicalizer_shim_honors_registry_lock() -> None:
    started = threading.Event()
    done = threading.Event()

    def worker() -> None:
        started.set()
        register_canonicalizer(
            "builtin",
            "locked_probe",
            "v1",
            lambda _context, _args: _prepared_action(),
        )
        done.set()

    with action_canonicalize._REGISTRY_LOCK:
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(timeout=2.0)
        assert not done.wait(timeout=0.2)
        assert ("builtin", "locked_probe", "v1") not in _registry_snapshot()

    thread.join(timeout=2.0)
    assert done.is_set()
    assert _registry_snapshot()[("builtin", "locked_probe", "v1")].fn is not None


def test_prepared_action_is_frozen() -> None:
    prepared = _prepared_action()

    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.canonical_target = "/workspace/other.txt"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name_desc", "canon_desc", "expected"),
    [
        (
            OperationDescriptor("probe", "mutating", "shared_family"),
            OperationDescriptor("probe", "read_only", "shared_family"),
            "canonical_looser",
        ),
        (
            OperationDescriptor("probe", "read_only", "shared_family"),
            OperationDescriptor("probe", "mutating", "shared_family"),
            "canonical_stricter",
        ),
        (
            OperationDescriptor("probe", "mutating", "code_write"),
            OperationDescriptor("probe", "mutating", "deploy"),
            "family_changed",
        ),
        (
            resolve("read_file"),
            resolve("read_file"),
            "same",
        ),
    ],
    ids=["looser", "stricter", "family-changed", "same"],
)
def test_classify_refinement_table(
    name_desc: OperationDescriptor,
    canon_desc: OperationDescriptor,
    expected: str,
) -> None:
    assert classify_refinement(name_desc, canon_desc) == expected


def test_canonicalization_context_is_frozen_and_requires_workspace() -> None:
    assert _CONTEXT.require_workspace() == "/workspace"

    with pytest.raises(dataclasses.FrozenInstanceError):
        _CONTEXT.workspace_root = "/other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("workspace_id", "workspace_root"),
    [("", "/workspace"), ("workspace-1", "")],
)
def test_canonicalization_context_require_workspace_fails_closed(
    workspace_id: str,
    workspace_root: str,
) -> None:
    context = CanonicalizationContext(
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        adapter_namespace="builtin",
    )

    with pytest.raises(CanonicalizationError) as caught:
        context.require_workspace()

    assert caught.value.reason == "no_workspace_context"


def test_canonicalization_error_enforces_runtime_closed_outcomes() -> None:
    error = CanonicalizationError("default_rejection")

    assert error.category is CanonOutcome.REJECTED
    with pytest.raises(
        TypeError,
        match="invalid canonicalization category: 'rejected'",
    ):
        CanonicalizationError(
            "invalid_category",
            category="rejected",  # type: ignore[arg-type]
        )


def test_canonicalize_deep_freezes_json_fields() -> None:
    lines = ["first"]
    rendering_parts = ["Write", "input.txt"]
    raw_args: dict[str, object] = {
        "path": "/workspace/input.txt",
        "lines": lines,
        "position": (1, 2),
    }
    rendering: dict[str, object] = {"parts": rendering_parts}

    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, args: _prepared_action(
            executable_args=args,
            human_rendering=rendering,
        ),
    )

    result = canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", raw_args)
    raw_args["path"] = "/workspace/other.txt"
    lines.append("second")
    rendering_parts.append("changed")

    assert result.executable_args == {
        "path": "/workspace/input.txt",
        "lines": ["first"],
        "position": [1, 2],
    }
    assert result.human_rendering == {"parts": ["Write", "input.txt"]}


def test_canonicalize_rejects_non_operation_descriptor() -> None:
    prepared = dataclasses.replace(
        _prepared_action(),
        operation_descriptor=None,  # type: ignore[arg-type]
    )
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: prepared,
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "descriptor_not_operation_descriptor"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


def test_canonicalize_rejects_non_str_canonical_target() -> None:
    prepared = dataclasses.replace(
        _prepared_action(),
        canonical_target=42,  # type: ignore[arg-type]
    )
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: prepared,
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "canonical_target_not_str"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("field_name", "invalid_root"),
    [
        ("executable_args", []),
        ("human_rendering", "not-a-mapping"),
    ],
    ids=["list-root", "str-root"],
)
def test_canonicalize_rejects_non_mapping_payload_roots(
    field_name: str,
    invalid_root: object,
) -> None:
    prepared = dataclasses.replace(
        _prepared_action(),
        **{field_name: invalid_root},  # type: ignore[arg-type]
    )
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: prepared,
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "payload_root_not_mapping"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


@pytest.mark.parametrize(
    "payload",
    [
        {1: "root"},
        {"a": {None: 1}},
        {"a": [{"b": {2: 3}}]},
    ],
    ids=["root", "nested-mapping", "nested-list"],
)
def test_canonicalize_rejects_non_str_payload_keys(payload: object) -> None:
    prepared = dataclasses.replace(
        _prepared_action(),
        executable_args=payload,  # type: ignore[arg-type]
    )
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: prepared,
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "non_str_payload_key"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


@pytest.mark.parametrize(
    "payload",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"nested": {"value": float("nan")}},
        {"nested": [{"value": float("-inf")}]},
    ],
    ids=["root-nan", "root-inf", "nested-nan", "nested-inf"],
)
def test_canonicalize_rejects_non_finite_payload_values(
    payload: Mapping[str, object],
) -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(executable_args=payload),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "non_serializable_args"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


@pytest.mark.parametrize("field_name", ["executable_args", "human_rendering"])
def test_canonicalize_rejects_non_serializable_json_fields(field_name: str) -> None:
    kwargs = {field_name: {"invalid": {"not-json"}}}
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(**kwargs),  # type: ignore[arg-type]
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "non_serializable_args"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


def test_canonicalize_rejects_descriptor_tool_mismatch() -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(
            operation_descriptor=resolve("read_file")
        ),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "descriptor_tool_mismatch"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR


def test_canonicalize_rejects_unregistered_descriptor_refinement() -> None:
    refined_descriptor = dataclasses.replace(
        resolve(_TOOL_NAME),
        effect="read_only",
        family="read_only",
    )
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(
            operation_descriptor=refined_descriptor
        ),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == (
        "refinement_unregistered:builtin/write_file@v1"
    )
    assert caught.value.category is CanonOutcome.REFINEMENT_UNREGISTERED


def test_canonicalize_rejects_non_prepared_action_result() -> None:
    def invalid_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return object()  # type: ignore[return-value]

    register_canonicalizer("builtin", _TOOL_NAME, "v1", invalid_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/write_file@v1"
    assert caught.value.category is CanonOutcome.INTERNAL_ERROR
    assert isinstance(caught.value.__cause__, CanonicalizationError)


def test_action_canonicalize_import_direction_is_stdlib_plus_tool_registry() -> None:
    source = pathlib.Path(action_canonicalize.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    for module in imported:
        if module == "backend.agents.tool_registry":
            continue
        root_module = module.partition(".")[0]
        assert root_module in sys.stdlib_module_names, f"non-leaf import: {module}"
