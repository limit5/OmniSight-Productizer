"""L3 Step 2 — ``GET /api/v1/bootstrap/ollama-detect`` endpoint tests.

Covers the read-only Ollama reachability probe used by the wizard's
LLM provider step when the operator picks "Ollama (local)":

  * happy path — ``/api/tags`` returns 200 with a model list →
    ``reachable=true`` and the models flow through unredacted
  * custom ``base_url`` query arg is honoured (and reported back)
  * connection refused → ``reachable=false`` + ``kind=network_unreachable``
  * provider replies 500 → ``reachable=false`` + ``kind=provider_error``
  * the probe never persists credentials, never records a bootstrap
    step, and never emits a ``bootstrap.llm_provisioned`` audit row
"""

from __future__ import annotations

import httpx
import pytest

from backend import bootstrap as _boot
from backend import llm_secrets as _secrets


@pytest.fixture()
async def _wizard_client(tmp_path, monkeypatch):
    db_path = tmp_path / "wizard.db"
    marker = tmp_path / "bootstrap.json"
    secret_path = tmp_path / "llm.enc"

    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-secret-key-material-abcdef")

    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()

    _boot._reset_for_tests(marker)
    _secrets._reset_for_tests(secret_path)
    from backend import secret_store
    secret_store._reset_for_tests()

    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    _boot._gate_cache_reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {"client": ac, "marker": marker, "secret_path": secret_path}
    _boot._gate_cache_reset()
    _secrets._reset_for_tests()
    secret_store._reset_for_tests()
    await db.close()


def _stub_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    class _StubClient(real_cls):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):  # noqa: D401
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("backend.llm_secrets.httpx.AsyncClient", _StubClient)


@pytest.mark.asyncio
async def test_ollama_detect_reachable_lists_models(_wizard_client, monkeypatch):
    client = _wizard_client["client"]
    calls: list[httpx.Request] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/tags"
        assert request.url.host == "localhost"
        assert request.url.port == 11434
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.1:8b"},
                    {"name": "qwen2:7b"},
                ]
            },
        )

    _stub_httpx(monkeypatch, _ok)

    r = await client.get(
        "/api/v1/bootstrap/ollama-detect",
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is True
    assert body["base_url"] == "http://localhost:11434"
    assert body["kind"] == ""
    assert body["detail"] == ""
    assert body["models"] == ["llama3.1:8b", "qwen2:7b"]
    assert isinstance(body["latency_ms"], int)
    assert len(calls) == 1

    # Probe is read-only — no credentials, no step row, no audit action.
    assert _secrets.get_provider_credentials("ollama") == {}
    assert await _boot.get_bootstrap_step(_boot.STEP_LLM_PROVIDER) is None
    from backend import audit
    rows = await audit.query(entity_kind="bootstrap", limit=50)
    assert all(row["action"] != "bootstrap.llm_provisioned" for row in rows)


@pytest.mark.asyncio
async def test_ollama_detect_honours_custom_base_url(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ollama.lan"
        assert request.url.port == 11435
        return httpx.Response(200, json={"models": []})

    _stub_httpx(monkeypatch, _ok)

    r = await client.get(
        "/api/v1/bootstrap/ollama-detect",
        params={"base_url": "http://ollama.lan:11435"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is True
    assert body["base_url"] == "http://ollama.lan:11435"
    assert body["models"] == []


@pytest.mark.asyncio
async def test_ollama_detect_connection_refused(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _stub_httpx(monkeypatch, _dead)

    r = await client.get(
        "/api/v1/bootstrap/ollama-detect",
        follow_redirects=False,
    )
    # Endpoint is always 200; reachability lives in the body so the UI
    # can render a "not reachable" affordance without parsing HTTP codes.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is False
    assert body["kind"] == "network_unreachable"
    assert "connection refused" in body["detail"] or "cannot reach" in body["detail"]
    assert body["models"] == []


@pytest.mark.asyncio
async def test_ollama_detect_provider_error(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _stub_httpx(monkeypatch, _boom)

    r = await client.get(
        "/api/v1/bootstrap/ollama-detect",
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is False
    assert body["kind"] == "provider_error"


@pytest.mark.asyncio
async def test_ollama_detect_empty_base_url_falls_back_to_default(
    _wizard_client, monkeypatch,
):
    client = _wizard_client["client"]
    observed: dict[str, str] = {}

    def _ok(request: httpx.Request) -> httpx.Response:
        observed["host"] = request.url.host
        observed["port"] = str(request.url.port)
        return httpx.Response(200, json={"models": [{"name": "tinyllama"}]})

    _stub_httpx(monkeypatch, _ok)

    r = await client.get(
        "/api/v1/bootstrap/ollama-detect",
        params={"base_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is True
    assert body["base_url"] == "http://localhost:11434"
    assert body["models"] == ["tinyllama"]
    assert observed == {"host": "localhost", "port": "11434"}
