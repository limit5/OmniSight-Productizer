"""U6-0 T9/T10 G2a prepared-action canonicalization framework (dormant).

This pure operation-shape mapper is additive and dormant: no runtime path
calls :func:`canonicalize`, and the framework-stage registry starts empty.
The registry is deliberately process-local; each worker starts empty and
future family modules register the same canonicalizers in every worker.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import threading
from collections.abc import Callable, Mapping, Sequence

from backend.agents.tool_registry import OperationDescriptor, resolve


@dataclasses.dataclass(frozen=True)
class PreparedAction:
    """Immutable operation-level output from a trusted canonicalizer.

    ``PreparedAction`` deliberately carries no ``OperationIdentity``, tenant,
    principal, or digest.  The guard assembles that identity from
    ``execution_context``, ``executable_args`` (for the arguments hash), and
    provenance, and then computes the prepared-action digest.  This module
    only maps operation shape.
    """

    operation_descriptor: OperationDescriptor
    canonical_target: str
    executable_args: Mapping[str, object]
    human_rendering: Mapping[str, object]


class CanonOutcome(str, enum.Enum):
    """Closed outcomes surfaced by the canonicalization boundary."""

    UNREGISTERED = "unregistered"
    REJECTED = "rejected"
    REFINEMENT_UNREGISTERED = "refinement_unregistered"
    INTERNAL_ERROR = "internal_error"


class CanonicalizationError(Exception):
    """Fail-closed canonicalization failure surfaced to the guard."""

    def __init__(
        self,
        reason: str,
        *,
        category: CanonOutcome = CanonOutcome.REJECTED,
    ) -> None:
        if not isinstance(category, CanonOutcome):
            raise TypeError(f"invalid canonicalization category: {category!r}")
        super().__init__(reason)
        self.reason = reason
        self.category = category


def _normalize_leaf_error(exc: BaseException) -> "CanonicalizationError":
    """Return a fresh exact error after one read of a forged leaf error.

    A leaf or finalizer may never produce ``UNREGISTERED``.  A forged,
    non-enum, raising, or deleted category becomes ``INTERNAL_ERROR``.
    """
    try:
        r = exc.reason  # type: ignore[attr-defined]
        reason = r if isinstance(r, str) else "canonicalization_error"
    except Exception:
        reason = "malformed_canonicalization_error"
    try:
        cat = exc.category  # type: ignore[attr-defined]
    except Exception:
        cat = None
    if not isinstance(cat, CanonOutcome) or cat is CanonOutcome.UNREGISTERED:
        cat = CanonOutcome.INTERNAL_ERROR
    return CanonicalizationError(reason, category=cat)


@dataclasses.dataclass(frozen=True)
class CanonicalizationContext:
    """Trusted adapter and workspace binding supplied by the guard."""

    workspace_id: str
    workspace_root: str
    adapter_namespace: str

    def require_workspace(self) -> str:
        """Return the authoritative root, or fail closed when unbound."""
        if not self.workspace_id or not self.workspace_root:
            raise CanonicalizationError("no_workspace_context")
        return self.workspace_root


Canonicalizer = Callable[
    [CanonicalizationContext, Mapping[str, object]], PreparedAction
]


@dataclasses.dataclass(frozen=True)
class CanonicalizerEntry:
    fn: "Canonicalizer"
    refinements: frozenset[OperationDescriptor] = frozenset()


_CanonicalizerKey = tuple[str, str, str]
_CANONICALIZERS: dict[_CanonicalizerKey, CanonicalizerEntry] = {}
_REGISTRY_LOCK = threading.RLock()


@dataclasses.dataclass(frozen=True)
class CanonicalizerSpec:
    adapter_namespace: str
    tool_name: str
    schema_version: str
    fn: "Canonicalizer"
    refinements: frozenset[OperationDescriptor] = frozenset()


def _entry_identical(a: CanonicalizerEntry, b: CanonicalizerEntry) -> bool:
    return a.fn is b.fn and a.refinements == b.refinements


def register_canonicalizers_atomic(
    specs: "Sequence[CanonicalizerSpec]",
) -> None:
    """Publish atomically; identical is a no-op and any conflict aborts."""
    with _REGISTRY_LOCK:
        staged: dict[_CanonicalizerKey, CanonicalizerEntry] = {}
        for spec in specs:
            key = (
                spec.adapter_namespace,
                spec.tool_name,
                spec.schema_version,
            )
            entry = CanonicalizerEntry(
                fn=spec.fn,
                refinements=frozenset(spec.refinements),
            )
            if key in staged:
                raise ValueError(f"duplicate_key_in_batch:{key}")
            existing = _CANONICALIZERS.get(key)
            if existing is not None and not _entry_identical(existing, entry):
                raise ValueError(
                    f"conflicting_canonicalizer_registration:{key}"
                )
            staged[key] = entry
        _CANONICALIZERS.update(staged)


def register_canonicalizer(
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    fn: Canonicalizer,
) -> None:
    """Register one exact-key canonicalizer through the atomic API."""
    register_canonicalizers_atomic(
        [CanonicalizerSpec(adapter_namespace, tool_name, schema_version, fn)]
    )


def canonicalize(
    context: CanonicalizationContext,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    """Dispatch an exact-key canonicalizer or fail closed."""
    if not (
        type(adapter_namespace) is str
        and type(tool_name) is str
        and type(schema_version) is str
    ):
        raise CanonicalizationError(
            "invalid_dispatch_key",
            category=CanonOutcome.INTERNAL_ERROR,
        )
    key = (adapter_namespace, tool_name, schema_version)
    target = f"{adapter_namespace}/{tool_name}@{schema_version}"
    entry = _CANONICALIZERS.get(key)
    if entry is None:
        raise CanonicalizationError(
            f"no_canonicalizer:{target}",
            category=CanonOutcome.UNREGISTERED,
        )

    try:
        prepared = entry.fn(context, raw_args)
        return _finalize_prepared(tool_name, prepared, target)
    except CanonicalizationError as exc:
        raise _normalize_leaf_error(exc) from exc
    except Exception as exc:
        raise CanonicalizationError(
            f"canonicalizer_failed:{target}",
            category=CanonOutcome.INTERNAL_ERROR,
        ) from exc


def _finalize_prepared(
    tool_name: str,
    prepared: PreparedAction,
    target: str,
) -> PreparedAction:
    """Return a detached JSON form after enforcing descriptor invariants."""
    if not isinstance(prepared, PreparedAction):
        raise CanonicalizationError(
            f"canonicalizer_failed:{target}",
            category=CanonOutcome.INTERNAL_ERROR,
        )

    d = prepared.operation_descriptor
    if not isinstance(d, OperationDescriptor):
        raise CanonicalizationError(
            "descriptor_not_operation_descriptor",
            category=CanonOutcome.INTERNAL_ERROR,
        )
    if not isinstance(prepared.canonical_target, str):
        raise CanonicalizationError(
            "canonical_target_not_str",
            category=CanonOutcome.INTERNAL_ERROR,
        )
    if not isinstance(prepared.executable_args, Mapping) or not isinstance(
        prepared.human_rendering, Mapping
    ):
        raise CanonicalizationError(
            "payload_root_not_mapping",
            category=CanonOutcome.INTERNAL_ERROR,
        )

    prepared = dataclasses.replace(
        prepared,
        executable_args=_freeze_json(prepared.executable_args),
        human_rendering=_freeze_json(prepared.human_rendering),
    )
    if d.tool_name != tool_name:
        raise CanonicalizationError(
            "descriptor_tool_mismatch",
            category=CanonOutcome.INTERNAL_ERROR,
        )
    if d != resolve(tool_name):
        raise CanonicalizationError(
            "descriptor_refinement_unregistered",
            category=CanonOutcome.INTERNAL_ERROR,
        )
    return prepared


def _reject_non_str_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    "non_str_payload_key",
                    category=CanonOutcome.INTERNAL_ERROR,
                )
            _reject_non_str_keys(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_str_keys(item)


def _freeze_json(value: Mapping[str, object]) -> Mapping[str, object]:
    """Deep-copy ``value`` through its authoritative JSON representation."""
    _reject_non_str_keys(value)
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(
            "non_serializable_args",
            category=CanonOutcome.INTERNAL_ERROR,
        ) from exc


def is_uncovered(exc: object) -> bool:
    """Return whether ``exc`` is the exact framework registry-miss outcome.

    This is the only outcome later guard stages may treat as an unbuilt tool.
    All other outcomes fail closed, and subclasses cannot impersonate a miss.
    """
    return (
        type(exc) is CanonicalizationError
        and exc.category is CanonOutcome.UNREGISTERED
    )


def _registry_snapshot() -> dict[_CanonicalizerKey, CanonicalizerEntry]:
    """Return a whole-entry registry copy for test isolation."""
    with _REGISTRY_LOCK:
        return dict(_CANONICALIZERS)


def _registry_restore(
    snap: dict[_CanonicalizerKey, CanonicalizerEntry],
) -> None:
    """Restore a test snapshot while preserving the registry object."""
    with _REGISTRY_LOCK:
        _CANONICALIZERS.clear()
        _CANONICALIZERS.update(snap)
