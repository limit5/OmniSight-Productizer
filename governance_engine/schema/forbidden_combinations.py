"""Pure forbidden-combination checks for S12.G v0 ticket contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from governance_engine.schema.v0 import TicketClass, TicketContract


@dataclass(frozen=True)
class ForbiddenCombinationError:
    rule_id: int
    rule_name: str
    detail: str
    remediation_hint: str = ""


Rule = Callable[[TicketContract], ForbiddenCombinationError | None]

_LIFECYCLE_SCOPE_COMPONENTS = frozenset(
    {
        "container",
        "containers",
        "process",
        "processes",
        "daemon",
        "daemons",
        "systemd",
        "supervisor",
    }
)


def _ticket_class_value(contract: TicketContract) -> str:
    return contract.ticket_class.value


def _is_subscription(contract: TicketContract) -> bool:
    return _ticket_class_value(contract).startswith("subscription-")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _is_none_marker(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().lower() == "none"
    )


def _defense_contract(contract: TicketContract) -> Any:
    return getattr(contract, "defense_contract", None)


def _rule_01_destructive_ops_need_l1_l2(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if contract.destructive_op_classes and contract.authority_required == "L3":
        return ForbiddenCombinationError(
            rule_id=1,
            rule_name="destructive_ops_need_l1_l2",
            detail="destructive_op_classes cannot be non-empty when authority_required is L3",
        )
    return None


def _rule_02_irreversible_side_effect_matches_reversibility(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if contract.external_side_effect == "irreversible" and contract.reversibility != "irreversible":
        return ForbiddenCombinationError(
            rule_id=2,
            rule_name="irreversible_side_effect_matches_reversibility",
            detail="external_side_effect irreversible requires reversibility irreversible",
        )
    return None


def _rule_03_top_window_requires_l1_reason(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if (
        contract.ticket_class is TicketClass.OPERATOR_WINDOW_TOP
        and contract.l1_exclusive_reason is None
    ):
        return ForbiddenCombinationError(
            rule_id=3,
            rule_name="top_window_requires_l1_reason",
            detail="operator-window-top tickets require l1_exclusive_reason",
        )
    return None


def _rule_04_deputy_window_requires_l2_reason(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if (
        contract.ticket_class is TicketClass.OPERATOR_WINDOW_DEPUTY
        and contract.l2_reason is None
    ):
        return ForbiddenCombinationError(
            rule_id=4,
            rule_name="deputy_window_requires_l2_reason",
            detail="operator-window-deputy tickets require l2_reason",
        )
    return None


def _rule_05_subscription_cannot_touch_production(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if _is_subscription(contract) and contract.environment_scope == "production":
        return ForbiddenCombinationError(
            rule_id=5,
            rule_name="subscription_cannot_touch_production",
            detail="subscription tickets cannot use production environment_scope",
        )
    return None


def _rule_06_subscription_cannot_be_operator_witnessed(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if contract.runtime_capability == "operator-witnessed" and _is_subscription(contract):
        return ForbiddenCombinationError(
            rule_id=6,
            rule_name="subscription_cannot_be_operator_witnessed",
            detail="subscription tickets cannot require operator-witnessed runtime_capability",
        )
    return None


def _rule_07_operator_evidence_requires_rehearsal(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if contract.evidence_class == "operator" and contract.execution_mode == "unit-testable":
        return ForbiddenCombinationError(
            rule_id=7,
            rule_name="operator_evidence_requires_rehearsal",
            detail="operator evidence cannot be paired with unit-testable execution_mode",
        )
    return None


def _rule_08_subscription_cannot_have_sensitive_payload(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if contract.external_payload_class == "sensitive" and _is_subscription(contract):
        return ForbiddenCombinationError(
            rule_id=8,
            rule_name="subscription_cannot_have_sensitive_payload",
            detail="subscription tickets cannot carry sensitive external_payload_class",
        )
    return None


def _rule_09_meta_requires_operator_rehearsal(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if contract.tag_type == "meta" and contract.execution_mode != "operator-rehearsal":
        return ForbiddenCombinationError(
            rule_id=9,
            rule_name="meta_requires_operator_rehearsal",
            detail="meta tickets require operator-rehearsal execution_mode",
        )
    return None


def _rule_10_required_paths_fit_files_touched_max(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    if len(contract.required_paths) > contract.files_touched_max:
        return ForbiddenCombinationError(
            rule_id=10,
            rule_name="required_paths_fit_files_touched_max",
            detail="required_paths count cannot exceed files_touched_max",
        )
    return None


def _rule_11_d1_required_for_runtime(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    defense_contract = _defense_contract(contract)
    error_detection = _field(defense_contract, "error_detection")
    if (
        contract.runtime_capability == "network-production-when-apply"
        and _field(error_detection, "signal") == "none"
    ):
        return ForbiddenCombinationError(
            rule_id=11,
            rule_name="D1-required-for-runtime",
            detail=(
                "runtime_capability network-production-when-apply requires "
                "error_detection.signal other than none"
            ),
            remediation_hint=(
                "Set error_detection.signal to a concrete observable signal "
                "and wire channel/observability_test evidence."
            ),
        )
    return None


def _rule_12_d2_loud_or_none(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    defense_contract = _defense_contract(contract)
    exception_handling = _field(defense_contract, "exception_handling")
    if (
        contract.external_side_effect == "network-production"
        and _field(exception_handling, "fail_loudly") is False
    ):
        return ForbiddenCombinationError(
            rule_id=12,
            rule_name="D2-loud-or-none",
            detail=(
                "external_side_effect network-production requires "
                "exception_handling.fail_loudly true"
            ),
            remediation_hint=(
                "Fail loudly on production-touching paths or reduce the "
                "external side effect to a non-production class."
            ),
        )
    return None


def _rule_13_d2_remediation_on_user_facing(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    defense_contract = _defense_contract(contract)
    exception_handling = _field(defense_contract, "exception_handling")
    if (
        _field(exception_handling, "user_facing") is True
        and _is_none_marker(_field(exception_handling, "remediation_hint_contract"))
    ):
        return ForbiddenCombinationError(
            rule_id=13,
            rule_name="D2-remediation-on-user-facing",
            detail=(
                "user-facing exception handling requires a non-none "
                "remediation_hint_contract"
            ),
            remediation_hint=(
                "Document the next user/operator action in "
                "exception_handling.remediation_hint_contract."
            ),
        )
    return None


def _rule_14_d3_required_for_lifecycle(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    touches_lifecycle = any(
        component in _LIFECYCLE_SCOPE_COMPONENTS for component in contract.scope_components
    )
    defense_contract = _defense_contract(contract)
    if (
        defense_contract is not None
        and touches_lifecycle
        and _field(defense_contract, "shutdown_contract") is None
    ):
        return ForbiddenCombinationError(
            rule_id=14,
            rule_name="D3-required-for-lifecycle",
            detail=(
                "container/process/daemon scope_components require "
                "defense_contract.shutdown_contract"
            ),
            remediation_hint=(
                "Add shutdown_contract with drain, cleanup, forced termination, "
                "and persistence details."
            ),
        )
    return None


def _rule_15_d4_required_for_destructive(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    defense_contract = _defense_contract(contract)
    recovery_path = _field(defense_contract, "recovery_path")
    if (
        defense_contract is not None
        and contract.destructive_op_classes
        and _is_none_marker(_field(recovery_path, "evidence_file"))
    ):
        return ForbiddenCombinationError(
            rule_id=15,
            rule_name="D4-required-for-destructive",
            detail=(
                "destructive_op_classes require recovery_path.evidence_file "
                "to prove recovery"
            ),
            remediation_hint=(
                "Provide a test, runbook, or audit artifact path in "
                "recovery_path.evidence_file."
            ),
        )
    return None


def _rule_16_d5_required_when_recovery_not_idempotent(
    contract: TicketContract,
) -> ForbiddenCombinationError | None:
    defense_contract = _defense_contract(contract)
    recovery_path = _field(defense_contract, "recovery_path")
    rescue_path = _field(defense_contract, "rescue_path")
    if (
        defense_contract is not None
        and _field(recovery_path, "idempotent") is False
        and _is_none_marker(_field(rescue_path, "trigger_condition"))
    ):
        return ForbiddenCombinationError(
            rule_id=16,
            rule_name="D5-required-when-recovery-not-idempotent",
            detail=(
                "non-idempotent recovery_path requires a non-none "
                "rescue_path.trigger_condition"
            ),
            remediation_hint=(
                "Define when operators switch from auto-recovery to the "
                "manual rescue path."
            ),
        )
    return None


RULES: list[Rule] = [
    _rule_01_destructive_ops_need_l1_l2,
    _rule_02_irreversible_side_effect_matches_reversibility,
    _rule_03_top_window_requires_l1_reason,
    _rule_04_deputy_window_requires_l2_reason,
    _rule_05_subscription_cannot_touch_production,
    _rule_06_subscription_cannot_be_operator_witnessed,
    _rule_07_operator_evidence_requires_rehearsal,
    _rule_08_subscription_cannot_have_sensitive_payload,
    _rule_09_meta_requires_operator_rehearsal,
    _rule_10_required_paths_fit_files_touched_max,
    _rule_11_d1_required_for_runtime,
    _rule_12_d2_loud_or_none,
    _rule_13_d2_remediation_on_user_facing,
    _rule_14_d3_required_for_lifecycle,
    _rule_15_d4_required_for_destructive,
    _rule_16_d5_required_when_recovery_not_idempotent,
]


def validate_forbidden_combinations(contract: TicketContract) -> list[ForbiddenCombinationError]:
    return [error for rule in RULES if (error := rule(contract)) is not None]


__all__ = ["ForbiddenCombinationError", "RULES", "validate_forbidden_combinations"]
