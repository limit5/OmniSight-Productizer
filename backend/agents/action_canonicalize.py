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

from backend.agents.tool_registry import (
    KNOWN_OPERATION_CLASSES,
    OperationDescriptor,
    resolve,
)


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


class CoverageStatus(str, enum.Enum):
    """Closed outcomes from a canonicalizer coverage lookup."""

    UNREGISTERED = "unregistered"
    PREPARED = "prepared"
    ERROR = "error"


@dataclasses.dataclass(frozen=True)
class CoverageResult:
    """Raise-free coverage lookup result with status-bound payloads."""

    status: CoverageStatus
    prepared: PreparedAction | None = None
    error: CanonicalizationError | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not CoverageStatus:
            raise TypeError(f"invalid coverage status: {self.status!r}")
        if self.status is CoverageStatus.PREPARED:
            if (
                not isinstance(self.prepared, PreparedAction)
                or self.error is not None
            ):
                raise ValueError(
                    "PREPARED requires a PreparedAction and no error"
                )
        elif self.status is CoverageStatus.ERROR:
            if (
                type(self.error) is not CanonicalizationError
                or self.prepared is not None
            ):
                raise ValueError(
                    "ERROR requires a CanonicalizationError and no prepared"
                )
        elif self.prepared is not None or self.error is not None:
            raise ValueError("UNREGISTERED carries no payload")


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
class Refinement:
    descriptor: OperationDescriptor
    review_note: str


def _verdict_rank(descriptor: OperationDescriptor) -> int:
    if descriptor.family == "__unknown_deny__":
        return 2
    if descriptor.effect == "read_only":
        return 0
    return 1


def classify_refinement(
    name_desc: OperationDescriptor,
    canon_desc: OperationDescriptor,
) -> str:
    """Telemetry ONLY (G6.3), NOT the gate. A family change is
    policy-INCOMPARABLE — NEVER ``same``.
    """
    if name_desc.family != canon_desc.family:
        return "family_changed"
    name_rank = _verdict_rank(name_desc)
    canon_rank = _verdict_rank(canon_desc)
    if canon_rank > name_rank:
        return "canonical_stricter"
    if canon_rank < name_rank:
        return "canonical_looser"
    return "same"


@dataclasses.dataclass(frozen=True)
class CanonicalizerEntry:
    fn: "Canonicalizer"
    refinements: frozenset[OperationDescriptor] = frozenset()


_CanonicalizerKey = tuple[str, str, str]
_CANONICALIZERS: dict[_CanonicalizerKey, CanonicalizerEntry] = {}
_REGISTRY_LOCK = threading.RLock()
_frozen: bool = False


@dataclasses.dataclass(frozen=True)
class CanonicalizerSpec:
    adapter_namespace: str
    tool_name: str
    schema_version: str
    fn: "Canonicalizer"
    refinements: tuple["Refinement", ...] = ()


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
            name_desc = resolve(spec.tool_name)
            seen: set[OperationDescriptor] = set()
            for refinement in spec.refinements:
                if name_desc.family == "__unknown_deny__":
                    raise ValueError(f"refinement_of_unknown_base:{key}")
                if refinement.descriptor.tool_name != spec.tool_name:
                    raise ValueError(f"refinement_tool_mismatch:{key}")
                if refinement.descriptor == name_desc:
                    raise ValueError(f"refinement_is_identity:{key}")
                operation_class = (
                    refinement.descriptor.effect,
                    refinement.descriptor.family,
                )
                if operation_class not in KNOWN_OPERATION_CLASSES:
                    raise ValueError(
                        f"refinement_unknown_operation_class:{key}"
                    )
                if (
                    name_desc.effect == refinement.descriptor.effect
                    and name_desc.family != refinement.descriptor.family
                ):
                    raise ValueError(
                        "refinement_same_effect_family_change_forbidden:"
                        f"{key}"
                    )
                if not refinement.review_note.strip():
                    raise ValueError(f"refinement_needs_review_note:{key}")
                if refinement.descriptor in seen:
                    raise ValueError(
                        f"duplicate_refinement_descriptor:{key}"
                    )
                seen.add(refinement.descriptor)
            entry = CanonicalizerEntry(
                fn=spec.fn,
                refinements=frozenset(seen),
            )
            if key in staged:
                raise ValueError(f"duplicate_key_in_batch:{key}")
            existing = _CANONICALIZERS.get(key)
            if existing is not None and not _entry_identical(existing, entry):
                raise ValueError(
                    f"conflicting_canonicalizer_registration:{key}"
                )
            staged[key] = entry
        to_insert = {
            key: entry
            for key, entry in staged.items()
            if key not in _CANONICALIZERS
        }
        if _frozen and to_insert:
            raise RuntimeError("canonicalizer_registry_frozen")
        _CANONICALIZERS.update(to_insert)


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


def freeze_registry() -> None:
    """Finish bootstrap and reject subsequent new-key publication."""
    global _frozen
    with _REGISTRY_LOCK:
        _frozen = True


def is_frozen() -> bool:
    """Return whether this process-local registry has finished bootstrap."""
    return _frozen


def reset_for_tests() -> None:
    """Clear and thaw the process-local registry for test isolation."""
    global _frozen
    with _REGISTRY_LOCK:
        _CANONICALIZERS.clear()
        _frozen = False


def is_registered(
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
) -> bool:
    """Return whether an exact, well-formed dispatch key is registered."""
    if not (
        type(adapter_namespace) is str
        and type(tool_name) is str
        and type(schema_version) is str
    ):
        return False
    return (adapter_namespace, tool_name, schema_version) in _CANONICALIZERS


def canonicalize_if_registered(
    context: CanonicalizationContext,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    raw_args: Mapping[str, object],
) -> CoverageResult:
    """Return tri-state coverage without raising canonicalization failures.

    ``UNREGISTERED`` costs one dictionary membership check. A malformed key
    is ``ERROR`` (fail closed), never uncovered.
    """
    if not (
        type(adapter_namespace) is str
        and type(tool_name) is str
        and type(schema_version) is str
    ):
        return CoverageResult(
            CoverageStatus.ERROR,
            error=CanonicalizationError(
                "invalid_dispatch_key",
                category=CanonOutcome.INTERNAL_ERROR,
            ),
        )
    if (adapter_namespace, tool_name, schema_version) not in _CANONICALIZERS:
        return CoverageResult(CoverageStatus.UNREGISTERED)
    try:
        return CoverageResult(
            CoverageStatus.PREPARED,
            prepared=canonicalize(
                context,
                adapter_namespace,
                tool_name,
                schema_version,
                raw_args,
            ),
        )
    except CanonicalizationError as exc:
        return CoverageResult(CoverageStatus.ERROR, error=exc)


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
        return _finalize_prepared(
            tool_name,
            prepared,
            target,
            entry.refinements,
        )
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
    refinements: frozenset[OperationDescriptor],
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
    if d == resolve(tool_name):
        return prepared
    if d in refinements:
        return prepared
    raise CanonicalizationError(
        f"refinement_unregistered:{target}",
        category=CanonOutcome.REFINEMENT_UNREGISTERED,
    )


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


def _snapshot_state_for_tests() -> tuple[
    dict[_CanonicalizerKey, CanonicalizerEntry],
    bool,
]:
    """Return all mutable registry state for exact test isolation."""
    with _REGISTRY_LOCK:
        return (dict(_CANONICALIZERS), _frozen)


def _restore_state_for_tests(
    state: tuple[dict[_CanonicalizerKey, CanonicalizerEntry], bool],
) -> None:
    """Restore all mutable registry state while preserving the dictionary."""
    global _frozen
    entries, frozen = state
    with _REGISTRY_LOCK:
        _CANONICALIZERS.clear()
        _CANONICALIZERS.update(entries)
        _frozen = frozen
