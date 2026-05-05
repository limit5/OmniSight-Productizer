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
import subprocess
import sys

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
    tmp_path / ".secret_key"

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
        return httpx.Response(
            401,
            json={"error": {"type": "authentication_error",
                            "message": "invalid x-api-key"}},
        )

    _stub_httpx(monkeypatch, _unauth)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "anthropic", "api_key": "sk-ant-bogus"},
    )
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["kind"] == "key_invalid"
    # Detail must carry the kind-specific prefix + the provider name so
    # the wizard can show it verbatim without stringly parsing.
    assert body["detail"].startswith("Invalid API key —"), body
    assert "Anthropic" in body["detail"], body
    # Provider-supplied error message is echoed so the operator sees the
    # precise upstream reason.
    assert "invalid x-api-key" in body["detail"], body
    # No credential written on failure
    assert _secrets.get_provider_credentials("anthropic") == {}
    # Step row NOT recorded
    assert await _boot.get_bootstrap_step(_boot.STEP_LLM_PROVIDER) is None


@pytest.mark.asyncio
async def test_quota_exhausted_returns_429(_wizard_client, monkeypatch):
    client = _wizard_client["client"]

    def _quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"type": "rate_limit_exceeded",
                            "message": "Requests per minute limit hit"}},
        )

    _stub_httpx(monkeypatch, _quota)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "openai", "api_key": "sk-proj-quota"},
    )
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["kind"] == "quota_exceeded"
    assert body["detail"].startswith("Quota exceeded —"), body
    assert "OpenAI" in body["detail"], body
    assert "Requests per minute limit hit" in body["detail"], body


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
    body = r.json()
    assert body["kind"] == "network_unreachable"
    assert body["detail"].startswith("Cannot reach provider —"), body
    # Operator-friendly message mentions the remediation hints.
    assert "anthropic" in body["detail"].lower(), body
    assert ("firewall" in body["detail"] or "DNS" in body["detail"]), body


@pytest.mark.asyncio
async def test_provider_error_5xx_returns_502_with_prefix(
    _wizard_client, monkeypatch,
):
    """5xx from the provider → kind=provider_error with clear prefix."""
    client = _wizard_client["client"]

    def _boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="temporary overload, try later")

    _stub_httpx(monkeypatch, _boom)

    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "anthropic", "api_key": "sk-ant-live"},
    )
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["kind"] == "provider_error"
    assert body["detail"].startswith("Provider error —"), body
    assert "HTTP 503" in body["detail"], body


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
    body = r.json()
    assert body["kind"] == "key_invalid"
    assert body["detail"].startswith("Invalid API key —"), body


@pytest.mark.asyncio
async def test_unsupported_provider_422(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "mystery-llm", "api_key": "x"},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["kind"] == "bad_request"
    assert body["detail"].startswith("Bad request —"), body


@pytest.mark.asyncio
async def test_azure_requires_base_url(_wizard_client):
    client = _wizard_client["client"]
    r = await client.post(
        "/api/v1/bootstrap/llm-provision",
        json={"provider": "azure", "api_key": "key-azr"},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["kind"] == "bad_request"
    assert body["detail"].startswith("Bad request —"), body
    assert "endpoint" in body["detail"].lower(), body


@pytest.mark.asyncio
async def test_clear_message_prefix_covers_every_kind():
    """Every kind the router maps to HTTP gets a human-readable prefix.

    Keeps the ``BOOTSTRAP_PROVISION_KIND_COPY`` in ``lib/api.ts`` in lock
    step with the backend: if a new kind is added here without UI copy,
    the wizard banner would fall through to the generic "provider error"
    label.
    """
    from backend.routers.bootstrap import _PING_KIND_TO_STATUS

    for kind in _PING_KIND_TO_STATUS:
        msg = _secrets.clear_message(kind, "anthropic", "probe failed")
        assert kind in _secrets.KIND_PREFIX, f"missing prefix for {kind}"
        assert msg.startswith(_secrets.KIND_PREFIX[kind] + " —"), (kind, msg)
        assert "anthropic" in msg
        assert "probe failed" in msg


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


def test_provider_credentials_use_ks_envelope_and_survive_hard_restart(
    tmp_path, monkeypatch,
):
    """KS.1.11 compat regression for future provider keys.

    The bootstrap provider-key marker is written as a KS envelope
    carrier, and a fresh interpreter can decrypt it after a simulated
    hard restart with only the shared ``OMNISIGHT_SECRET_KEY``.
    """
    monkeypatch.setenv(
        "OMNISIGHT_SECRET_KEY",
        "ks-1-11-llm-hard-restart-secret",
    )
    from backend import secret_store
    secret_store._reset_for_tests()
    path = tmp_path / "provider-creds.enc"
    _secrets._reset_for_tests(path)

    _secrets.set_provider_credentials(
        "openai",
        api_key="sk-proj-hard-restart",
        model="gpt-4o-mini",
    )
    raw = path.read_text(encoding="ascii")
    outer = json.loads(raw)
    assert outer["fmt"] == _secrets._LLM_SECRET_ENVELOPE_FORMAT_VERSION
    assert outer["dek_ref"]["tenant_id"] == "t-default"
    assert (
        outer["dek_ref"]["encryption_context"]["purpose"]
        == "llm-provider-secrets"
    )
    assert "sk-proj-hard-restart" not in raw

    code = """
import json
import os
from pathlib import Path
from backend import llm_secrets

llm_secrets._reset_for_tests(Path(os.environ["KS111_LLM_SECRET_PATH"]))
print(json.dumps(llm_secrets.get_provider_credentials("openai"), sort_keys=True))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=True,
        env={
            **dict(os.environ),
            "KS111_LLM_SECRET_PATH": str(path),
        },
    )
    assert json.loads(proc.stdout)["api_key"] == "sk-proj-hard-restart"

    _secrets._reset_for_tests()
    secret_store._reset_for_tests()


def test_legacy_fernet_provider_credentials_are_deprecated(tmp_path, monkeypatch):
    """Existing Fernet-only provider-key files are treated as empty
    after the KS.1 compatibility window."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-11-legacy-llm-secret")
    from backend import secret_store
    secret_store._reset_for_tests()
    path = tmp_path / "legacy-creds.enc"
    path.write_text(
        secret_store.encrypt(json.dumps({
            "anthropic": {"api_key": "sk-ant-legacy", "model": "claude"},
        })),
        encoding="ascii",
    )
    _secrets._reset_for_tests(path)
    assert _secrets.get_provider_credentials("anthropic") == {}
    _secrets._reset_for_tests()
    secret_store._reset_for_tests()


def test_provider_credentials_envelope_disabled_env_still_writes_envelope(
    tmp_path, monkeypatch,
):
    """KS.1 completion: knob-off no longer writes provider credentials
    in the legacy single-Fernet marker format."""
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-12-llm-rollback-secret")
    from backend import secret_store
    from backend.security import envelope as tenant_envelope
    secret_store._reset_for_tests()
    monkeypatch.setenv(tenant_envelope.ENVELOPE_ENABLED_ENV, "false")
    path = tmp_path / "provider-creds-rollback.enc"
    _secrets._reset_for_tests(path)

    _secrets.set_provider_credentials("openai", api_key="sk-proj-rollback")
    raw = path.read_text(encoding="ascii")

    assert raw.lstrip().startswith("{")
    assert _secrets.get_provider_credentials("openai")["api_key"] == "sk-proj-rollback"
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
