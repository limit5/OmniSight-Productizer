"""U6-0 T9/T10 G2a prepared-action canonicalization framework (dormant).

This pure operation-shape mapper is additive and dormant: no runtime path
calls :func:`canonicalize`, and the framework-stage registry starts empty.
The registry is deliberately process-local; each worker starts empty and
future family modules register the same canonicalizers in every worker.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping

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


class CanonicalizationError(Exception):
    """Fail-closed canonicalization failure surfaced to the guard."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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

_CanonicalizerKey = tuple[str, str, str]
_CANONICALIZERS: dict[_CanonicalizerKey, Canonicalizer] = {}


def register_canonicalizer(
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    fn: Canonicalizer,
) -> None:
    """Register one exact-key canonicalizer without allowing overwrite."""
    key = (adapter_namespace, tool_name, schema_version)
    if key in _CANONICALIZERS:
        raise ValueError(
            "duplicate canonicalizer: "
            f"{adapter_namespace}/{tool_name}@{schema_version}"
        )
    _CANONICALIZERS[key] = fn


def canonicalize(
    context: CanonicalizationContext,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    """Dispatch an exact-key canonicalizer or fail closed."""
    key = (adapter_namespace, tool_name, schema_version)
    canonicalizer = _CANONICALIZERS.get(key)
    target = f"{adapter_namespace}/{tool_name}@{schema_version}"
    if canonicalizer is None:
        raise CanonicalizationError(f"no_canonicalizer:{target}")

    try:
        prepared = canonicalizer(context, raw_args)
    except CanonicalizationError:
        raise
    except Exception as exc:
        raise CanonicalizationError(f"canonicalizer_failed:{target}") from exc

    return _finalize_prepared(tool_name, prepared, target)


def _finalize_prepared(
    tool_name: str,
    prepared: PreparedAction,
    target: str,
) -> PreparedAction:
    """Return a detached JSON form after enforcing descriptor invariants."""
    if not isinstance(prepared, PreparedAction):
        raise CanonicalizationError(f"canonicalizer_failed:{target}")

    prepared = dataclasses.replace(
        prepared,
        executable_args=_freeze_json(prepared.executable_args),
        human_rendering=_freeze_json(prepared.human_rendering),
    )
    if prepared.operation_descriptor.tool_name != tool_name:
        raise CanonicalizationError("descriptor_tool_mismatch")
    if prepared.operation_descriptor != resolve(tool_name):
        raise CanonicalizationError("descriptor_refinement_unregistered")
    return prepared


def _freeze_json(value: Mapping[str, object]) -> Mapping[str, object]:
    """Deep-copy ``value`` through its authoritative JSON representation."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError("non_serializable_args") from exc


def _registry_snapshot() -> dict[_CanonicalizerKey, Canonicalizer]:
    """Return a shallow registry copy for test isolation."""
    return _CANONICALIZERS.copy()


def _registry_restore(snap: dict[_CanonicalizerKey, Canonicalizer]) -> None:
    """Restore a test snapshot while preserving the registry object."""
    _CANONICALIZERS.clear()
    _CANONICALIZERS.update(snap)
