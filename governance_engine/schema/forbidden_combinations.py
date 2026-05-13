"""Pure forbidden-combination checks for S12.G v0 ticket contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from governance_engine.schema.v0 import TicketClass, TicketContract


@dataclass(frozen=True)
class ForbiddenCombinationError:
    rule_id: int
    rule_name: str
    detail: str


Rule = Callable[[TicketContract], ForbiddenCombinationError | None]


def _ticket_class_value(contract: TicketContract) -> str:
    return contract.ticket_class.value


def _is_subscription(contract: TicketContract) -> bool:
    return _ticket_class_value(contract).startswith("subscription-")


def _rule_01_destructive_ops_need_l1_l2(contract: TicketContract) -> ForbiddenCombinationError | None:
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


def _rule_03_top_window_requires_l1_reason(contract: TicketContract) -> ForbiddenCombinationError | None:
    if contract.ticket_class is TicketClass.OPERATOR_WINDOW_TOP and contract.l1_exclusive_reason is None:
        return ForbiddenCombinationError(
            rule_id=3,
            rule_name="top_window_requires_l1_reason",
            detail="operator-window-top tickets require l1_exclusive_reason",
        )
    return None


def _rule_04_deputy_window_requires_l2_reason(contract: TicketContract) -> ForbiddenCombinationError | None:
    if contract.ticket_class is TicketClass.OPERATOR_WINDOW_DEPUTY and contract.l2_reason is None:
        return ForbiddenCombinationError(
            rule_id=4,
            rule_name="deputy_window_requires_l2_reason",
            detail="operator-window-deputy tickets require l2_reason",
        )
    return None


def _rule_05_subscription_cannot_touch_production(contract: TicketContract) -> ForbiddenCombinationError | None:
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


def _rule_07_operator_evidence_requires_rehearsal(contract: TicketContract) -> ForbiddenCombinationError | None:
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


def _rule_09_meta_requires_operator_rehearsal(contract: TicketContract) -> ForbiddenCombinationError | None:
    if contract.tag_type == "meta" and contract.execution_mode != "operator-rehearsal":
        return ForbiddenCombinationError(
            rule_id=9,
            rule_name="meta_requires_operator_rehearsal",
            detail="meta tickets require operator-rehearsal execution_mode",
        )
    return None


def _rule_10_required_paths_fit_files_touched_max(contract: TicketContract) -> ForbiddenCombinationError | None:
    if len(contract.required_paths) > contract.files_touched_max:
        return ForbiddenCombinationError(
            rule_id=10,
            rule_name="required_paths_fit_files_touched_max",
            detail="required_paths count cannot exceed files_touched_max",
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
]


def validate_forbidden_combinations(contract: TicketContract) -> list[ForbiddenCombinationError]:
    return [error for rule in RULES if (error := rule(contract)) is not None]


__all__ = ["ForbiddenCombinationError", "RULES", "validate_forbidden_combinations"]
