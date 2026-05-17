"""OP-1326 -- input-validation tests for llm_credentials router."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import auth as auth_mod
from backend.routers import llm_credentials as router_mod


def _admin_user() -> auth_mod.User:
    return auth_mod.User(
        id="u-test-admin",
        email="admin@example.test",
        name="Admin",
        role="admin",
        tenant_id="t-op-1326",
    )


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[auth_mod.require_admin] = _admin_user
    monkeypatch.setattr(router_mod, "_ensure_tenant", lambda user: None)
    return TestClient(app)


def _validation_types(response) -> set[str]:
    assert response.status_code == 422
    return {err["type"] for err in response.json()["detail"]}


# -- Pydantic request-body validation ---------------------------------------


def test_create_model_rejects_none_input() -> None:
    with pytest.raises(ValidationError):
        router_mod.LLMCredentialCreate.model_validate(None)


def test_create_model_accepts_empty_optional_fields() -> None:
    body = router_mod.LLMCredentialCreate.model_validate({
        "provider": "openai",
        "metadata": {},
    })

    assert body.label == ""
    assert body.value == ""
    assert body.metadata == {}


def test_create_model_rejects_wrong_types() -> None:
    with pytest.raises(ValidationError) as exc:
        router_mod.LLMCredentialCreate.model_validate({
            "provider": ["openai"],
            "metadata": ["not", "a", "dict"],
        })

    error_types = {err["type"] for err in exc.value.errors()}
    assert {"string_type", "dict_type"} <= error_types


def test_create_model_rejects_very_large_fields() -> None:
    with pytest.raises(ValidationError) as exc:
        router_mod.LLMCredentialCreate.model_validate({
            "provider": "openai",
            "label": "l" * 257,
            "value": "v" * 8193,
        })

    fields = {tuple(err["loc"]) for err in exc.value.errors()}
    assert {("label",), ("value",)} <= fields


def test_update_model_rejects_none_input() -> None:
    with pytest.raises(ValidationError):
        router_mod.LLMCredentialUpdate.model_validate(None)


def test_update_model_accepts_empty_update_collection() -> None:
    body = router_mod.LLMCredentialUpdate.model_validate({})

    assert body.model_dump(exclude_unset=True) == {}


def test_update_model_rejects_wrong_metadata_type() -> None:
    with pytest.raises(ValidationError) as exc:
        router_mod.LLMCredentialUpdate.model_validate({
            "metadata": ["not", "a", "dict"],
        })

    assert exc.value.errors()[0]["type"] == "dict_type"


def test_update_model_rejects_very_large_invalid_auth_type() -> None:
    with pytest.raises(ValidationError) as exc:
        router_mod.LLMCredentialUpdate.model_validate({
            "auth_type": "oauth" + ("x" * 9000),
        })

    assert exc.value.errors()[0]["type"] == "string_pattern_mismatch"


# -- HTTP/query validation ---------------------------------------------------


@pytest.mark.parametrize(
    ("query", "error_type"),
    [
        ("provider=not-a-provider", "string_pattern_mismatch"),
        ("provider=" + ("x" * 9000), "string_pattern_mismatch"),
        ("enabled_only=not-bool", "bool_parsing"),
    ],
)
def test_list_llm_credentials_rejects_invalid_query_values(
    client: TestClient,
    query: str,
    error_type: str,
) -> None:
    assert error_type in _validation_types(
        client.get(f"/llm-credentials?{query}")
    )


def test_create_llm_credential_rejects_none_body(client: TestClient) -> None:
    assert _validation_types(
        client.post(
            "/llm-credentials",
            content="null",
            headers={"Content-Type": "application/json"},
        )
    )


def test_create_llm_credential_rejects_empty_body(client: TestClient) -> None:
    assert "missing" in _validation_types(
        client.post("/llm-credentials", json={})
    )


def test_create_llm_credential_rejects_wrong_type_body(
    client: TestClient,
) -> None:
    errors = _validation_types(
        client.post(
            "/llm-credentials",
            json={"provider": ["openai"], "metadata": []},
        )
    )

    assert {"string_type", "dict_type"} <= errors


def test_create_llm_credential_rejects_very_large_body(
    client: TestClient,
) -> None:
    response = client.post(
        "/llm-credentials",
        json={
            "provider": "openai",
            "label": "l" * 257,
            "value": "v" * 8193,
        },
    )

    fields = {tuple(err["loc"]) for err in response.json()["detail"]}
    assert response.status_code == 422
    assert {("body", "label"), ("body", "value")} <= fields


def test_update_llm_credential_rejects_none_body(client: TestClient) -> None:
    assert _validation_types(
        client.patch(
            "/llm-credentials/lc-test",
            content="null",
            headers={"Content-Type": "application/json"},
        )
    )


def test_update_llm_credential_rejects_wrong_type_body(
    client: TestClient,
) -> None:
    assert "dict_type" in _validation_types(
        client.patch(
            "/llm-credentials/lc-test",
            json={"metadata": ["not", "a", "dict"]},
        )
    )


def test_update_llm_credential_rejects_very_large_invalid_auth_type(
    client: TestClient,
) -> None:
    assert "string_pattern_mismatch" in _validation_types(
        client.patch(
            "/llm-credentials/lc-test",
            json={"auth_type": "pat" + ("x" * 9000)},
        )
    )


def test_delete_llm_credential_rejects_wrong_type_query(
    client: TestClient,
) -> None:
    assert "bool_parsing" in _validation_types(
        client.delete(
            "/llm-credentials/lc-test?auto_elect_new_default=not-bool"
        )
    )


# -- Direct endpoint calls with service stubs --------------------------------


@pytest.mark.asyncio
async def test_list_llm_credentials_returns_empty_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list_credentials(*, provider, enabled_only):
        assert provider is None
        assert enabled_only is False
        return []

    monkeypatch.setattr(router_mod, "_ensure_tenant", lambda user: None)
    monkeypatch.setattr(router_mod._lc, "list_credentials", fake_list_credentials)

    out = await router_mod.list_llm_credentials(
        provider=None,
        enabled_only=False,
        user=_admin_user(),
    )

    assert out == {"items": [], "count": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_id", [None, {}, "x" * 9000])
async def test_get_llm_credential_invalid_id_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
    credential_id: object,
) -> None:
    async def fake_get_credential(seen_credential_id):
        assert seen_credential_id is credential_id
        return None

    monkeypatch.setattr(router_mod, "_ensure_tenant", lambda user: None)
    monkeypatch.setattr(router_mod._lc, "get_credential", fake_get_credential)

    with pytest.raises(HTTPException) as exc:
        await router_mod.get_llm_credential_endpoint(
            credential_id=credential_id,
            user=_admin_user(),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_llm_credential_empty_update_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = router_mod.LLMCredentialUpdate.model_validate({})

    async def fake_update_credential(credential_id, *, updates):
        assert credential_id == "lc-test"
        assert updates == {}
        return {"id": "lc-test", "label": "unchanged"}

    monkeypatch.setattr(router_mod, "_ensure_tenant", lambda user: None)
    monkeypatch.setattr(
        router_mod._lc, "update_credential", fake_update_credential
    )

    out = await router_mod.update_llm_credential(
        credential_id="lc-test",
        body=body,
        user=_admin_user(),
    )

    assert out == {"id": "lc-test", "label": "unchanged"}


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_id", [None, {}, "x" * 9000])
async def test_delete_llm_credential_invalid_id_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
    credential_id: object,
) -> None:
    async def fake_delete_credential(seen_credential_id, *, auto_elect_new_default):
        assert seen_credential_id is credential_id
        assert auto_elect_new_default is True
        raise router_mod._lc.LLMCredentialNotFound()

    monkeypatch.setattr(router_mod, "_ensure_tenant", lambda user: None)
    monkeypatch.setattr(
        router_mod._lc, "delete_credential", fake_delete_credential
    )

    with pytest.raises(HTTPException) as exc:
        await router_mod.delete_llm_credential(
            credential_id=credential_id,
            auto_elect_new_default=True,
            user=_admin_user(),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_id", [None, {}, "x" * 9000])
async def test_test_llm_credential_invalid_id_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
    credential_id: object,
) -> None:
    async def fake_get_credential(seen_credential_id):
        assert seen_credential_id is credential_id
        return None

    monkeypatch.setattr(router_mod, "_ensure_tenant", lambda user: None)
    monkeypatch.setattr(router_mod._lc, "get_credential", fake_get_credential)

    with pytest.raises(HTTPException) as exc:
        await router_mod.test_llm_credential(
            credential_id=credential_id,
            user=_admin_user(),
        )

    assert exc.value.status_code == 404
