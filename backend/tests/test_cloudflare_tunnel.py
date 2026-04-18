"""B12 — Cloudflare Tunnel wizard tests.

Uses respx to mock all Cloudflare API v4 calls. Covers:
  - Token validation (success / invalid / missing scope)
  - Zone listing
  - Full provision → status → teardown cycle
  - Existing tunnel (conflict / idempotent reuse)
  - DNS already exists (conflict handling)
  - Rate limit mapping
  - Partial-failure rollback
  - Token rotation
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from backend.cloudflare_client import CF_API_BASE, CloudflareClient

# ── Fixtures ──────────────────────────────────────────────────────

CF = CF_API_BASE


def _cf_ok(result):
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def _cf_err(status, code=0, msg="error"):
    return httpx.Response(status, json={"success": False, "errors": [{"code": code, "message": msg}]})


@pytest.fixture(autouse=True)
def _reset_router_state():
    from backend.routers.cloudflare_tunnel import _reset_for_tests
    _reset_for_tests()
    from backend.secret_store import _reset_for_tests as _reset_sec
    _reset_sec()
    yield
    _reset_for_tests()


# ── Unit tests: CloudflareClient ─────────────────────────────────

class TestCloudflareClient:

    @respx.mock
    @pytest.mark.asyncio
    async def test_verify_token_success(self):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        client = CloudflareClient("test-token")
        result = await client.verify_token()
        assert result["status"] == "active"

    @respx.mock
    @pytest.mark.asyncio
    async def test_verify_token_invalid(self):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_err(401, msg="Invalid API Token"))
        client = CloudflareClient("bad-token")
        from backend.cloudflare_client import InvalidTokenError
        with pytest.raises(InvalidTokenError):
            await client.verify_token()

    @respx.mock
    @pytest.mark.asyncio
    async def test_verify_token_forbidden(self):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_err(403, msg="Missing scope"))
        client = CloudflareClient("limited-token")
        from backend.cloudflare_client import MissingScopeError
        with pytest.raises(MissingScopeError):
            await client.verify_token()

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_accounts(self):
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([
            {"id": "acc-1", "name": "My Account"},
        ]))
        client = CloudflareClient("test-token")
        accounts = await client.list_accounts()
        assert len(accounts) == 1
        assert accounts[0].id == "acc-1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_zones(self):
        respx.get(f"{CF}/zones").mock(return_value=_cf_ok([
            {"id": "zone-1", "name": "example.com", "account": {"id": "acc-1"}, "status": "active"},
        ]))
        client = CloudflareClient("test-token")
        zones = await client.list_zones("acc-1")
        assert len(zones) == 1
        assert zones[0].name == "example.com"

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_tunnel(self):
        respx.post(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok({
            "id": "tun-1", "name": "omnisight", "status": "inactive", "created_at": "2026-01-01T00:00:00Z",
        }))
        client = CloudflareClient("test-token")
        secret = base64.b64encode(b"x" * 32).decode()
        tunnel = await client.create_tunnel("acc-1", "omnisight", secret)
        assert tunnel.id == "tun-1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_tunnel_conflict(self):
        respx.post(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_err(409, msg="Tunnel already exists"))
        client = CloudflareClient("test-token")
        from backend.cloudflare_client import ConflictError
        with pytest.raises(ConflictError):
            await client.create_tunnel("acc-1", "omnisight", base64.b64encode(b"x" * 32).decode())

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit(self):
        respx.get(f"{CF}/accounts").mock(return_value=httpx.Response(
            429, json={"success": False, "errors": [{"message": "Rate limited"}]},
            headers={"Retry-After": "30"},
        ))
        client = CloudflareClient("test-token")
        from backend.cloudflare_client import RateLimitError
        with pytest.raises(RateLimitError) as exc_info:
            await client.list_accounts()
        assert exc_info.value.retry_after == 30

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_dns_cname(self):
        respx.post(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_ok({
            "id": "rec-1", "name": "omnisight.example.com", "type": "CNAME", "content": "tun-1.cfargotunnel.com",
        }))
        client = CloudflareClient("test-token")
        rec = await client.create_dns_cname("zone-1", "omnisight.example.com", "tun-1.cfargotunnel.com")
        assert rec.id == "rec-1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_delete_tunnel(self):
        respx.delete(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1").mock(return_value=_cf_ok({}))
        client = CloudflareClient("test-token")
        await client.delete_tunnel("acc-1", "tun-1")

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_tunnel_token(self):
        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/token").mock(
            return_value=_cf_ok("eyJ0..connector-token")
        )
        client = CloudflareClient("test-token")
        tok = await client.get_tunnel_token("acc-1", "tun-1")
        assert "connector-token" in tok

    @respx.mock
    @pytest.mark.asyncio
    async def test_put_tunnel_config(self):
        respx.put(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/configurations").mock(return_value=_cf_ok({}))
        client = CloudflareClient("test-token")
        resp = await client.put_tunnel_config("acc-1", "tun-1", {"ingress": []})
        assert resp["success"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_dns_records(self):
        respx.get(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_ok([
            {"id": "rec-1", "name": "omnisight.example.com", "type": "CNAME", "content": "tun.cfargotunnel.com"},
        ]))
        client = CloudflareClient("test-token")
        recs = await client.list_dns_records("zone-1", name="omnisight.example.com")
        assert len(recs) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_delete_dns_record(self):
        respx.delete(f"{CF}/zones/zone-1/dns_records/rec-1").mock(return_value=_cf_ok({}))
        client = CloudflareClient("test-token")
        await client.delete_dns_record("zone-1", "rec-1")


# ── Integration tests: Router endpoints ──────────────────────────

class TestRouterEndpoints:

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_token_endpoint(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([
            {"id": "acc-1", "name": "Test Account"},
        ]))
        resp = await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-test-token-12345678"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert len(data["accounts"]) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_token_invalid(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_err(401, msg="Bad token"))
        resp = await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "bad"})
        assert resp.status_code == 401

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_token_missing_scope(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_err(403, msg="Missing scope"))
        resp = await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "limited"})
        assert resp.status_code == 403

    @respx.mock
    @pytest.mark.asyncio
    async def test_zones_endpoint(self, client):
        # First validate token to store it
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([{"id": "acc-1", "name": "Test"}]))
        await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-test-token-12345678"})

        respx.get(f"{CF}/zones").mock(return_value=_cf_ok([
            {"id": "zone-1", "name": "example.com", "account": {"id": "acc-1"}, "status": "active"},
        ]))
        resp = await client.get("/api/v1/cloudflare/zones?account_id=acc-1")
        assert resp.status_code == 200
        zones = resp.json()
        assert len(zones) == 1
        assert zones[0]["name"] == "example.com"

    @respx.mock
    @pytest.mark.asyncio
    async def test_provision_full_cycle(self, client):
        # Step 1: validate token
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([{"id": "acc-1", "name": "Test"}]))
        await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-test-token-12345678"})

        # Step 2: provision
        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok([]))
        respx.post(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok({
            "id": "tun-1", "name": "omnisight", "status": "inactive", "created_at": "2026-01-01T00:00:00Z",
        }))
        respx.put(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/configurations").mock(return_value=_cf_ok({}))
        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/token").mock(return_value=_cf_ok("connector-tok"))
        respx.post(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_ok({
            "id": "rec-1", "name": "omnisight.example.com", "type": "CNAME", "content": "tun-1.cfargotunnel.com",
        }))

        resp = await client.post("/api/v1/cloudflare/provision", json={
            "account_id": "acc-1", "zone_id": "zone-1", "zone_name": "example.com",
            "tunnel_name": "omnisight",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["tunnel_id"] == "tun-1"
        assert data["connector_token_set"] is True

        # Step 3: status
        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok([{
            "id": "tun-1", "name": "omnisight", "status": "active", "created_at": "2026-01-01T00:00:00Z",
            "connections": [{"is_pending_reconnect": False}],
        }]))
        respx.get(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_ok([
            {"id": "rec-1", "name": "omnisight.example.com", "type": "CNAME", "content": "tun-1.cfargotunnel.com"},
        ]))
        resp = await client.get("/api/v1/cloudflare/status")
        assert resp.status_code == 200
        status = resp.json()
        assert status["provisioned"] is True
        assert status["connector_online"] is True

        # Step 4: teardown
        respx.get(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_ok([
            {"id": "rec-1", "name": "omnisight.example.com", "type": "CNAME", "content": "tun-1.cfargotunnel.com"},
        ]))
        respx.delete(f"{CF}/zones/zone-1/dns_records/rec-1").mock(return_value=_cf_ok({}))
        respx.delete(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1").mock(return_value=_cf_ok({}))
        resp = await client.delete("/api/v1/cloudflare/tunnel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tunnel_deleted"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_provision_reuses_existing_tunnel(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([{"id": "acc-1", "name": "Test"}]))
        await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-test-token-12345678"})

        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok([{
            "id": "tun-existing", "name": "omnisight", "status": "active", "created_at": "2026-01-01T00:00:00Z",
        }]))
        respx.put(f"{CF}/accounts/acc-1/cfd_tunnel/tun-existing/configurations").mock(return_value=_cf_ok({}))
        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel/tun-existing/token").mock(return_value=_cf_ok("tok"))
        respx.post(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_ok({
            "id": "rec-1", "name": "omnisight.example.com", "type": "CNAME", "content": "tun-existing.cfargotunnel.com",
        }))

        resp = await client.post("/api/v1/cloudflare/provision", json={
            "account_id": "acc-1", "zone_id": "zone-1", "zone_name": "example.com",
        })
        assert resp.status_code == 200
        assert resp.json()["tunnel_id"] == "tun-existing"

    @respx.mock
    @pytest.mark.asyncio
    async def test_provision_dns_conflict_skips(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([{"id": "acc-1", "name": "Test"}]))
        await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-test-token-12345678"})

        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok([]))
        respx.post(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok({
            "id": "tun-1", "name": "omnisight", "status": "inactive", "created_at": "2026-01-01T00:00:00Z",
        }))
        respx.put(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/configurations").mock(return_value=_cf_ok({}))
        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/token").mock(return_value=_cf_ok("tok"))
        respx.post(f"{CF}/zones/zone-1/dns_records").mock(return_value=_cf_err(409, msg="Record exists"))

        resp = await client.post("/api/v1/cloudflare/provision", json={
            "account_id": "acc-1", "zone_id": "zone-1", "zone_name": "example.com",
        })
        assert resp.status_code == 200
        assert resp.json()["dns_records_created"] == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_provision_rollback_on_failure(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([{"id": "acc-1", "name": "Test"}]))
        await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-test-token-12345678"})

        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok([]))
        respx.post(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_cf_ok({
            "id": "tun-1", "name": "omnisight", "status": "inactive", "created_at": "2026-01-01T00:00:00Z",
        }))
        # Config PUT fails
        respx.put(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/configurations").mock(return_value=_cf_err(500, msg="Internal error"))
        # Rollback should delete the tunnel
        respx.delete(f"{CF}/accounts/acc-1/cfd_tunnel/tun-1").mock(return_value=_cf_ok({}))

        resp = await client.post("/api/v1/cloudflare/provision", json={
            "account_id": "acc-1", "zone_id": "zone-1", "zone_name": "example.com",
        })
        assert resp.status_code == 502

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_endpoint(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=httpx.Response(
            429, json={"success": False, "errors": [{"message": "Rate limited"}]},
            headers={"Retry-After": "60"},
        ))
        resp = await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "tok"})
        assert resp.status_code == 429

    @respx.mock
    @pytest.mark.asyncio
    async def test_status_no_tunnel(self, client):
        resp = await client.get("/api/v1/cloudflare/status")
        assert resp.status_code == 200
        assert resp.json()["provisioned"] is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_teardown_no_tunnel(self, client):
        resp = await client.delete("/api/v1/cloudflare/tunnel")
        assert resp.status_code == 404

    @respx.mock
    @pytest.mark.asyncio
    async def test_rotate_token(self, client):
        # First validate to store token
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(return_value=_cf_ok([{"id": "acc-1", "name": "Test"}]))
        await client.post("/api/v1/cloudflare/validate-token", json={"api_token": "cf-old-token-12345678"})

        # Rotate
        resp = await client.post("/api/v1/cloudflare/rotate-token", json={"new_api_token": "cf-new-token-87654321"})
        assert resp.status_code == 200
        assert resp.json()["rotated"] is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_rotate_token_invalid(self, client):
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_cf_err(401, msg="Bad"))
        resp = await client.post("/api/v1/cloudflare/rotate-token", json={"new_api_token": "bad"})
        assert resp.status_code == 401


# ── Unit tests: secrets module ───────────────────────────────────

class TestSecrets:

    def test_encrypt_decrypt_roundtrip(self):
        from backend.secret_store import _reset_for_tests, encrypt, decrypt
        _reset_for_tests()
        plaintext = "cf-api-token-abc123"
        cipher = encrypt(plaintext)
        assert cipher != plaintext
        assert decrypt(cipher) == plaintext

    def test_fingerprint(self):
        from backend.secret_store import fingerprint
        assert fingerprint("abcdefghijklmnop") == "…mnop"
        assert fingerprint("short") == "****"


# ── Unit tests: cloudflared service ──────────────────────────────

class TestCloudflaredService:

    def test_detect_mode_container_env(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CF_MODE", "container")
        from backend.cloudflared_service import detect_mode, ServiceMode
        assert detect_mode() == ServiceMode.container

    def test_sudoers_snippet(self):
        from backend.cloudflared_service import generate_sudoers_snippet
        snippet = generate_sudoers_snippet()
        assert "NOPASSWD" in snippet
        assert "cloudflared.service" in snippet
