"""C21 — L4-CORE-21 Enterprise web stack pattern tests (#242).

Covers: Auth, RBAC, Audit (hash-chain), Reports, i18n, Multi-tenant,
Import/Export, Workflow engine, Test recipes, Artifacts, Gate validation.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from openpyxl import load_workbook

from backend import enterprise_web_stack as ews


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _clean_state():
    ews.reload_config()
    ews._sessions.clear()
    ews._audit_entries.clear()
    ews._tenants.clear()
    ews._workflow_instances.clear()
    ews._i18n_bundles.clear()
    yield
    ews._sessions.clear()
    ews._audit_entries.clear()
    ews._tenants.clear()
    ews._workflow_instances.clear()
    ews._i18n_bundles.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfig:
    def test_load_config(self):
        cfg = ews._load_config()
        assert "auth" in cfg
        assert "rbac" in cfg
        assert "audit" in cfg
        assert "reports" in cfg
        assert "i18n" in cfg
        assert "multi_tenant" in cfg
        assert "import_export" in cfg
        assert "workflow_engine" in cfg

    def test_reload_config(self):
        cfg1 = ews._load_config()
        cfg2 = ews.reload_config()
        assert cfg1.keys() == cfg2.keys()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAuth:
    def test_list_providers(self):
        providers = ews.list_auth_providers()
        assert len(providers) >= 4
        ids = [p.id for p in providers]
        assert "credentials" in ids
        assert "ldap" in ids
        assert "saml" in ids
        assert "oidc" in ids

    def test_get_provider(self):
        p = ews.get_auth_provider("credentials")
        assert p is not None
        assert p.type == "credentials"
        assert p.enabled is True

    def test_get_provider_not_found(self):
        p = ews.get_auth_provider("nonexistent")
        assert p is None

    def test_auth_credentials_success(self):
        creds = ews.AuthCredentials(username="test@example.com", password="P@ss1234!")
        result = ews.authenticate("credentials", creds)
        assert result.result == ews.AuthResult.success.value
        assert result.user_id.startswith("u-")

    def test_auth_credentials_missing_password(self):
        creds = ews.AuthCredentials(username="test@example.com", password="")
        result = ews.authenticate("credentials", creds)
        assert result.result == ews.AuthResult.failed.value

    def test_auth_ldap(self):
        creds = ews.AuthCredentials(username="ldap-user", password="pass")
        result = ews.authenticate("ldap", creds)
        assert result.result in (ews.AuthResult.success.value, ews.AuthResult.failed.value)

    def test_auth_saml_no_response(self):
        creds = ews.AuthCredentials(provider_data={})
        result = ews.authenticate("saml", creds)
        assert result.result in (ews.AuthResult.failed.value, ews.AuthResult.provider_error.value)

    def test_auth_oidc_no_code(self):
        creds = ews.AuthCredentials(provider_data={})
        result = ews.authenticate("oidc", creds)
        assert result.result in (ews.AuthResult.failed.value, ews.AuthResult.provider_error.value)

    def test_auth_unknown_provider(self):
        creds = ews.AuthCredentials(username="x", password="y")
        result = ews.authenticate("nonexistent", creds)
        assert result.result == ews.AuthResult.failed.value

    def test_session_config(self):
        cfg = ews.get_session_config()
        assert "ttl_seconds" in cfg
        assert cfg["ttl_seconds"] > 0


class TestSession:
    def test_create_session(self):
        sess = ews.create_session("u-test", "ten-001")
        assert sess.token
        assert sess.user_id == "u-test"
        assert sess.tenant_id == "ten-001"
        assert sess.status == ews.SessionStatus.active.value
        assert sess.expires_at > sess.created_at

    def test_validate_session(self):
        sess = ews.create_session("u-test", "ten-001")
        assert ews.validate_session(sess.token) is True

    def test_validate_expired_session(self):
        sess = ews.create_session("u-test", "ten-001")
        sess.expires_at = time.time() - 1
        assert ews.validate_session(sess.token) is False

    def test_validate_invalid_token(self):
        assert ews.validate_session("bad-token") is False

    def test_revoke_session(self):
        sess = ews.create_session("u-test", "ten-001")
        assert ews.revoke_session(sess.token) is True
        assert ews.validate_session(sess.token) is False

    def test_revoke_nonexistent(self):
        assert ews.revoke_session("bad-token") is False

    def test_refresh_session(self):
        sess = ews.create_session("u-test", "ten-001")
        sess.expires_at = time.time() + 100
        refreshed = ews.refresh_session(sess.token)
        if refreshed:
            assert refreshed.expires_at > sess.created_at

    def test_max_sessions_per_user(self):
        for i in range(6):
            ews.create_session("u-same", f"ten-{i}")
        active = [s for s in ews._sessions.values()
                  if s.user_id == "u-same" and s.status == ews.SessionStatus.active.value]
        assert len(active) <= 5

    def test_get_session(self):
        sess = ews.create_session("u-test", "ten-001")
        found = ews.get_session(sess.token)
        assert found is not None
        assert found.user_id == "u-test"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RBAC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRBAC:
    def test_list_roles(self):
        roles = ews.list_roles()
        assert len(roles) >= 6
        ids = [r.id for r in roles]
        assert "super_admin" in ids
        assert "viewer" in ids
        assert "guest" in ids

    def test_role_hierarchy(self):
        roles = ews.list_roles()
        levels = {r.id: r.level for r in roles}
        assert levels["super_admin"] > levels["tenant_admin"]
        assert levels["tenant_admin"] > levels["manager"]
        assert levels["manager"] > levels["editor"]
        assert levels["editor"] > levels["viewer"]
        assert levels["viewer"] > levels["guest"]

    def test_get_role(self):
        r = ews.get_role("manager")
        assert r is not None
        assert r.level == 60

    def test_get_role_not_found(self):
        assert ews.get_role("nonexistent") is None

    def test_list_permissions(self):
        perms = ews.list_permissions()
        assert len(perms) >= 10
        ids = [p.id for p in perms]
        assert "users.create" in ids
        assert "audit.read" in ids

    def test_get_permission(self):
        p = ews.get_permission("users.create")
        assert p is not None
        assert p.resource == "users"
        assert p.action == "create"

    def test_super_admin_wildcard(self):
        assert ews.check_permission("super_admin", "users.create") is True
        assert ews.check_permission("super_admin", "anything.whatever") is True

    def test_viewer_limited(self):
        assert ews.check_permission("viewer", "users.read") is True
        assert ews.check_permission("viewer", "users.create") is False
        assert ews.check_permission("viewer", "users.delete") is False

    def test_guest_minimal(self):
        assert ews.check_permission("guest", "reports.view") is True
        assert ews.check_permission("guest", "users.read") is False

    def test_editor_permissions(self):
        assert ews.check_permission("editor", "workflow.create") is True
        assert ews.check_permission("editor", "workflow.approve") is False

    def test_manager_permissions(self):
        assert ews.check_permission("manager", "workflow.approve") is True
        assert ews.check_permission("manager", "workflow.reject") is True
        assert ews.check_permission("manager", "users.create") is False

    def test_tenant_admin_permissions(self):
        assert ews.check_permission("tenant_admin", "users.create") is True
        assert ews.check_permission("tenant_admin", "settings.manage") is True
        assert ews.check_permission("tenant_admin", "tenant.manage") is False

    def test_enforce_policy_allow(self):
        result = ews.enforce_policy("super_admin", "users", "create")
        assert result.verdict == ews.PolicyVerdict.allow.value

    def test_enforce_policy_deny(self):
        result = ews.enforce_policy("guest", "users", "create")
        assert result.verdict == ews.PolicyVerdict.deny.value

    def test_enforce_policy_unknown_role(self):
        result = ews.enforce_policy("nonexistent", "users", "create")
        assert result.verdict == ews.PolicyVerdict.deny.value

    def test_get_role_permissions(self):
        perms = ews.get_role_permissions("manager")
        assert "audit.read" in perms
        assert "reports.view" in perms

    def test_get_role_permissions_empty(self):
        perms = ews.get_role_permissions("nonexistent")
        assert perms == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAudit:
    def test_list_audit_actions(self):
        actions = ews.list_audit_actions()
        assert len(actions) >= 10
        ids = [a.id for a in actions]
        assert "auth.login" in ids
        assert "record.create" in ids

    def test_get_audit_config(self):
        cfg = ews.get_audit_config()
        assert cfg["enabled"] is True
        assert cfg["hash_chain"] is True

    def test_write_audit_entry(self):
        entry = ews.write_audit("record.create", "user-1", "orders", "o1", "ten-001",
                                before={}, after={"name": "Order 1"})
        assert entry.id.startswith("aud-")
        assert entry.action == "record.create"
        assert entry.actor == "user-1"
        assert entry.curr_hash
        assert entry.prev_hash == "0" * 64

    def test_write_multiple_entries_chain(self):
        e1 = ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        e2 = ews.write_audit("record.update", "u1", "orders", "o1", "t1")
        assert e2.prev_hash == e1.curr_hash
        assert e1.curr_hash != e2.curr_hash

    def test_verify_chain_valid(self):
        ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        ews.write_audit("record.update", "u1", "orders", "o1", "t1")
        ews.write_audit("record.delete", "u1", "orders", "o1", "t1")
        result = ews.verify_audit_chain()
        assert result.valid is True
        assert result.entries_checked == 3

    def test_verify_chain_tamper_detection(self):
        ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        e2 = ews.write_audit("record.update", "u1", "orders", "o1", "t1")
        e2.curr_hash = "tampered_hash"
        ews.write_audit("record.delete", "u1", "orders", "o1", "t1")
        result = ews.verify_audit_chain()
        assert result.valid is False

    def test_verify_empty_chain(self):
        result = ews.verify_audit_chain()
        assert result.valid is True
        assert result.entries_checked == 0

    def test_query_by_action(self):
        ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        ews.write_audit("record.update", "u1", "orders", "o1", "t1")
        entries = ews.query_audit(action="record.create")
        assert len(entries) == 1

    def test_query_by_actor(self):
        ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        ews.write_audit("record.create", "u2", "orders", "o2", "t1")
        entries = ews.query_audit(actor="u1")
        assert len(entries) == 1

    def test_query_by_tenant(self):
        ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        ews.write_audit("record.create", "u1", "orders", "o2", "t2")
        entries = ews.query_audit(tenant_id="t1")
        assert len(entries) == 1

    def test_query_limit(self):
        for i in range(10):
            ews.write_audit("record.create", "u1", "orders", f"o{i}", "t1")
        entries = ews.query_audit(limit=3)
        assert len(entries) == 3

    def test_audit_severity(self):
        e = ews.write_audit("user.delete", "admin", "users", "u1", "t1")
        assert e.severity == "warn"

    def test_clear_audit_entries(self):
        ews.write_audit("record.create", "u1", "orders", "o1", "t1")
        ews.clear_audit_entries()
        assert len(ews._audit_entries) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Reports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestReports:
    def test_list_report_types(self):
        types = ews.list_report_types()
        assert len(types) >= 6
        ids = [t.id for t in types]
        assert "tabular" in ids
        assert "bar_chart" in ids
        assert "line_chart" in ids
        assert "pie_chart" in ids
        assert "kpi_card" in ids
        assert "pivot_table" in ids

    def test_get_report_type(self):
        t = ews.get_report_type("tabular")
        assert t is not None
        assert "sort" in t.features

    def test_get_report_type_not_found(self):
        assert ews.get_report_type("nonexistent") is None

    def test_list_export_formats(self):
        fmts = ews.list_export_formats()
        assert len(fmts) >= 4
        ids = [f["id"] for f in fmts]
        assert "csv" in ids
        assert "xlsx" in ids
        assert "pdf" in ids
        assert "json" in ids

    def test_generate_tabular_report(self):
        data = [{"name": "A", "value": 10}, {"name": "B", "value": 20}]
        report = ews.generate_report("tabular", data, "Test Report")
        assert report.report_id.startswith("rpt-")
        assert report.report_type == "tabular"
        assert report.title == "Test Report"
        assert len(report.data) == 2
        assert "name" in report.columns
        assert "value" in report.columns

    def test_generate_chart_report(self):
        data = [{"label": "X", "value": 100}]
        report = ews.generate_report("bar_chart", data, "Chart")
        assert report.chart_config["type"] == "bar"

    def test_generate_report_unknown_type(self):
        with pytest.raises(ValueError, match="Unknown report type"):
            ews.generate_report("nonexistent", [])

    def test_generate_report_empty_data(self):
        report = ews.generate_report("tabular", [], "Empty")
        assert report.data == []
        assert report.columns == []

    def test_export_csv(self):
        data = [{"name": "A", "value": 10}, {"name": "B", "value": 20}]
        report = ews.generate_report("tabular", data)
        export = ews.export_report(report, "csv")
        assert export.format == "csv"
        assert export.row_count == 2
        assert b"name,value" in export.content
        assert export.mime_type == "text/csv"

    def test_export_json(self):
        data = [{"name": "A"}]
        report = ews.generate_report("tabular", data)
        export = ews.export_report(report, "json")
        parsed = json.loads(export.content)
        assert "data" in parsed
        assert len(parsed["data"]) == 1

    def test_export_xlsx(self):
        data = [{"name": "A", "value": 10}]
        report = ews.generate_report("tabular", data)
        export = ews.export_report(report, "xlsx")
        assert export.format == "xlsx"
        assert export.row_count == 1
        workbook = load_workbook(io.BytesIO(export.content))
        worksheet = workbook.active
        assert worksheet.title == "Report"
        assert [cell.value for cell in worksheet[1]] == ["name", "value"]
        assert [cell.value for cell in worksheet[2]] == ["A", "10"]
        assert export.content.startswith(b"PK")

    def test_export_pdf_stub(self):
        data = [{"name": "A"}]
        report = ews.generate_report("tabular", data, "PDF Report")
        export = ews.export_report(report, "pdf")
        assert b"PDF Report" in export.content

    def test_export_unknown_format(self):
        report = ews.generate_report("tabular", [{"a": 1}])
        with pytest.raises(ValueError, match="Unknown export format"):
            ews.export_report(report, "xml")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  i18n
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestI18n:
    def test_list_locales(self):
        locales = ews.list_locales()
        assert len(locales) >= 4
        ids = [loc.id for loc in locales]
        assert "en" in ids
        assert "zh-TW" in ids
        assert "zh-CN" in ids
        assert "ja" in ids

    def test_get_locale(self):
        loc = ews.get_locale("en")
        assert loc is not None
        assert loc.name == "English"
        assert loc.direction == "ltr"

    def test_get_locale_not_found(self):
        assert ews.get_locale("xx") is None

    def test_i18n_config(self):
        cfg = ews.get_i18n_config()
        assert cfg["default_locale"] == "en"
        assert cfg["framework"] == "next-intl"

    def test_list_namespaces(self):
        ns = ews.list_namespaces()
        assert "common" in ns
        assert "auth" in ns
        assert "errors" in ns

    def test_get_locale_bundle_en(self):
        bundle = ews.get_locale_bundle("en", "common")
        assert bundle.locale == "en"
        assert bundle.namespace == "common"
        assert "app.name" in bundle.messages
        assert bundle.messages["app.name"] == "OmniSight Enterprise"

    def test_get_locale_bundle_zh_tw(self):
        bundle = ews.get_locale_bundle("zh-TW", "common")
        assert "app.name" in bundle.messages
        assert "企業" in bundle.messages["app.name"]

    def test_get_locale_bundle_ja(self):
        bundle = ews.get_locale_bundle("ja", "common")
        assert "app.name" in bundle.messages
        assert "エンタープライズ" in bundle.messages["app.name"]

    def test_translate_en(self):
        result = ews.translate("app.name", "en")
        assert result == "OmniSight Enterprise"

    def test_translate_zh_tw(self):
        result = ews.translate("app.name", "zh-TW")
        assert "企業" in result

    def test_translate_with_interpolation(self):
        result = ews.translate("app.welcome", "en", {"appName": "TestApp"})
        assert "TestApp" in result

    def test_translate_fallback_to_default(self):
        result = ews.translate("app.name", "xx")
        assert result == "OmniSight Enterprise"

    def test_translate_unknown_key(self):
        result = ews.translate("nonexistent.key", "en")
        assert result == "nonexistent.key"

    def test_i18n_coverage(self):
        coverage = ews.check_i18n_coverage()
        assert len(coverage) >= 4
        en_cov = next(c for c in coverage if c.locale == "en")
        assert en_cov.coverage_pct == 100.0
        assert en_cov.missing_keys == []

    def test_i18n_all_locales_have_common_bundle(self):
        for loc in ews.list_locales():
            bundle = ews.get_locale_bundle(loc.id, "common")
            assert "app.name" in bundle.messages

    def test_i18n_auth_bundle(self):
        bundle = ews.get_locale_bundle("en", "auth")
        assert "auth.login" in bundle.messages
        assert "auth.logout" in bundle.messages


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Multi-tenant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMultiTenant:
    def test_tenant_config(self):
        cfg = ews.get_multi_tenant_config()
        assert cfg["enabled"] is True
        assert cfg["isolation"] == "row_level_security"

    def test_list_strategies(self):
        strategies = ews.list_tenant_strategies()
        assert len(strategies) >= 3
        ids = [s.id for s in strategies]
        assert "rls" in ids
        assert "schema" in ids
        assert "database" in ids

    def test_create_tenant(self):
        t = ews.create_tenant("Acme Corp", "acme", "starter", 10)
        assert t.id.startswith("ten-")
        assert t.name == "Acme Corp"
        assert t.slug == "acme"
        assert t.plan == "starter"
        assert t.max_users == 10
        assert t.active is True

    def test_create_tenant_duplicate_slug(self):
        ews.create_tenant("A", "same-slug", "free")
        with pytest.raises(ValueError, match="already exists"):
            ews.create_tenant("B", "same-slug", "free")

    def test_list_tenants(self):
        ews.create_tenant("A", "a-slug", "free")
        ews.create_tenant("B", "b-slug", "starter")
        tenants = ews.list_tenants()
        assert len(tenants) == 2

    def test_get_tenant(self):
        t = ews.create_tenant("Test", "test-slug", "professional")
        found = ews.get_tenant(t.id)
        assert found is not None
        assert found.name == "Test"

    def test_get_tenant_not_found(self):
        assert ews.get_tenant("nonexistent") is None

    def test_get_tenant_by_slug(self):
        ews.create_tenant("By Slug", "by-slug-test", "free")
        found = ews.get_tenant_by_slug("by-slug-test")
        assert found is not None
        assert found.name == "By Slug"

    def test_update_tenant(self):
        t = ews.create_tenant("Update Me", "update-slug", "free")
        updated = ews.update_tenant(t.id, {"plan": "enterprise", "max_users": 100})
        assert updated is not None
        assert updated.plan == "enterprise"
        assert updated.max_users == 100

    def test_update_tenant_not_found(self):
        assert ews.update_tenant("nonexistent", {"name": "X"}) is None

    def test_delete_tenant(self):
        t = ews.create_tenant("Delete Me", "delete-slug", "free")
        assert ews.delete_tenant(t.id) is True
        assert ews.get_tenant(t.id) is None

    def test_delete_tenant_not_found(self):
        assert ews.delete_tenant("nonexistent") is False

    def test_apply_rls_no_where(self):
        rls = ews.apply_rls("SELECT * FROM orders", "ten-001")
        assert rls.applied is True
        assert "WHERE tenant_id = :tenant_id" in rls.filtered_query
        assert rls.params == {"tenant_id": "ten-001"}
        # Tenant id MUST NOT be concatenated into the SQL string (FX.1.8).
        assert "ten-001" not in rls.filtered_query

    def test_apply_rls_with_where(self):
        rls = ews.apply_rls("SELECT * FROM orders WHERE status = 'active'", "ten-002")
        assert "AND tenant_id = :tenant_id" in rls.filtered_query
        assert rls.params == {"tenant_id": "ten-002"}
        assert "ten-002" not in rls.filtered_query

    def test_apply_rls_rejects_sql_injection_in_tenant_id(self):
        # FX.1.8: tenant_id is parameterized — injection payload is preserved
        # verbatim in params, never spliced into the SQL string.
        payload = "x'; DROP TABLE orders; --"
        rls = ews.apply_rls("SELECT * FROM orders", payload)
        assert "DROP TABLE" not in rls.filtered_query
        assert rls.params == {"tenant_id": payload}

    def test_clear_tenants(self):
        ews.create_tenant("A", "a-clr", "free")
        ews.clear_tenants()
        assert len(ews.list_tenants()) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Import/Export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestImportExport:
    def test_list_import_formats(self):
        fmts = ews.list_import_formats()
        assert len(fmts) >= 3
        ids = [f.id for f in fmts]
        assert "csv" in ids
        assert "xlsx" in ids
        assert "json" in ids

    def test_get_import_format(self):
        f = ews.get_import_format("csv")
        assert f is not None
        assert f.mime == "text/csv"

    def test_import_steps(self):
        steps = ews.list_import_steps()
        assert len(steps) >= 6
        step_names = [s.step for s in steps]
        assert "upload" in step_names
        assert "validate" in step_names
        assert "commit" in step_names

    def test_export_steps(self):
        steps = ews.list_export_steps()
        assert len(steps) >= 4

    def test_preview_csv(self):
        data = "name,age,email\nAlice,30,a@x.com\nBob,25,b@x.com\nCharlie,35,c@x.com"
        preview = ews.preview_import(data, "csv", 2)
        assert preview.format == "csv"
        assert preview.total_rows == 3
        assert len(preview.sample_rows) == 2
        assert "name" in preview.columns
        assert preview.detected_types["age"] == "number"

    def test_preview_json(self):
        data = json.dumps([
            {"name": "Alice", "score": 95},
            {"name": "Bob", "score": 87},
        ])
        preview = ews.preview_import(data, "json", 5)
        assert preview.format == "json"
        assert preview.total_rows == 2
        assert preview.detected_types["score"] == "number"

    def test_preview_xlsx_stub(self):
        data = "name\tage\nAlice\t30\nBob\t25"
        preview = ews.preview_import(data, "xlsx", 5)
        assert preview.format == "xlsx"
        assert preview.total_rows == 2

    def test_preview_unknown_format(self):
        with pytest.raises(ValueError, match="Unknown import format"):
            ews.preview_import("data", "xml")

    def test_execute_import_csv(self):
        data = "name,age\nAlice,30\nBob,25"
        result = ews.execute_import(data, "csv", "ten-001")
        assert result.import_id.startswith("imp-")
        assert result.total_rows == 2
        assert result.inserted >= 2
        assert result.tenant_id == "ten-001"

    def test_execute_import_json(self):
        data = json.dumps([{"name": "A"}, {"name": "B"}, {"name": "C"}])
        result = ews.execute_import(data, "json", "ten-002")
        assert result.total_rows == 3
        assert result.inserted >= 3

    def test_execute_import_with_mapping(self):
        data = "col_a,col_b\nAlice,30\nBob,25"
        result = ews.execute_import(data, "csv", "ten-001",
                                    column_mapping={"col_a": "name", "col_b": "age"})
        assert result.inserted >= 2

    def test_execute_export_csv(self):
        data = [{"name": "A", "value": 1}, {"name": "B", "value": 2}]
        result = ews.execute_export(data, "csv", "ten-001")
        assert result.export_id.startswith("exp-")
        assert result.row_count == 2
        assert result.mime_type == "text/csv"
        assert b"name,value" in result.content

    def test_execute_export_json(self):
        data = [{"x": 1}]
        result = ews.execute_export(data, "json")
        parsed = json.loads(result.content)
        assert len(parsed) == 1

    def test_execute_export_xlsx_stub(self):
        data = [{"a": 1, "b": 2}]
        result = ews.execute_export(data, "xlsx")
        assert result.format == "xlsx"

    def test_execute_export_unknown_format(self):
        with pytest.raises(ValueError, match="Unknown export format"):
            ews.execute_export([{"a": 1}], "xml")

    def test_roundtrip_csv(self):
        original = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
        export_result = ews.execute_export(original, "csv")
        import_result = ews.execute_import(export_result.content.decode("utf-8"), "csv")
        assert import_result.total_rows == 2

    def test_roundtrip_json(self):
        original = [{"name": "Alice", "score": 95}]
        export_result = ews.execute_export(original, "json")
        import_result = ews.execute_import(export_result.content.decode("utf-8"), "json")
        assert import_result.total_rows == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Workflow engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWorkflow:
    def test_list_states(self):
        states = ews.list_workflow_states()
        assert len(states) >= 8
        ids = [s.id for s in states]
        assert "draft" in ids
        assert "submitted" in ids
        assert "approved" in ids
        assert "rejected" in ids
        assert "completed" in ids

    def test_initial_state(self):
        states = ews.list_workflow_states()
        initial = [s for s in states if s.initial]
        assert len(initial) == 1
        assert initial[0].id == "draft"

    def test_terminal_states(self):
        states = ews.list_workflow_states()
        terminal = [s for s in states if s.terminal]
        assert len(terminal) >= 3
        terminal_ids = {s.id for s in terminal}
        assert "rejected" in terminal_ids
        assert "completed" in terminal_ids
        assert "cancelled" in terminal_ids

    def test_get_workflow_state(self):
        s = ews.get_workflow_state("draft")
        assert s is not None
        assert s.initial is True
        assert "submitted" in s.transitions

    def test_approval_chain_config(self):
        cfg = ews.get_approval_chain_config()
        assert cfg.min_approvers >= 1
        assert cfg.max_approvers >= cfg.min_approvers
        assert cfg.escalation_timeout_hours > 0

    def test_create_workflow_instance(self):
        inst = ews.create_workflow_instance(
            "purchase_order", {"amount": 5000}, "user-1", "ten-001", ["mgr-1"]
        )
        assert inst.id.startswith("wf-")
        assert inst.state == "draft"
        assert inst.submitter == "user-1"
        assert inst.tenant_id == "ten-001"
        assert len(inst.history) == 1

    def test_get_workflow_instance(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        found = ews.get_workflow_instance(inst.id)
        assert found is not None
        assert found.id == inst.id

    def test_list_workflow_instances(self):
        ews.create_workflow_instance("a", {}, "u1", "t1")
        ews.create_workflow_instance("b", {}, "u2", "t2")
        all_inst = ews.list_workflow_instances()
        assert len(all_inst) == 2
        t1_inst = ews.list_workflow_instances(tenant_id="t1")
        assert len(t1_inst) == 1

    def test_transition_draft_to_submitted(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        updated = ews.transition_workflow(inst.id, "submitted", "user-1")
        assert updated.state == "submitted"
        assert len(updated.history) == 2

    def test_full_lifecycle(self):
        inst = ews.create_workflow_instance("po", {"amt": 1000}, "user-1", approvers=["mgr-1"])
        inst = ews.transition_workflow(inst.id, "submitted", "user-1")
        inst = ews.transition_workflow(inst.id, "under_review", "mgr-1")
        inst = ews.transition_workflow(inst.id, "approved", "mgr-1")
        inst = ews.transition_workflow(inst.id, "completed", "user-1")
        assert inst.state == "completed"
        assert len(inst.history) == 5

    def test_invalid_transition(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        with pytest.raises(ValueError, match="Invalid transition"):
            ews.transition_workflow(inst.id, "approved", "user-1")

    def test_transition_nonexistent_instance(self):
        with pytest.raises(ValueError, match="not found"):
            ews.transition_workflow("wf-nonexistent", "submitted", "user-1")

    def test_approve_workflow(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        approved = ews.approve_workflow(inst.id, "mgr-1", "Looks good")
        assert approved.state == "approved"

    def test_reject_workflow(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        rejected = ews.reject_workflow(inst.id, "mgr-1", "Insufficient detail")
        assert rejected.state == "rejected"

    def test_approve_from_submitted(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        approved = ews.approve_workflow(inst.id, "mgr-1")
        assert approved.state == "approved"

    def test_reject_from_submitted(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        rejected = ews.reject_workflow(inst.id, "mgr-1")
        assert rejected.state == "rejected"

    def test_complete_workflow(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        ews.approve_workflow(inst.id, "mgr-1")
        completed = ews.complete_workflow(inst.id, "user-1")
        assert completed.state == "completed"

    def test_complete_not_approved(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        with pytest.raises(ValueError, match="Cannot complete"):
            ews.complete_workflow(inst.id, "user-1")

    def test_cancel_workflow(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        cancelled = ews.cancel_workflow(inst.id, "user-1", "Changed my mind")
        assert cancelled.state == "cancelled"

    def test_cancel_terminal_state(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        ews.reject_workflow(inst.id, "mgr-1")
        with pytest.raises(ValueError, match="terminal state"):
            ews.cancel_workflow(inst.id, "user-1")

    def test_needs_revision_cycle(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        ews.transition_workflow(inst.id, "under_review", "mgr-1")
        ews.transition_workflow(inst.id, "needs_revision", "mgr-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        ews.transition_workflow(inst.id, "under_review", "mgr-1")
        approved = ews.transition_workflow(inst.id, "approved", "mgr-1")
        assert approved.state == "approved"
        assert len(approved.history) >= 7

    def test_history_tracking(self):
        inst = ews.create_workflow_instance("test", {}, "user-1")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        assert len(inst.history) == 2
        last = inst.history[-1]
        assert last["from_state"] == "draft"
        assert last["to_state"] == "submitted"
        assert last["actor"] == "user-1"

    def test_clear_workflow_instances(self):
        ews.create_workflow_instance("test", {}, "user-1")
        ews.clear_workflow_instances()
        assert len(ews.list_workflow_instances()) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test recipes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRecipes:
    def test_list_test_recipes(self):
        recipes = ews.list_test_recipes()
        assert len(recipes) >= 10
        ids = [r.id for r in recipes]
        assert "auth_flow" in ids
        assert "rbac_enforcement" in ids
        assert "audit_chain" in ids
        assert "tenant_isolation" in ids
        assert "workflow_lifecycle" in ids
        assert "i18n_coverage" in ids
        assert "full_integration" in ids

    def test_get_test_recipe(self):
        r = ews.get_test_recipe("auth_flow")
        assert r is not None
        assert r.domain == "auth"
        assert len(r.steps) >= 3

    def test_get_test_recipe_not_found(self):
        assert ews.get_test_recipe("nonexistent") is None

    def test_run_auth_flow_recipe(self):
        result = ews.run_test_recipe("auth_flow")
        assert result.status == ews.TestRecipeStatus.passed.value
        assert result.steps_passed == result.steps_total

    def test_run_rbac_enforcement_recipe(self):
        result = ews.run_test_recipe("rbac_enforcement")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_audit_chain_recipe(self):
        result = ews.run_test_recipe("audit_chain")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_tenant_isolation_recipe(self):
        result = ews.run_test_recipe("tenant_isolation")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_workflow_lifecycle_recipe(self):
        result = ews.run_test_recipe("workflow_lifecycle")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_i18n_coverage_recipe(self):
        result = ews.run_test_recipe("i18n_coverage")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_import_export_roundtrip_recipe(self):
        result = ews.run_test_recipe("import_export_roundtrip")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_report_generation_recipe(self):
        result = ews.run_test_recipe("report_generation")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_full_integration_recipe(self):
        result = ews.run_test_recipe("full_integration")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_sso_integration_recipe(self):
        result = ews.run_test_recipe("sso_integration")
        assert result.status == ews.TestRecipeStatus.passed.value

    def test_run_nonexistent_recipe(self):
        result = ews.run_test_recipe("nonexistent")
        assert result.status == ews.TestRecipeStatus.error.value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifacts + Gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestArtifacts:
    def test_list_artifacts(self):
        arts = ews.list_artifacts()
        assert len(arts) >= 8
        ids = [a.id for a in arts]
        assert "auth_module" in ids
        assert "rbac_module" in ids
        assert "audit_module" in ids
        assert "workflow_module" in ids

    def test_get_artifact(self):
        a = ews.get_artifact("auth_module")
        assert a is not None
        assert len(a.files) > 0

    def test_get_artifact_not_found(self):
        assert ews.get_artifact("nonexistent") is None


class TestGate:
    def test_gate_all_present(self):
        result = ews.validate_gate("auth", ["auth_module"])
        assert result.passed is True

    def test_gate_missing_artifact(self):
        result = ews.validate_gate("auth", [])
        assert result.passed is False

    def test_gate_full_stack(self):
        all_ids = [a.id for a in ews.list_artifacts()]
        result = ews.validate_gate("full", all_ids)
        assert result.passed is True

    def test_gate_unknown_domain(self):
        result = ews.validate_gate("unknown_domain", [])
        assert result.passed is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum coverage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEnums:
    def test_web_stack_domain(self):
        assert len(ews.WebStackDomain) >= 9

    def test_auth_provider_type(self):
        assert len(ews.AuthProviderType) == 4

    def test_auth_result(self):
        assert len(ews.AuthResult) >= 5

    def test_session_status(self):
        assert len(ews.SessionStatus) == 3

    def test_role_level(self):
        assert len(ews.RoleLevel) == 6

    def test_policy_verdict(self):
        assert len(ews.PolicyVerdict) == 2

    def test_audit_severity(self):
        assert len(ews.AuditSeverity) == 3

    def test_report_type_enum(self):
        assert len(ews.ReportType) == 6

    def test_export_format(self):
        assert len(ews.ExportFormat) == 4

    def test_import_format(self):
        assert len(ews.ImportFormat) == 3

    def test_tenant_plan(self):
        assert len(ews.TenantPlan) == 4

    def test_tenant_strategy(self):
        assert len(ews.TenantStrategy) == 3

    def test_workflow_state_enum(self):
        assert len(ews.WorkflowState) == 8

    def test_text_direction(self):
        assert len(ews.TextDirection) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration / Cross-domain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIntegration:
    def test_auth_then_rbac(self):
        creds = ews.AuthCredentials(username="admin@test.com", password="Admin1234!")
        auth_result = ews.authenticate("credentials", creds)
        assert auth_result.result == "success"
        assert ews.check_permission("super_admin", "users.create") is True

    def test_tenant_isolation_with_audit(self):
        t1 = ews.create_tenant("T1", "t1-iso", "starter")
        t2 = ews.create_tenant("T2", "t2-iso", "starter")
        ews.write_audit("record.create", "u1", "orders", "o1", t1.id)
        ews.write_audit("record.create", "u2", "orders", "o2", t2.id)
        t1_entries = ews.query_audit(tenant_id=t1.id)
        t2_entries = ews.query_audit(tenant_id=t2.id)
        assert len(t1_entries) == 1
        assert len(t2_entries) == 1
        assert t1_entries[0].tenant_id == t1.id

    def test_workflow_with_audit_trail(self):
        inst = ews.create_workflow_instance("po", {"amount": 5000}, "user-1", "ten-001")
        ews.write_audit("workflow.submit", "user-1", "workflow", inst.id, "ten-001")
        ews.transition_workflow(inst.id, "submitted", "user-1")
        ews.write_audit("workflow.approve", "mgr-1", "workflow", inst.id, "ten-001")
        ews.approve_workflow(inst.id, "mgr-1")
        entries = ews.query_audit(tenant_id="ten-001")
        assert len(entries) == 2
        chain = ews.verify_audit_chain("ten-001")
        assert chain.valid is True

    def test_export_then_import_roundtrip(self):
        data = [
            {"product": "Widget A", "qty": "100", "price": "9.99"},
            {"product": "Widget B", "qty": "50", "price": "19.99"},
        ]
        export = ews.execute_export(data, "csv", "ten-001")
        assert export.row_count == 2

        imp = ews.execute_import(export.content.decode("utf-8"), "csv", "ten-001")
        assert imp.total_rows == 2
        assert imp.inserted == 2

    def test_i18n_with_report(self):
        title_en = ews.translate("reports.title", "en")
        title_zh = ews.translate("reports.title", "zh-TW")
        report = ews.generate_report("tabular", [{"a": 1}], title_en)
        assert report.title == "Reports"
        report_zh = ews.generate_report("tabular", [{"a": 1}], title_zh)
        assert report_zh.title == "報表"

    def test_full_stack_flow(self):
        tenant = ews.create_tenant("Full Stack Test", "fs-test", "professional", 50)
        creds = ews.AuthCredentials(username="admin@fs-test.com", password="Admin1234!")
        auth = ews.authenticate("credentials", creds)
        assert auth.result == "success"

        sess = ews.create_session(auth.user_id, tenant.id)
        assert ews.validate_session(sess.token)

        assert ews.check_permission("tenant_admin", "workflow.create")

        ews.write_audit("auth.login", auth.user_id, "session", sess.token, tenant.id)

        inst = ews.create_workflow_instance(
            "purchase_order", {"amount": 5000, "vendor": "Acme"},
            auth.user_id, tenant.id, ["mgr-1"],
        )
        ews.transition_workflow(inst.id, "submitted", auth.user_id)
        ews.approve_workflow(inst.id, "mgr-1")
        ews.complete_workflow(inst.id, auth.user_id)
        assert inst.state == "completed"

        ews.write_audit("workflow.submit", auth.user_id, "workflow", inst.id, tenant.id)

        report = ews.generate_report("tabular", [
            {"order_id": inst.id, "amount": 5000, "status": "completed"},
        ], "Purchase Orders")
        export = ews.export_report(report, "csv")
        assert export.row_count == 1

        entries = ews.query_audit(tenant_id=tenant.id)
        assert len(entries) >= 2

        chain = ews.verify_audit_chain()
        assert chain.valid is True

        title = ews.translate("workflow.title", "zh-TW")
        assert "流程" in title

        rls = ews.apply_rls("SELECT * FROM purchase_orders", tenant.id)
        assert rls.params.get("tenant_id") == tenant.id
        assert ":tenant_id" in rls.filtered_query
