"""Deterministic orchestration for all v1 preflight checks."""

from __future__ import annotations

from typing import TypeAlias

from governance_engine.preflight.credential_escalation import (
    PreflightError as CredentialEscalationError,
    check_credential_material_requires_l1_non_subscription,
)
from governance_engine.preflight.cross_phase_blockers import (
    PreflightError as CrossPhaseBlockerError,
    check_cross_phase_blocker_shape,
)
from governance_engine.preflight.dependency_artifacts import (
    PreflightError as DependencyArtifactError,
    check_dependency_artifacts,
)
from governance_engine.preflight.meta_blockedby import (
    PreflightError as MetaBlockedByError,
    check_meta_blocked_by_completeness,
)
from governance_engine.preflight.mutex_consistency import (
    PreflightError as MutexConsistencyError,
    check_mutex_consistency,
)
from governance_engine.preflight.path_reciprocity import (
    PreflightError as PathReciprocityError,
    check_path_reciprocity,
)
from governance_engine.preflight.plugin_version import (
    PluginRegistry,
    PreflightError as PluginVersionError,
    check_plugin_version_compatibility,
)
from governance_engine.schema.v1 import TicketContractV1

PreflightError: TypeAlias = (
    CredentialEscalationError
    | CrossPhaseBlockerError
    | DependencyArtifactError
    | MetaBlockedByError
    | MutexConsistencyError
    | PathReciprocityError
    | PluginVersionError
)


def run_all_preflight_checks(
    roster: list[TicketContractV1],
    registry: PluginRegistry,
    claimed_child_count: dict[str, int],
) -> list[PreflightError]:
    """Run all v1 preflight checks in stable report order."""
    errors: list[PreflightError] = []
    errors.extend(check_plugin_version_compatibility(roster, registry))
    errors.extend(check_credential_material_requires_l1_non_subscription(roster))
    errors.extend(check_cross_phase_blocker_shape(roster))
    errors.extend(check_dependency_artifacts(roster))
    errors.extend(check_meta_blocked_by_completeness(roster, claimed_child_count))
    errors.extend(check_mutex_consistency(roster))
    errors.extend(check_path_reciprocity(roster))
    return errors


__all__ = ["PreflightError", "run_all_preflight_checks"]
