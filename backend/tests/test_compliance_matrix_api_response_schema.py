"""BP.D.6 contract tests for compliance matrix API response schemas."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import auth as _au
from backend.routers.compliance_matrix import (
    ComplianceMatrixListResponse,
    ComplianceMatrixResponse,
    router,
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_au.require_operator] = lambda: None
    return TestClient(app)


def _valid_response_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "is_auxiliary_compliant": True,
        "compliance_matrix": "medical",
        "disclaimer": (
            "This is an auxiliary check tool. AI-assisted output MUST be "
            "reviewed by a human certified engineer."
        ),
        "standards": ["IEC 62304"],
        "claims": [],
        "gaps": [],
    }
    payload.update(overrides)
    return payload


def test_compliance_matrix_response_defaults_to_advisory_human_signoff() -> None:
    response = ComplianceMatrixResponse(**_valid_response_payload())

    assert response.audit_type == "advisory"
    assert response.requires_human_signoff is True
    assert response.model_dump()["audit_type"] == "advisory"
    assert response.model_dump()["requires_human_signoff"] is True


@pytest.mark.parametrize("bad", ["authoritative", "blocking", "", None])
def test_compliance_matrix_response_rejects_non_advisory_audit_type(
    bad: object,
) -> None:
    with pytest.raises(ValidationError) as exc:
        ComplianceMatrixResponse(
            **_valid_response_payload(audit_type=bad)
        )

    assert any(e["type"] == "literal_error" for e in exc.value.errors())


@pytest.mark.parametrize("bad", [False, None, 0, "true"])
def test_compliance_matrix_response_rejects_human_signoff_waiver(
    bad: object,
) -> None:
    with pytest.raises(ValidationError) as exc:
        ComplianceMatrixResponse(
            **_valid_response_payload(requires_human_signoff=bad)
        )

    assert any(e["type"] == "literal_error" for e in exc.value.errors())


def test_compliance_matrix_list_response_is_wrapped() -> None:
    response = ComplianceMatrixListResponse(items=[], count=0)

    assert response.audit_type == "advisory"
    assert response.requires_human_signoff is True


def test_check_route_has_pinned_response_model() -> None:
    check_routes = [
        route for route in router.routes
        if getattr(route, "path", None) == "/compliance-matrix/check"
    ]

    assert len(check_routes) == 1
    assert check_routes[0].response_model is ComplianceMatrixResponse


def test_check_route_returns_advisory_human_signoff_wrapper() -> None:
    response = _client().post(
        "/compliance-matrix/check",
        json={
            "compliance_matrix": "medical",
            "guild": "architect",
            "tier": "T0",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["audit_type"] == "advisory"
    assert data["requires_human_signoff"] is True
    assert data["compliance_matrix"] == "medical"
    assert data["is_auxiliary_compliant"] is True


def test_list_route_returns_advisory_human_signoff_wrapper() -> None:
    response = _client().get("/compliance-matrix/matrices")

    assert response.status_code == 200
    data = response.json()
    assert data["audit_type"] == "advisory"
    assert data["requires_human_signoff"] is True
    assert data["count"] == 4
