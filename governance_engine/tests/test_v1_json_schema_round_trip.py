"""Round-trip parity between pydantic (TicketContractV1) and the JSON
Schema view (`jsonschema.validate` against ``v1.schema.json``).

OP-1082 (G.A-v1-4) acceptance criteria:

* The golden YAML fixture validates against the JSON Schema derived from
  the v1 contract.
* The same payload validates through ``TicketContractV1.model_validate``.
* Both engines reject the same bad payload (missing required field).
* Both engines reject the same enum violation.

The ``v1.schema.json`` referenced by the AC is generated on-the-fly from
``TicketContractV1.model_json_schema(by_alias=True)`` — Pydantic IS the
canonical source of the v1 contract, and ``governance_engine/schema/``
is sealed from edits by the OP-1082 boundary block.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.schema.v0 import L2Reason, TicketClass  # noqa: E402
from governance_engine.schema.v1 import TicketContractV1  # noqa: E402

GOLDEN_YAML = Path(__file__).parent / "fixtures" / "v1_ticket_golden.yaml"

# JSON Schema view of the v1 contract — semantically equivalent to a
# checked-in ``v1.schema.json``; built here because the schema package
# is out of scope for this ticket.
V1_SCHEMA_JSON: dict[str, Any] = TicketContractV1.model_json_schema(by_alias=True)


def _load_golden() -> dict[str, Any]:
    return yaml.safe_load(GOLDEN_YAML.read_text(encoding="utf-8"))


# ---- AC #1-#4 (the four named round-trip tests) ----------------------


def test_golden_yaml_validates_against_json_schema() -> None:
    payload = _load_golden()
    jsonschema.validate(payload, V1_SCHEMA_JSON)


def test_golden_yaml_validates_against_pydantic() -> None:
    payload = _load_golden()
    contract = TicketContractV1.model_validate(payload)
    assert contract.ticket_key == "OP-9999"
    assert contract.phase_plugin_version == "v1"


def test_pydantic_and_jsonschema_agree_on_bad_payload() -> None:
    payload = _load_golden()
    payload.pop("loc_delta_max")

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(payload, V1_SCHEMA_JSON)
    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


def test_pydantic_and_jsonschema_agree_on_enum_violation() -> None:
    payload = _load_golden()
    payload["tag_type"] = "bogus"

    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate(payload, V1_SCHEMA_JSON)
    with pytest.raises(ValidationError):
        TicketContractV1.model_validate(payload)


# ---- "every enum value exercised at least once" ----------------------
#
# The single golden YAML can only hold one value per enum-typed field.
# These parameterised tests cycle through every value of every closed
# enum so the AC's enum-coverage clause is provable rather than
# aspirational. Each iteration exercises BOTH engines: pydantic AND
# jsonschema must accept every enumerated value.

_ENUM_FIELDS: dict[str, list[str]] = {
    "schema_version": ["v0", "v1"],
    "class": [c.value for c in TicketClass],
    "destructive_op_scope": ["none", "local", "staging", "production"],
    "external_side_effect": ["none", "read", "write", "irreversible"],
    "external_payload_class": ["operational", "sensitive", "public", "credential-material"],
    "runtime_capability": ["unit-only", "integration", "operator-witnessed"],
    "execution_mode": ["unit-testable", "integration", "operator-rehearsal"],
    "evidence_class": [
        "unit", "integration", "operator", "audit-log",
        "structural", "behavioral-smoke", "semantic", "longitudinal",
    ],
    "on_scope_creep": ["file-followup", "abort", "escalate"],
    "tag_type": ["story", "meta", "epic", "spike"],
    "authority_required": ["L1", "L2", "L3"],
    "reversibility": ["reversible", "irreversible", "destructive"],
    "environment_scope": ["local", "staging", "production"],
    "phase_plugin_version": ["v1"],
    "l2_reason": [r.value for r in L2Reason],
}


def _apply_class_coherence(payload: dict[str, Any], *, l2_reason: str | None = None) -> None:
    """Keep v0's ``require_authority_reasons`` validator happy when the
    parametric sweep flips ``class`` or ``l2_reason``."""
    cls = payload.get("class")
    if cls == TicketClass.OPERATOR_WINDOW_TOP.value:
        payload["l1_exclusive_reason"] = "operator-top requires l1 reason"
        payload["l2_reason"] = None
    elif cls == TicketClass.OPERATOR_WINDOW_DEPUTY.value:
        payload["l1_exclusive_reason"] = None
        payload["l2_reason"] = l2_reason or L2Reason.DESTRUCTIVE_ACTION.value
    else:
        payload["l1_exclusive_reason"] = None
        payload["l2_reason"] = None


@pytest.mark.parametrize(
    ("field", "value"),
    [(field, value) for field, values in _ENUM_FIELDS.items() for value in values],
)
def test_every_enum_value_round_trips_through_both_engines(
    field: str, value: str
) -> None:
    payload = _load_golden()
    if field == "l2_reason":
        payload["class"] = TicketClass.OPERATOR_WINDOW_DEPUTY.value
        _apply_class_coherence(payload, l2_reason=value)
    else:
        payload[field] = value
        if field == "class":
            _apply_class_coherence(payload)

    jsonschema.validate(payload, V1_SCHEMA_JSON)
    TicketContractV1.model_validate(payload)
