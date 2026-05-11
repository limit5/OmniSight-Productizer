"""OP-885 D13 — API backwards-compatibility CI gate tests.

Six cases pinned by the ticket test plan:

1. added field allowed,
2. removed field refused,
3. renamed endpoint refused,
4. approved-breaking trailer override,
5. deprecation header injected (per-endpoint, 90-day Sunset),
6. OpenAPI parse error degrades to manual review.

The script under test is ``scripts/check_api_compat.py``. We import it
directly via a path-based import so the tests stay light (no subprocess
boot) and report concrete coverage of the diff logic.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


# ── Import the script under test (it lives outside the backend package) ──

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_api_compat.py"


def _load_check_api_compat():
    spec = importlib.util.spec_from_file_location("check_api_compat", _SCRIPT)
    assert spec and spec.loader, f"could not load {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_api_compat"] = mod
    spec.loader.exec_module(mod)
    return mod


check_api_compat = _load_check_api_compat()


# ── Spec fixtures ──────────────────────────────────────────────────


def _base_spec() -> dict:
    """A small but realistic OpenAPI 3.x document with two endpoints."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "test", "version": "1.0"},
        "paths": {
            "/widgets": {
                "post": {
                    "operationId": "createWidget",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "color": {"type": "string"},
                                    },
                                    "required": ["name"],
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/widgets/{id}": {
                "get": {
                    "operationId": "getWidget",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True}
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


# ── Case 1: added field allowed ───────────────────────────────────


def test_added_optional_field_is_not_breaking():
    base = _base_spec()
    head = deepcopy(base)
    # add a new optional response field — purely additive
    head["paths"]["/widgets"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["created_at"] = {"type": "string"}
    # also add a new optional request field
    head["paths"]["/widgets"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["tags"] = {"type": "array"}

    findings = check_api_compat.diff_specs(base, head)
    assert findings == [], (
        f"expected no breaking findings for additive change, got {findings}"
    )


# ── Case 2: removed field refused ─────────────────────────────────


def test_removed_request_field_is_breaking():
    base = _base_spec()
    head = deepcopy(base)
    del head["paths"]["/widgets"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["color"]

    findings = check_api_compat.diff_specs(base, head)
    kinds = {f.kind for f in findings}
    assert "removed_field" in kinds
    assert any("color" in f.location for f in findings)


# ── Case 3: renamed endpoint refused ──────────────────────────────


def test_renamed_endpoint_is_breaking():
    base = _base_spec()
    head = deepcopy(base)
    # Move /widgets/{id} → /widgets/by-id/{id} but keep the same operationId.
    moved_op = head["paths"].pop("/widgets/{id}")
    head["paths"]["/widgets/by-id/{id}"] = moved_op

    findings = check_api_compat.diff_specs(base, head)
    kinds = {f.kind for f in findings}
    assert "renamed_endpoint" in kinds
    # And NOT mis-categorised as a removed_endpoint — that would suggest the
    # rename heuristic isn't pairing them by operationId.
    assert "removed_endpoint" not in kinds


# ── Case 4: approved-breaking trailer override ────────────────────


def test_approved_breaking_trailer_overrides_refusal(tmp_path: Path):
    base = _base_spec()
    head = deepcopy(base)
    # Force a breaking change.
    del head["paths"]["/widgets"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["color"]

    base_file = tmp_path / "base.json"
    head_file = tmp_path / "head.json"
    base_file.write_text(json.dumps(base))
    head_file.write_text(json.dumps(head))

    args = SimpleNamespace(
        base_file=str(base_file),
        head_file=str(head_file),
        base_ref="origin/main",
        head_ref="HEAD",
        spec_path="openapi.json",
        commit_range="origin/main..HEAD",
        allow_missing_base=False,
    )

    # Without trailer → exit 1 (APIBreakingChangeRefused).
    with patch.object(
        check_api_compat, "has_approved_breaking_trailer", return_value=False
    ):
        report, code = check_api_compat.run(args)
    assert code == 1
    assert report.has_breaking

    # With trailer → exit 0 (override).
    with patch.object(
        check_api_compat, "has_approved_breaking_trailer", return_value=True
    ):
        report, code = check_api_compat.run(args)
    assert code == 0
    assert report.has_breaking, "findings should still be reported for audit trail"
    assert report.approved_breaking is True


# ── Case 5: per-endpoint deprecation header injected ──────────────


def test_per_endpoint_deprecation_header_90_day_window():
    """OP-885 AC#4: register an endpoint, see a Sunset 90 days out."""
    from backend import api_versioning as av

    app = FastAPI()
    router = APIRouter()

    @router.get("/legacy_status")
    async def legacy_status() -> dict:
        return {"ok": True}

    av.register_versioned_api(app, [router])
    av.install_deprecation_headers_middleware(app)

    # Register the endpoint as deprecated with the default 90-day window.
    av.clear_endpoint_deprecations()
    before = datetime.now(timezone.utc)
    av.register_endpoint_deprecation(path="/legacy_status", method="GET")
    after = datetime.now(timezone.utc)

    try:
        client = TestClient(app)
        resp = client.get("/api/v1/legacy_status")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"
        sunset_raw = resp.headers.get("Sunset")
        assert sunset_raw is not None
        sunset = datetime.strptime(
            sunset_raw, "%a, %d %b %Y %H:%M:%S GMT"
        ).replace(tzinfo=timezone.utc)
        # The header should be 90 days out (within the wallclock skew of
        # the test itself).
        assert before + timedelta(days=90) - timedelta(seconds=5) <= sunset
        assert sunset <= after + timedelta(days=90) + timedelta(seconds=5)
    finally:
        av.clear_endpoint_deprecations()


# ── Case 6: parse error → manual review path ──────────────────────


def test_openapi_parse_error_degrades_to_manual_review(tmp_path: Path):
    base_file = tmp_path / "base.json"
    head_file = tmp_path / "head.json"
    base_file.write_text("{ this is not valid json")
    head_file.write_text(json.dumps(_base_spec()))

    args = SimpleNamespace(
        base_file=str(base_file),
        head_file=str(head_file),
        base_ref="origin/main",
        head_ref="HEAD",
        spec_path="openapi.json",
        commit_range="origin/main..HEAD",
        allow_missing_base=False,
    )
    report, code = check_api_compat.run(args)

    assert code == 2, "parse failure should exit 2 (manual review), not 0 or 1"
    assert report.parse_error is not None
    assert "base" in report.parse_error.lower()
    assert not report.findings


# ── Bonus: an extra parameter-tightening case to prove query-param coverage ──
# (Not part of the AC count, but cheap to verify.)


def test_required_new_query_param_is_breaking():
    base = _base_spec()
    head = deepcopy(base)
    head["paths"]["/widgets/{id}"]["get"]["parameters"].append(
        {"name": "tenant_id", "in": "query", "required": True}
    )

    findings = check_api_compat.diff_specs(base, head)
    kinds = {f.kind for f in findings}
    assert "required_new_field" in kinds
    assert any("tenant_id" in f.location for f in findings)
