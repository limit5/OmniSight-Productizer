"""M6 — Per-tenant egress allow-list tests.

Covers:
  * validate_host / validate_cidr / validate_default_action
  * get_policy / upsert_policy / list_policies
  * submit_request / list_requests / approve_request / reject_request
  * resolve_allow_targets DNS cache + CIDR passthrough
  * build_rule_plan deduplication + terminal action
  * policy_for legacy env fallback
  * sandbox_net.resolve_network_arg honours per-tenant policy
  * isolation: tenant A allow-list ≠ tenant B allow-list
  * REST endpoints: ACL, request submission, admin approval
  * audit chain entries
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestValidators:
    async def test_validate_host_simple(self):
        from backend import tenant_egress as te
        assert te.validate_host("api.openai.com") == "api.openai.com"
        assert te.validate_host("api.openai.com:443") == "api.openai.com:443"

    async def test_validate_host_lowercases(self):
        from backend import tenant_egress as te
        assert te.validate_host("API.OpenAI.com") == "api.openai.com"

    async def test_validate_host_rejects_empty(self):
        from backend import tenant_egress as te
        with pytest.raises(ValueError):
            te.validate_host("")
        with pytest.raises(ValueError):
            te.validate_host("   ")

    async def test_validate_host_rejects_metachars(self):
        from backend import tenant_egress as te
        for bad in ("a;rm -rf /", "host with space", "x|y", "$(whoami)"):
            with pytest.raises(ValueError):
                te.validate_host(bad)

    async def test_validate_host_rejects_bad_port(self):
        from backend import tenant_egress as te
        with pytest.raises(ValueError):
            te.validate_host("a.com:abc")
        with pytest.raises(ValueError):
            te.validate_host("a.com:99999")

    async def test_validate_cidr_accepts_v4_and_v6(self):
        from backend import tenant_egress as te
        assert te.validate_cidr("10.0.0.0/8") == "10.0.0.0/8"
        assert te.validate_cidr("::1/128") == "::1/128"

    async def test_validate_cidr_normalises_bare_ip(self):
        from backend import tenant_egress as te
        assert te.validate_cidr("1.2.3.4") == "1.2.3.4/32"

    async def test_validate_cidr_rejects_garbage(self):
        from backend import tenant_egress as te
        with pytest.raises(ValueError):
            te.validate_cidr("not-a-cidr")
        with pytest.raises(ValueError):
            te.validate_cidr("999.999.999.999/8")

    async def test_validate_default_action(self):
        from backend import tenant_egress as te
        assert te.validate_default_action("DENY") == "deny"
        assert te.validate_default_action("allow") == "allow"
        with pytest.raises(ValueError):
            te.validate_default_action("permit")

    async def test_validate_tenant_id(self):
        from backend import tenant_egress as te
        assert te.validate_tenant_id("t-default") == "t-default"
        with pytest.raises(ValueError):
            te.validate_tenant_id("")
        with pytest.raises(ValueError):
            te.validate_tenant_id("../etc/passwd")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Build rule plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRulePlan:
    async def test_plan_deduplicates_destinations(self):
        from backend import tenant_egress as te
        pol = te.EgressPolicy(
            tenant_id="t-a",
            allowed_hosts=("a.com", "b.com"),
            allowed_cidrs=("10.0.0.0/8",),
            default_action="deny",
        )
        resolved = {"a.com": ["1.1.1.1", "2.2.2.2"],
                    "b.com": ["1.1.1.1"],   # dup with a.com
                    "10.0.0.0/8": ["10.0.0.0/8"]}
        plan = te.build_rule_plan(pol, sandbox_uid=12345, resolved=resolved)
        dests = [r["destination"] for r in plan["rules"]]
        # 1.1.1.1 + 2.2.2.2 + 10.0.0.0/8 == 3 rules; dup folded.
        assert dests == ["1.1.1.1", "2.2.2.2", "10.0.0.0/8"]
        assert plan["default_action"] == "deny"
        assert all(r["uid_owner"] == 12345 for r in plan["rules"])

    async def test_plan_rejects_bad_uid(self):
        from backend import tenant_egress as te
        pol = te.EgressPolicy("t-a", (), (), "deny")
        with pytest.raises(ValueError):
            te.build_rule_plan(pol, sandbox_uid=0)

    async def test_plan_with_unresolved_host(self):
        from backend import tenant_egress as te
        pol = te.EgressPolicy("t-a", ("ghost.invalid",), (), "deny")
        plan = te.build_rule_plan(pol, sandbox_uid=12345,
                                  resolved={"ghost.invalid": []})
        assert plan["rules"] == []
        assert plan["default_action"] == "deny"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Policy CRUD against the real DB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "egress.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    await db.init()
    # Seed a couple of tenants so FK references resolve.
    conn = db._conn()
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
        ("t-alpha", "Alpha", "free"),
    )
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
        ("t-beta", "Beta", "pro"),
    )
    await conn.commit()
    yield db
    await db.close()


class TestPolicyCrud:
    async def test_get_policy_default_when_missing(self, fresh_db):
        from backend import tenant_egress as te
        pol = await te.get_policy("t-alpha")
        assert pol.tenant_id == "t-alpha"
        assert pol.allowed_hosts == ()
        assert pol.allowed_cidrs == ()
        assert pol.default_action == "deny"

    async def test_upsert_round_trip(self, fresh_db):
        from backend import tenant_egress as te
        pol = await te.upsert_policy(
            "t-alpha",
            allowed_hosts=["api.openai.com", "API.OpenAI.com:443"],
            allowed_cidrs=["10.0.0.0/8"],
            actor="user:admin@test",
        )
        assert pol.allowed_hosts == ("api.openai.com", "api.openai.com:443")
        assert pol.allowed_cidrs == ("10.0.0.0/8",)
        # Re-read from DB
        pol2 = await te.get_policy("t-alpha")
        assert pol2.allowed_hosts == pol.allowed_hosts
        assert pol2.allowed_cidrs == pol.allowed_cidrs

    async def test_upsert_rejects_invalid_entry(self, fresh_db):
        from backend import tenant_egress as te
        with pytest.raises(ValueError):
            await te.upsert_policy(
                "t-alpha", allowed_hosts=["good.com", "bad host"],
                actor="user:admin@test",
            )
        # No partial write: still empty.
        pol = await te.get_policy("t-alpha")
        assert pol.allowed_hosts == ()

    async def test_upsert_preserves_omitted_fields(self, fresh_db):
        from backend import tenant_egress as te
        await te.upsert_policy(
            "t-alpha", allowed_hosts=["a.com"], allowed_cidrs=["10.0.0.0/8"],
            actor="user:admin",
        )
        # Update only hosts; cidrs should persist
        await te.upsert_policy("t-alpha", allowed_hosts=["a.com", "b.com"], actor="user:admin")
        pol = await te.get_policy("t-alpha")
        assert pol.allowed_hosts == ("a.com", "b.com")
        assert pol.allowed_cidrs == ("10.0.0.0/8",)

    async def test_isolation_tenants_independent(self, fresh_db):
        from backend import tenant_egress as te
        await te.upsert_policy("t-alpha", allowed_hosts=["api.openai.com"], actor="admin")
        await te.upsert_policy("t-beta", allowed_cidrs=["10.0.0.0/8"], actor="admin")
        a = await te.get_policy("t-alpha")
        b = await te.get_policy("t-beta")
        assert a.allowed_hosts == ("api.openai.com",) and a.allowed_cidrs == ()
        assert b.allowed_hosts == () and b.allowed_cidrs == ("10.0.0.0/8",)

    async def test_list_policies_returns_only_set_rows(self, fresh_db):
        from backend import tenant_egress as te
        await te.upsert_policy("t-alpha", allowed_hosts=["a.com"], actor="admin")
        rows = await te.list_policies()
        tids = {r.tenant_id for r in rows}
        assert "t-alpha" in tids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request workflow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRequests:
    async def test_submit_lists_pending(self, fresh_db):
        from backend import tenant_egress as te
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer@x",
            kind="host", value="api.openai.com",
            justification="LLM provider",
        )
        assert req.status == "pending"
        assert req.kind == "host"
        rows = await te.list_requests(tenant_id="t-alpha", status="pending")
        assert len(rows) == 1 and rows[0].id == req.id

    async def test_submit_idempotent_dedup(self, fresh_db):
        from backend import tenant_egress as te
        a = await te.submit_request(
            "t-alpha", requested_by="user:viewer@x",
            kind="host", value="a.com",
        )
        b = await te.submit_request(
            "t-alpha", requested_by="user:viewer@x",
            kind="host", value="a.com",
        )
        assert a.id == b.id
        rows = await te.list_requests(tenant_id="t-alpha", status="pending")
        assert len(rows) == 1

    async def test_submit_invalid_kind_rejected(self, fresh_db):
        from backend import tenant_egress as te
        with pytest.raises(ValueError):
            await te.submit_request(
                "t-alpha", requested_by="user:x", kind="weird", value="a.com",
            )

    async def test_approve_merges_into_policy(self, fresh_db):
        from backend import tenant_egress as te
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer", kind="host", value="api.openai.com",
        )
        decided, pol = await te.approve_request(req.id, actor="user:admin", note="OK")
        assert decided.status == "approved"
        assert decided.decided_by == "user:admin"
        assert "api.openai.com" in pol.allowed_hosts

    async def test_approve_idempotent_against_existing_entry(self, fresh_db):
        from backend import tenant_egress as te
        await te.upsert_policy("t-alpha", allowed_hosts=["a.com"], actor="admin")
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer", kind="host", value="a.com",
        )
        _, pol = await te.approve_request(req.id, actor="user:admin")
        # Still only one entry (no duplicate).
        assert pol.allowed_hosts.count("a.com") == 1

    async def test_reject_does_not_touch_policy(self, fresh_db):
        from backend import tenant_egress as te
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer", kind="host", value="evil.com",
        )
        decided = await te.reject_request(req.id, actor="user:admin", note="nope")
        assert decided.status == "rejected"
        pol = await te.get_policy("t-alpha")
        assert "evil.com" not in pol.allowed_hosts

    async def test_double_approve_raises(self, fresh_db):
        from backend import tenant_egress as te
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer", kind="cidr", value="10.0.0.0/8",
        )
        await te.approve_request(req.id, actor="user:admin")
        with pytest.raises(ValueError):
            await te.approve_request(req.id, actor="user:admin")

    async def test_approved_cidr_lands_in_cidr_list(self, fresh_db):
        from backend import tenant_egress as te
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer", kind="cidr", value="10.0.0.0/8",
        )
        _, pol = await te.approve_request(req.id, actor="user:admin")
        assert "10.0.0.0/8" in pol.allowed_cidrs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  resolve_allow_targets — DNS + CIDR passthrough
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestResolve:
    async def test_cidrs_passthrough(self, fresh_db):
        from backend import tenant_egress as te
        te._reset_dns_cache_for_tests()
        pol = te.EgressPolicy("t-x", (), ("10.0.0.0/8", "192.168.0.0/16"), "deny")
        resolved = await te.resolve_allow_targets(pol)
        assert resolved == {"10.0.0.0/8": ["10.0.0.0/8"],
                            "192.168.0.0/16": ["192.168.0.0/16"]}

    async def test_dns_cache_used_within_ttl(self, fresh_db, monkeypatch):
        from backend import tenant_egress as te
        import asyncio
        te._reset_dns_cache_for_tests()
        calls = {"n": 0}

        async def fake_getaddrinfo(host, port, type=None):
            calls["n"] += 1
            return [(0, 0, 0, "", ("127.0.0.1", port or 0))]

        loop = asyncio.get_event_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo, raising=False)

        pol = te.EgressPolicy("t-x", ("example.com",), (), "deny")
        out1 = await te.resolve_allow_targets(pol, now=1000.0)
        out2 = await te.resolve_allow_targets(pol, now=1100.0)
        assert out1 == out2 == {"example.com": ["127.0.0.1"]}
        assert calls["n"] == 1

    async def test_dns_failure_maps_to_empty(self, fresh_db, monkeypatch):
        from backend import tenant_egress as te
        import asyncio
        te._reset_dns_cache_for_tests()

        async def fake_getaddrinfo(host, port, type=None):
            raise OSError("simulated DNS failure")

        loop = asyncio.get_event_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo, raising=False)

        pol = te.EgressPolicy("t-x", ("missing.invalid",), (), "deny")
        out = await te.resolve_allow_targets(pol)
        assert out["missing.invalid"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  policy_for legacy fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLegacyFallback:
    async def test_legacy_csv_picked_up_when_no_db_row(self, fresh_db, monkeypatch):
        from backend import tenant_egress as te
        from backend.config import settings as _settings
        monkeypatch.setattr(_settings, "t1_egress_allow_hosts",
                            "github.com,gerrit.internal:29418", raising=False)
        pol = await te.policy_for("t-alpha")  # no DB row exists
        assert "github.com" in pol.allowed_hosts
        assert "gerrit.internal:29418" in pol.allowed_hosts
        assert pol.updated_by == "legacy-env"

    async def test_db_row_wins_over_legacy(self, fresh_db, monkeypatch):
        from backend import tenant_egress as te
        from backend.config import settings as _settings
        await te.upsert_policy("t-alpha", allowed_hosts=["new.com"], actor="admin")
        monkeypatch.setattr(_settings, "t1_egress_allow_hosts", "legacy.com", raising=False)
        pol = await te.policy_for("t-alpha")
        assert pol.allowed_hosts == ("new.com",)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  sandbox_net.resolve_network_arg integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSandboxNetIntegration:
    async def test_per_tenant_policy_opens_egress(self, fresh_db, monkeypatch):
        from backend import sandbox_net as sn
        from backend import tenant_egress as te
        from backend.config import settings as _settings

        monkeypatch.setattr(_settings, "t1_allow_egress", False, raising=False)
        monkeypatch.setattr(_settings, "t1_egress_allow_hosts", "", raising=False)
        await te.upsert_policy("t-alpha", allowed_hosts=["1.2.3.4"], actor="admin")

        async def fake_runner(cmd, timeout=10):
            if "network ls" in cmd:
                return (0, sn.T1_NETWORK_NAME, "")
            return (0, "", "")
        sn._reset_dns_cache_for_tests()
        arg = await sn.resolve_network_arg(runner=fake_runner, tenant_id="t-alpha")
        assert arg == f"--network {sn.T1_NETWORK_NAME}"

    async def test_tenant_without_policy_remains_air_gapped(self, fresh_db, monkeypatch):
        from backend import sandbox_net as sn
        from backend.config import settings as _settings

        monkeypatch.setattr(_settings, "t1_allow_egress", False, raising=False)
        monkeypatch.setattr(_settings, "t1_egress_allow_hosts", "", raising=False)

        async def fake_runner(cmd, timeout=10):
            if "network ls" in cmd:
                return (0, sn.T1_NETWORK_NAME, "")
            return (0, "", "")
        sn._reset_dns_cache_for_tests()
        arg = await sn.resolve_network_arg(runner=fake_runner, tenant_id="t-beta")
        assert arg == "--network none"

    async def test_a_and_b_isolated_at_launch(self, fresh_db, monkeypatch):
        """Acceptance: A allows api.openai.com, B internal CIDR only."""
        from backend import sandbox_net as sn
        from backend import tenant_egress as te
        from backend.config import settings as _settings

        monkeypatch.setattr(_settings, "t1_allow_egress", False, raising=False)
        monkeypatch.setattr(_settings, "t1_egress_allow_hosts", "", raising=False)
        await te.upsert_policy("t-alpha", allowed_hosts=["api.openai.com"], actor="admin")
        await te.upsert_policy("t-beta", allowed_cidrs=["10.0.0.0/8"], actor="admin")

        a = await te.policy_for("t-alpha")
        b = await te.policy_for("t-beta")
        assert a.allowed_hosts == ("api.openai.com",) and a.allowed_cidrs == ()
        assert b.allowed_hosts == () and b.allowed_cidrs == ("10.0.0.0/8",)

        # Build rule plans that the iptables installer would consume.
        plan_a = te.build_rule_plan(a, sandbox_uid=10001,
                                    resolved={"api.openai.com": ["1.2.3.4"]})
        plan_b = te.build_rule_plan(b, sandbox_uid=10002,
                                    resolved={"10.0.0.0/8": ["10.0.0.0/8"]})
        a_dests = {r["destination"] for r in plan_a["rules"]}
        b_dests = {r["destination"] for r in plan_b["rules"]}
        assert a_dests == {"1.2.3.4"}
        assert b_dests == {"10.0.0.0/8"}
        # No overlap — A cannot reach B's CIDR and vice versa.
        assert a_dests.isdisjoint(b_dests)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
async def http_client(tmp_path, monkeypatch):
    db_path = tmp_path / "egress_http.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    from backend.main import app
    await db.init()
    conn = db._conn()
    await conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, ?)",
        ("t-alpha", "Alpha", "free"),
    )
    await conn.commit()

    # Finalize bootstrap so the bootstrap gate middleware doesn't
    # intercept API calls with 503 "bootstrap_required".
    from backend import bootstrap as _boot
    _boot._gate_cache["finalized"] = True

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        _boot._gate_cache["finalized"] = False  # reset for other tests
        await db.close()


class TestRestApi:
    async def test_get_my_egress_returns_default(self, http_client: AsyncClient):
        r = await http_client.get("/api/v1/tenants/me/egress")
        assert r.status_code == 200
        body = r.json()
        assert body["policy"]["tenant_id"] == "t-default"
        assert body["policy"]["default_action"] == "deny"

    async def test_admin_put_then_get(self, http_client: AsyncClient):
        r = await http_client.put(
            "/api/v1/tenants/t-alpha/egress",
            json={"allowed_hosts": ["api.openai.com"], "allowed_cidrs": ["10.0.0.0/8"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["policy"]["allowed_hosts"] == ["api.openai.com"]

        r2 = await http_client.get("/api/v1/tenants/t-alpha/egress")
        assert r2.status_code == 200
        assert r2.json()["policy"]["allowed_cidrs"] == ["10.0.0.0/8"]

    async def test_put_rejects_invalid_host(self, http_client: AsyncClient):
        r = await http_client.put(
            "/api/v1/tenants/t-alpha/egress",
            json={"allowed_hosts": ["bad host"]},
        )
        assert r.status_code == 400

    async def test_request_submission_and_approval(self, http_client: AsyncClient):
        r = await http_client.post(
            "/api/v1/tenants/me/egress/requests",
            json={"kind": "host", "value": "api.openai.com",
                  "justification": "LLM"},
        )
        assert r.status_code == 201, r.text
        rid = r.json()["request"]["id"]

        # admin lists
        listed = await http_client.get("/api/v1/tenants/egress/requests?status=pending")
        assert listed.status_code == 200
        assert any(req["id"] == rid for req in listed.json()["requests"])

        # admin approves
        ap = await http_client.post(
            f"/api/v1/tenants/egress/requests/{rid}/approve",
            json={"note": "OK"},
        )
        assert ap.status_code == 200
        assert ap.json()["request"]["status"] == "approved"
        assert "api.openai.com" in ap.json()["policy"]["allowed_hosts"]

    async def test_request_submission_missing_value(self, http_client: AsyncClient):
        r = await http_client.post(
            "/api/v1/tenants/me/egress/requests",
            json={"kind": "host"},
        )
        assert r.status_code == 400

    async def test_approve_unknown_returns_404(self, http_client: AsyncClient):
        r = await http_client.post(
            "/api/v1/tenants/egress/requests/egr-deadbeef/approve",
        )
        assert r.status_code == 404

    async def test_reject_then_double_reject_conflicts(self, http_client: AsyncClient):
        r = await http_client.post(
            "/api/v1/tenants/me/egress/requests",
            json={"kind": "cidr", "value": "172.16.0.0/12"},
        )
        rid = r.json()["request"]["id"]
        rej = await http_client.post(
            f"/api/v1/tenants/egress/requests/{rid}/reject",
            json={"note": "no"},
        )
        assert rej.status_code == 200
        again = await http_client.post(
            f"/api/v1/tenants/egress/requests/{rid}/reject",
        )
        assert again.status_code == 409

    async def test_dns_cache_reset(self, http_client: AsyncClient):
        await http_client.put(
            "/api/v1/tenants/t-alpha/egress",
            json={"allowed_cidrs": ["10.0.0.0/8"]},
        )
        r = await http_client.post(
            "/api/v1/tenants/t-alpha/egress/dns-cache/reset",
        )
        assert r.status_code == 200
        assert r.json()["resolved"]["10.0.0.0/8"] == ["10.0.0.0/8"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit chain hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAudit:
    async def test_upsert_emits_audit(self, fresh_db):
        from backend import tenant_egress as te
        from backend import audit as _audit
        await te.upsert_policy("t-alpha", allowed_hosts=["a.com"], actor="user:admin@x")
        rows = await _audit.query(entity_kind="tenant_egress", limit=20)
        upserts = [r for r in rows if r["action"] == "tenant_egress.upsert"]
        assert any(r["entity_id"] == "t-alpha" and r["actor"] == "user:admin@x"
                   for r in upserts)

    async def test_request_lifecycle_emits_three_events(self, fresh_db):
        from backend import tenant_egress as te
        from backend import audit as _audit
        req = await te.submit_request(
            "t-alpha", requested_by="user:viewer", kind="host", value="a.com",
        )
        await te.approve_request(req.id, actor="user:admin")
        rows = await _audit.query(entity_kind="tenant_egress", limit=50)
        actions = {r["action"] for r in rows}
        assert "tenant_egress.request_submit" in actions
        assert "tenant_egress.request_approve" in actions
        # Approve also writes an upsert event because the policy changes.
        assert "tenant_egress.upsert" in actions
