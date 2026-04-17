"""L4 Step 3 — ``POST /api/v1/bootstrap/cf-tunnel-skip`` endpoint tests.

Validates the wizard's LAN-only escape hatch:

  * happy path — 200, ``cf_tunnel_skipped`` marker written, STEP_CF_TUNNEL
    recorded with ``metadata.skipped=true`` + the operator's free-text
    reason
  * operator reason is optional — empty/missing body still succeeds
  * audit row ``bootstrap.cf_tunnel_skipped`` is emitted with
    ``severity=warning`` so the choice is traceable
  * endpoint is unauthenticated (mirrors the other wizard steps)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from backend import bootstrap as _boot


@pytest.fixture()
def _marker_tmp():
    """Isolate the bootstrap marker + CF router state so neither leaks.

    The provision test exercises ``POST /cloudflare/provision`` end-to-end,
    which writes ``tunnel_id`` into the module-level ``_stored_state`` of
    :mod:`backend.routers.cloudflare_tunnel`. Without an explicit reset
    that value survives into later tests (e.g. ``test_bootstrap.py``'s
    marker-roundtrip assertions), where ``_cf_tunnel_is_configured`` then
    trips the in-memory branch instead of the marker branch under test.
    """
    from backend.routers import cloudflare_tunnel as _cft

    tmp = tempfile.mkdtemp(prefix="omnisight_cf_skip_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    _cft._reset_for_tests()
    try:
        yield
    finally:
        _boot._reset_for_tests()
        _cft._reset_for_tests()


@pytest.mark.asyncio
async def test_cf_tunnel_skip_happy_path(client, _marker_tmp):
    """Skip call flips marker + records STEP_CF_TUNNEL in bootstrap_state."""
    r = await client.post(
        "/api/v1/bootstrap/cf-tunnel-skip",
        json={"reason": "air-gapped lab install"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"status": "skipped", "cf_tunnel_configured": True}

    # Marker: cf_tunnel_skipped flag set, configured flag NOT set.
    marker = _boot._read_marker()
    assert marker.get("cf_tunnel_skipped") is True
    assert "cf_tunnel_configured" not in marker

    # bootstrap_state row: metadata captures the skip intent + reason.
    row = await _boot.get_bootstrap_step(_boot.STEP_CF_TUNNEL)
    assert row is not None
    assert row["metadata"]["skipped"] is True
    assert row["metadata"]["reason"] == "air-gapped lab install"
    assert row["metadata"]["source"] == "wizard"


@pytest.mark.asyncio
async def test_cf_tunnel_skip_without_reason(client, _marker_tmp):
    """Reason is optional; empty body + default reason still succeeds."""
    r = await client.post(
        "/api/v1/bootstrap/cf-tunnel-skip",
        json={},
        follow_redirects=False,
    )
    assert r.status_code == 200
    row = await _boot.get_bootstrap_step(_boot.STEP_CF_TUNNEL)
    assert row is not None
    assert row["metadata"]["reason"] == ""


@pytest.mark.asyncio
async def test_cf_tunnel_skip_emits_audit_warning(client, _marker_tmp):
    """The skip must leave an audit trail — severity=warning."""
    from backend import audit

    r = await client.post(
        "/api/v1/bootstrap/cf-tunnel-skip",
        json={"reason": "offline deploy"},
        follow_redirects=False,
    )
    assert r.status_code == 200

    rows = await audit.query(limit=50)
    skip_rows = [r for r in rows if r.get("action") == "bootstrap.cf_tunnel_skipped"]
    assert len(skip_rows) == 1, f"expected single audit row, got {len(skip_rows)}"
    after = skip_rows[0].get("after") or {}
    assert after.get("skipped") is True
    assert after.get("reason") == "offline deploy"
    assert after.get("severity") == "warning"


@pytest.mark.asyncio
async def test_cf_tunnel_skip_makes_gate_green(client, _marker_tmp):
    """After skip, ``_cf_tunnel_is_configured`` probe returns True."""
    assert _boot._cf_tunnel_is_configured() is False

    r = await client.post(
        "/api/v1/bootstrap/cf-tunnel-skip",
        json={"reason": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert _boot._cf_tunnel_is_configured() is True


# ── L4 Step 3 — provision path writes STEP_CF_TUNNEL ──────────────


@pytest.mark.asyncio
async def test_cf_tunnel_provision_records_bootstrap_step(client, _marker_tmp):
    """B12 provision endpoint must record STEP_CF_TUNNEL + set marker.

    Uses respx to mock the Cloudflare API so the provisioning flow is
    exercised end-to-end — mirrors ``test_cloudflare_tunnel::test_provision_full_cycle``
    but asserts the L4 Step 3 side-effect specifically.
    """
    import respx
    import httpx

    CF = "https://api.cloudflare.com/client/v4"

    def _ok(result):
        return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": result})

    with respx.mock:
        respx.get(f"{CF}/user/tokens/verify").mock(return_value=_ok({"status": "active"}))
        respx.get(f"{CF}/accounts").mock(
            return_value=_ok([{"id": "acc-1", "name": "Test"}]),
        )
        r = await client.post(
            "/api/v1/cloudflare/validate-token",
            json={"api_token": "cf-test-token-12345678"},
        )
        assert r.status_code == 200, r.text

        respx.get(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_ok([]))
        respx.post(f"{CF}/accounts/acc-1/cfd_tunnel").mock(return_value=_ok({
            "id": "tun-1", "name": "omnisight", "status": "inactive",
            "created_at": "2026-01-01T00:00:00Z",
        }))
        respx.put(
            f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/configurations",
        ).mock(return_value=_ok({}))
        respx.get(
            f"{CF}/accounts/acc-1/cfd_tunnel/tun-1/token",
        ).mock(return_value=_ok("connector-tok"))
        respx.post(f"{CF}/zones/zone-1/dns_records").mock(return_value=_ok({
            "id": "rec-1", "name": "omnisight.example.com",
            "type": "CNAME", "content": "tun-1.cfargotunnel.com",
        }))

        r = await client.post(
            "/api/v1/cloudflare/provision",
            json={
                "account_id": "acc-1", "zone_id": "zone-1",
                "zone_name": "example.com", "tunnel_name": "omnisight",
            },
        )
        assert r.status_code == 200, r.text

    marker = _boot._read_marker()
    assert marker.get("cf_tunnel_configured") is True

    row = await _boot.get_bootstrap_step(_boot.STEP_CF_TUNNEL)
    assert row is not None
    assert row["metadata"].get("skipped") is not True
    assert row["metadata"]["tunnel_id"] == "tun-1"
    assert row["metadata"]["source"] == "wizard"
