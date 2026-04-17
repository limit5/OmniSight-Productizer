"""L3 Step 2 — ``POST /api/v1/bootstrap/llm-provision`` endpoint tests.

Covers the LLM provider provisioning flow driven by the wizard:

  * happy path — Anthropic key pinged OK → credentials stored encrypted,
    ``settings.llm_provider`` flipped, ``llm_provider_configured`` step
    recorded, audit row ``bootstrap.llm_provisioned`` written
  * Ollama local reachability probe succeeds → no API key required
  * key invalid → 401 with ``kind=key_invalid`` and no persistence
  * quota exhausted → 429 with ``kind=quota_exceeded``
  * network unreachable → 504 with ``kind=network_unreachable``
  * missing key for hosted provider → 422
  * unsupported provider → 422
  * azure without base_url → 422
  * secrets persisted on disk are encrypted (not plaintext)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest

from backend import bootstrap as _boot
from backend import llm_secrets as _secrets
from backend.config import settings as _settings


# ─────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture()
async def _wizard_client(tmp_path, monkeypatch):
    """Fresh sqlite + isolated bootstrap + secrets markers.

    Matches the ``_wizard_client`` pattern from the admin-password tests
    so we can drive the real FastAPI app through AsyncClient without
    the bootstrap gate middleware interfering.
    """
    db_path = tmp_path / "wizard.db"
    marker = tmp_path / "bootstrap.json"
    secret_path = tmp_path / "llm.enc"
    secret_key_path = tmp_path / ".secret_key"

    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-secret-key-material-abcdef")

    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()

    _boot._reset_for_tests(marker)
    _secrets._reset_for_tests(secret_path)
    # Reset fernet so the env-provided key takes effect for this test.
    from backend import secret_store
    secret_store._reset_for_tests()

    # Pin admin / settings baseline back to a clean slate.
    original_provider = _settings.llm_provider
    original_model = _settings.llm_model
    original_anth = _settings.anthropic_api_key
    original_openai = _settings.openai_api_key

    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    _boot._gate_cache_reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {
            "client": ac,
            "marker": marker,
            "secret_path": secret_path,
        }
    _boot._gate_cache_reset()
    _secrets._reset_for_tests()
    _settings.llm_provider = original_provider
    _settings.llm_model = original_model
    _settings.anthropic_api_key = original_anth
    _settings.openai_api_key = original_openai
    secret_store._reset_for_tests()
    await db.close()


def _stub_httpx(monkeypatch, handler):
    """Replace ``httpx.AsyncClient`` with one backed by a MockTransport.

    ``handler`` is a callable taking an ``httpx.Request`` and returning
    an ``httpx.Response`` — lets each test pin the provider's reply.
    """
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    class _StubClient(real_cls):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):  # noqa: D401
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("backend.llm_secrets.httpx.AsyncClient", _StubClient)


# ─────────────────────────────────────────────────────────────────
#  Happy paths
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_happy_path_persists_and_records(
    _wizard_client, monkeypatch,
):
    """Valid Anthropic key → 200, encrypted persistence, step recorded."""
    client = _wizard_client["client"]

    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        assert request.headers.get("x-api-key") == "sk-ant-test-happy"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-opus-4-7"},
                    {"id": "claude-sonnet-4-20250514"},
                ]
            },
        )

    _stub_httpx(monkeypatch, _ok)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={
            "provider": "anthropic",
            "api_key": "sk-ant-test-happy",
            "model": "claude-opus-4-7",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "provisioned"
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-opus-4-7"
    assert body["fingerprint"].endswith("appy")  # tail of sk-ant-test-happy
    assert "claude-opus-4-7" in body["models"]

    # Settings flipped for the live process
    assert _settings.llm_provider == "anthropic"
    assert _settings.anthropic_api_key == "sk-ant-test-happy"
    assert _settings.llm_model == "claude-opus-4-7"

    # Credential record decryptable + contains the stored key
    stored = _secrets.get_provider_credentials("anthropic")
    assert stored.get("api_key") == "sk-ant-test-happy"
    assert stored.get("model") == "claude-opus-4-7"

    # At-rest file is encrypted — the key must NOT appear in plaintext
    on_disk = _wizard_client["secret_path"].read_text(encoding="ascii")
    assert "sk-ant-test-happy" not in on_disk

    # Wizard step recorded
    step = await _boot.get_bootstrap_step(_boot.STEP_LLM_PROVIDER)
    assert step is not None
    assert step["metadata"]["provider"] == "anthropic"
    assert step["metadata"]["fingerprint"].endswith("appy")

    # Audit row written
    from backend import audit
    rows = await audit.query(entity_kind="bootstrap", limit=50)
    actions = [row["action"] for row in rows]
    assert "bootstrap.llm_provisioned" in actions


@pytest.mark.asyncio
async def test_ollama_local_probe_no_key(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        assert request.url.host == "localhost"
        return httpx.Response(
            200,
            json={"models": [{"name": "llama3.1:8b"}, {"name": "mistral"}]},
        )

    _stub_httpx(monkeypatch, _ok)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "ollama"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "ollama"
    assert body["fingerprint"] == "****"  # no key → fully masked
    assert "llama3.1:8b" in body["models"]
    assert _settings.llm_provider == "ollama"


@pytest.mark.asyncio
async def test_openai_happy_persists_and_records(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer sk-proj-openai-happy"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    _stub_httpx(monkeypatch, _ok)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "openai", "api_key": "sk-proj-openai-happy"},
    )
    assert r.status_code == 200, r.text
    assert _settings.openai_api_key == "sk-proj-openai-happy"
    assert _settings.llm_provider == "openai"


# ─────────────────────────────────────────────────────────────────
#  Error paths — provider ping failures
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_key_returns_401_and_no_persistence(
    _wizard_client, monkeypatch,
):
    client = _wizard_client["client"]

    def _unauth(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_api_key"})

    _stub_httpx(monkeypatch, _unauth)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "anthropic", "api_key": "sk-ant-bogus"},
    )
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["kind"] == "key_invalid"
    # No credential written on failure
    assert _secrets.get_provider_credentials("anthropic") == {}
    # Step row NOT recorded
    assert await _boot.get_bootstrap_step(_boot.STEP_LLM_PROVIDER) is None


@pytest.mark.asyncio
async def test_quota_exhausted_returns_429(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate_limit_exceeded"})

    _stub_httpx(monkeypatch, _quota)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "openai", "api_key": "sk-proj-quota"},
    )
    assert r.status_code == 429, r.text
    assert r.json()["kind"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_network_unreachable_returns_504(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    _stub_httpx(monkeypatch, _dead)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "anthropic", "api_key": "sk-ant-xyz"},
    )
    assert r.status_code == 504, r.text
    assert r.json()["kind"] == "network_unreachable"


# ─────────────────────────────────────────────────────────────────
#  Error paths — request shape
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_key_for_hosted_provider_422(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "anthropic"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["kind"] == "key_invalid"


@pytest.mark.asyncio
async def test_unsupported_provider_422(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "mystery-llm", "api_key": "x"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["kind"] == "bad_request"


@pytest.mark.asyncio
async def test_azure_requires_base_url(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "azure", "api_key": "key-azr"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["kind"] == "bad_request"


# ─────────────────────────────────────────────────────────────────
#  Secrets module unit coverage
# ─────────────────────────────────────────────────────────────────


def test_set_and_get_credentials_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "unit-test-secret-material-xyz")
    from backend import secret_store
    secret_store._reset_for_tests()
    _secrets._reset_for_tests(tmp_path / "creds.enc")

    _secrets.set_provider_credentials(
        "openai",
        api_key="sk-proj-unit-key",
        model="gpt-4o-mini",
    )
    back = _secrets.get_provider_credentials("openai")
    assert back["api_key"] == "sk-proj-unit-key"
    assert back["model"] == "gpt-4o-mini"

    # Listing redacts the key
    listing = _secrets.list_provider_fingerprints()
    assert listing["openai"]["has_key"] is True
    assert listing["openai"]["fingerprint"].endswith("-key")
    assert "sk-proj-unit-key" not in json.dumps(listing)

    # Second provider survives alongside first
    _secrets.set_provider_credentials("anthropic", api_key="sk-ant-another")
    assert _secrets.get_provider_credentials("openai")["api_key"] == "sk-proj-unit-key"
    assert _secrets.get_provider_credentials("anthropic")["api_key"] == "sk-ant-another"

    _secrets._reset_for_tests()
    secret_store._reset_for_tests()


def test_set_credentials_rejects_unknown_provider(tmp_path, monkeypatch):
    _secrets._reset_for_tests(tmp_path / "creds.enc")
    with pytest.raises(ValueError):
        _secrets.set_provider_credentials("mystery", api_key="x")
    _secrets._reset_for_tests()


def test_fingerprint_redacts():
    assert _secrets.fingerprint("") == "****"
    assert _secrets.fingerprint("short") == "****"
    long_key = "sk-ant-abcdefghijklmn"
    fp = _secrets.fingerprint(long_key)
    assert fp.startswith("…")
    assert fp.endswith(long_key[-4:])
