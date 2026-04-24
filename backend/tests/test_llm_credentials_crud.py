"""Phase 5b-3 (#llm-credentials) — llm_credentials CRUD tests.

Covers the service layer in :mod:`backend.llm_credentials` plus the
probe-dispatch helper in :mod:`backend.routers.llm_credentials`.
The router itself is a thin re-raise-into-HTTPException wrapper;
the service-level pool tests give us the full CRUD + RLS contract
without booting the FastAPI app.

All PG tests are gated on ``OMNI_TEST_PG_URL`` via the shared
``pg_test_pool`` fixture; skipped locally when the test PG container
is not up.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


DEFAULT_TENANT = "t-default"
OTHER_TENANT = "t-lc-other"


@pytest.fixture()
async def _lc_db(pg_test_pool):
    """Fresh ``llm_credentials`` slate + two seeded tenants.

    The test ``llm_credentials`` rows have an FK to ``tenants`` (ON
    DELETE CASCADE) so both tenants must exist before the service
    inserts. TRUNCATE clears prior test pollution; CASCADE sweeps
    any audit_log rows that pile up.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, $2, $3), ($4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            DEFAULT_TENANT, "Default", "starter",
            OTHER_TENANT, "Other", "starter",
        )
        await conn.execute(
            "TRUNCATE llm_credentials, audit_log RESTART IDENTITY CASCADE"
        )
    from backend.db_context import set_tenant_id
    set_tenant_id(DEFAULT_TENANT)
    import backend.llm_credentials as lc
    try:
        yield lc
    finally:
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE llm_credentials, audit_log RESTART IDENTITY CASCADE"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Create / list / get
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_create_returns_public_dict_without_plaintext_key(_lc_db):
    lc = _lc_db
    out = await lc.create_credential(
        provider="anthropic",
        label="main",
        value="sk-ant-supersecret-abc4",
    )
    assert out["provider"] == "anthropic"
    assert out["label"] == "main"
    # Must not echo the key or its ciphertext — only the fingerprint.
    assert "value" not in out
    assert "encrypted_value" not in out
    assert out["value_fingerprint"].endswith("abc4")
    assert out["value_fingerprint"].startswith("…")
    assert out["id"].startswith("lc-")
    assert out["version"] == 0
    assert out["auth_type"] == "pat"
    assert out["enabled"] is True


async def test_list_uses_fingerprint_never_plaintext(_lc_db):
    lc = _lc_db
    await lc.create_credential(
        provider="openai", label="prod",
        value="sk-openai-zzzzzzzz_1234",
    )
    items = await lc.list_credentials()
    assert len(items) == 1
    assert items[0]["value_fingerprint"] == "…1234"
    assert "value" not in items[0]
    assert "encrypted_value" not in items[0]


async def test_get_credential_and_missing(_lc_db):
    lc = _lc_db
    created = await lc.create_credential(provider="openai", label="one")
    fetched = await lc.get_credential(created["id"])
    assert fetched is not None
    assert fetched["label"] == "one"
    assert await lc.get_credential("lc-does-not-exist") is None


async def test_list_filter_by_provider_and_enabled(_lc_db):
    lc = _lc_db
    await lc.create_credential(provider="anthropic", label="a1")
    await lc.create_credential(provider="openai", label="o1")
    await lc.create_credential(
        provider="openai", label="o2-disabled", enabled=False,
    )
    all_ant = await lc.list_credentials(provider="anthropic")
    assert len(all_ant) == 1
    all_openai = await lc.list_credentials(provider="openai")
    assert len(all_openai) == 2
    only_enabled_openai = await lc.list_credentials(
        provider="openai", enabled_only=True,
    )
    assert len(only_enabled_openai) == 1
    assert only_enabled_openai[0]["label"] == "o1"


async def test_create_ollama_with_metadata_base_url(_lc_db):
    """Keyless providers (ollama) are legal without a value — the
    resolver threads metadata.base_url through the adapter instead."""
    lc = _lc_db
    out = await lc.create_credential(
        provider="ollama", label="local",
        value="",  # keyless
        metadata={"base_url": "http://ai_engine:11434"},
    )
    assert out["value_fingerprint"] == ""  # no key → empty fingerprint
    assert out["metadata"] == {"base_url": "http://ai_engine:11434"}


async def test_create_unknown_provider_raises(_lc_db):
    lc = _lc_db
    with pytest.raises(ValueError, match="Unknown provider"):
        await lc.create_credential(provider="nonsense", value="x")


async def test_create_unknown_auth_type_raises(_lc_db):
    lc = _lc_db
    with pytest.raises(ValueError, match="Unknown auth_type"):
        await lc.create_credential(
            provider="anthropic", auth_type="cert", value="x",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default-per-(tenant, provider) invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_second_default_create_raises_conflict(_lc_db):
    lc = _lc_db
    await lc.create_credential(
        provider="anthropic", label="p1", is_default=True,
    )
    with pytest.raises(lc.LLMCredentialConflict):
        await lc.create_credential(
            provider="anthropic", label="p2", is_default=True,
        )


async def test_patch_to_default_unsets_current_default(_lc_db):
    lc = _lc_db
    p1 = await lc.create_credential(
        provider="openai", label="p1", is_default=True,
    )
    p2 = await lc.create_credential(
        provider="openai", label="p2", is_default=False,
    )
    out = await lc.update_credential(p2["id"], updates={"is_default": True})
    assert out["is_default"] is True

    fresh = {row["id"]: row for row in await lc.list_credentials()}
    assert fresh[p2["id"]]["is_default"] is True
    assert fresh[p1["id"]]["is_default"] is False


async def test_patch_rotates_key_fingerprint_changes(_lc_db):
    lc = _lc_db
    a = await lc.create_credential(
        provider="anthropic", label="rot",
        value="sk-ant-old-value-1111",
    )
    assert a["value_fingerprint"].endswith("1111")
    b = await lc.update_credential(
        a["id"], updates={"value": "sk-ant-NEW-2222"},
    )
    assert b["value_fingerprint"].endswith("2222")
    assert b["version"] > a["version"]


async def test_patch_clears_key_when_empty_string(_lc_db):
    lc = _lc_db
    a = await lc.create_credential(
        provider="groq", label="will-clear", value="gsk_12345678",
    )
    assert a["value_fingerprint"] == "…5678"
    b = await lc.update_credential(a["id"], updates={"value": ""})
    assert b["value_fingerprint"] == ""


async def test_patch_metadata_replaces_dict(_lc_db):
    lc = _lc_db
    a = await lc.create_credential(
        provider="openai", label="meta",
        metadata={"org_id": "org-abc"},
    )
    b = await lc.update_credential(
        a["id"],
        updates={"metadata": {"org_id": "org-xyz", "notes": "n"}},
    )
    assert b["metadata"] == {"org_id": "org-xyz", "notes": "n"}


async def test_patch_unknown_field_raises(_lc_db):
    lc = _lc_db
    a = await lc.create_credential(provider="openai", label="x")
    with pytest.raises(ValueError, match="Unknown update fields"):
        await lc.update_credential(a["id"], updates={"nope": 1})


async def test_patch_empty_updates_returns_existing(_lc_db):
    lc = _lc_db
    a = await lc.create_credential(provider="openai", label="x")
    out = await lc.update_credential(a["id"], updates={})
    assert out["id"] == a["id"]
    assert out["label"] == "x"


async def test_patch_not_found_raises(_lc_db):
    lc = _lc_db
    with pytest.raises(lc.LLMCredentialNotFound):
        await lc.update_credential(
            "lc-nonexistent", updates={"label": "whatever"},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Delete — refuse-without-replacement AND auto-elect paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_delete_nondefault_just_deletes(_lc_db):
    lc = _lc_db
    a = await lc.create_credential(
        provider="openai", label="one", is_default=True,
    )
    b = await lc.create_credential(provider="openai", label="two")
    out = await lc.delete_credential(b["id"])
    assert out["promoted_id"] is None
    remaining = await lc.list_credentials()
    assert len(remaining) == 1
    assert remaining[0]["id"] == a["id"]
    assert remaining[0]["is_default"] is True


async def test_delete_default_auto_elects_new_default(_lc_db):
    lc = _lc_db
    d = await lc.create_credential(
        provider="anthropic", label="default-one", is_default=True,
    )
    runner_up = await lc.create_credential(
        provider="anthropic", label="runner-up",
    )
    out = await lc.delete_credential(d["id"])
    assert out["promoted_id"] == runner_up["id"]

    fresh = await lc.list_credentials(provider="anthropic")
    assert len(fresh) == 1
    assert fresh[0]["id"] == runner_up["id"]
    assert fresh[0]["is_default"] is True


async def test_delete_default_refuse_without_replacement(_lc_db):
    lc = _lc_db
    d = await lc.create_credential(
        provider="anthropic", label="default", is_default=True,
    )
    await lc.create_credential(provider="anthropic", label="other")
    with pytest.raises(lc.LLMCredentialConflict):
        await lc.delete_credential(
            d["id"], auto_elect_new_default=False,
        )
    assert await lc.get_credential(d["id"]) is not None


async def test_delete_sole_credential_even_if_default_succeeds(_lc_db):
    """refuse path only fires when a replacement exists. A solo
    default on the provider is allowed to leave the tenant
    defaultless (otherwise we could never delete the last row)."""
    lc = _lc_db
    d = await lc.create_credential(
        provider="anthropic", label="only-one", is_default=True,
    )
    out = await lc.delete_credential(
        d["id"], auto_elect_new_default=False,
    )
    assert out["promoted_id"] is None
    assert (await lc.list_credentials(provider="anthropic")) == []


async def test_delete_missing_raises_not_found(_lc_db):
    lc = _lc_db
    with pytest.raises(lc.LLMCredentialNotFound):
        await lc.delete_credential("lc-nonexistent")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tenant isolation — A cannot touch B's rows
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_tenant_isolation(_lc_db):
    lc = _lc_db
    from backend.db_context import set_tenant_id

    set_tenant_id(DEFAULT_TENANT)
    a_row = await lc.create_credential(
        provider="anthropic", label="A-owned",
        value="sk-ant-tenantA-xyz",
    )
    assert len(await lc.list_credentials()) == 1

    set_tenant_id(OTHER_TENANT)
    assert await lc.list_credentials() == []
    assert await lc.get_credential(a_row["id"]) is None

    with pytest.raises(lc.LLMCredentialNotFound):
        await lc.update_credential(
            a_row["id"], updates={"label": "hijack"},
        )
    with pytest.raises(lc.LLMCredentialNotFound):
        await lc.delete_credential(a_row["id"])

    set_tenant_id(DEFAULT_TENANT)
    still_a = await lc.get_credential(a_row["id"])
    assert still_a is not None
    assert still_a["label"] == "A-owned"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit log — each mutation writes a row
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_mutations_write_audit_rows(_lc_db, pg_test_pool):
    lc = _lc_db
    created = await lc.create_credential(
        provider="anthropic", label="audit-test",
    )
    await lc.update_credential(
        created["id"], updates={"label": "audit-rot"},
    )
    await lc.delete_credential(created["id"])

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action FROM audit_log "
            "WHERE tenant_id = $1 AND entity_kind = 'llm_credential' "
            "ORDER BY id ASC",
            DEFAULT_TENANT,
        )
    actions = [r["action"] for r in rows]
    assert "llm_credential.create" in actions
    assert "llm_credential.update" in actions
    assert "llm_credential.delete" in actions


async def test_audit_log_never_contains_plaintext_key(
    _lc_db, pg_test_pool,
):
    """Acceptance-critical: rotation drill must not echo the old /
    new plaintext key into audit_log.before_json / after_json.
    """
    lc = _lc_db
    created = await lc.create_credential(
        provider="anthropic", label="rot",
        value="sk-ant-PLAINTEXT-SHOULDNOTLEAK",
    )
    await lc.update_credential(
        created["id"], updates={"value": "sk-ant-NEW-PLAIN-SECRET"},
    )

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT before_json, after_json FROM audit_log "
            "WHERE tenant_id = $1 AND entity_kind = 'llm_credential'",
            DEFAULT_TENANT,
        )
    blob = " ".join(
        (r["before_json"] or "") + " " + (r["after_json"] or "")
        for r in rows
    )
    assert "PLAINTEXT-SHOULDNOTLEAK" not in blob
    assert "NEW-PLAIN-SECRET" not in blob


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Probe dispatcher (unit — no network)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_probe_unknown_provider_returns_error():
    from backend.routers.llm_credentials import _probe_llm_credential
    out = await _probe_llm_credential("nonsense", "k", {})
    assert out["status"] == "error"
    assert "Unknown provider" in out["message"]


async def test_probe_requires_value_for_keyed_providers():
    from backend.routers.llm_credentials import _probe_llm_credential
    out = await _probe_llm_credential("anthropic", "", {})
    assert out["status"] == "error"
    assert "API key is required" in out["message"]


async def test_probe_ollama_requires_base_url():
    from backend.routers.llm_credentials import _probe_llm_credential
    out = await _probe_llm_credential("ollama", "", {})
    assert out["status"] == "error"
    assert "base_url" in out["message"]


async def test_probe_specs_cover_all_keyed_providers():
    """Drift guard: the probe-specs table must list every keyed
    provider in the service layer's ``_VALID_PROVIDERS`` set minus
    ``ollama`` (which is special-cased in the dispatcher). Adding a
    new provider to the schema but forgetting to wire the probe
    breaks the /test endpoint silently — this test catches it."""
    from backend.llm_credentials import _VALID_PROVIDERS
    from backend.routers.llm_credentials import _PROBE_SPECS

    keyed = _VALID_PROVIDERS - {"ollama"}
    assert keyed == set(_PROBE_SPECS.keys()), (
        f"probe-spec drift: keyed providers {sorted(keyed)} != "
        f"probe specs {sorted(_PROBE_SPECS.keys())}"
    )


async def test_probe_dispatch_uses_bearer_header_for_bearer_providers(
    monkeypatch,
):
    """Unit-level check that the probe dispatcher threads the key
    into the correct header. Uses monkeypatch on ``_curl_json`` to
    observe the ``args`` list without actually hitting the network.
    """
    from backend.routers import llm_credentials as mod

    captured: dict = {}

    async def fake_curl(args):
        captured["args"] = args
        return (200, {"data": [{"id": "x"}]}, "")

    monkeypatch.setattr(mod, "_curl_json", fake_curl)
    out = await mod._probe_llm_credential("openai", "sk-openai-xxxx", {})
    assert out["status"] == "ok"
    assert out["model_count"] == 1
    args = captured["args"]
    assert any("Authorization: Bearer sk-openai-xxxx" == a for a in args)


async def test_probe_dispatch_uses_custom_header_for_anthropic(monkeypatch):
    from backend.routers import llm_credentials as mod

    captured: dict = {}

    async def fake_curl(args):
        captured["args"] = args
        return (200, {"data": []}, "")

    monkeypatch.setattr(mod, "_curl_json", fake_curl)
    await mod._probe_llm_credential("anthropic", "sk-ant-abc", {})
    args = captured["args"]
    assert any(a == "x-api-key: sk-ant-abc" for a in args)
    assert any("anthropic-version" in a for a in args)


async def test_probe_dispatch_uses_query_param_for_google(monkeypatch):
    from backend.routers import llm_credentials as mod

    captured: dict = {}

    async def fake_curl(args):
        captured["args"] = args
        return (200, {"models": [{}, {}, {}]}, "")

    monkeypatch.setattr(mod, "_curl_json", fake_curl)
    out = await mod._probe_llm_credential("google", "AIza-fake", {})
    assert out["status"] == "ok"
    assert out["model_count"] == 3
    url_arg = captured["args"][-1]
    assert "key=AIza-fake" in url_arg


async def test_probe_non_2xx_returns_error_with_upstream_message(
    monkeypatch,
):
    from backend.routers import llm_credentials as mod

    async def fake_curl(args):
        return (
            401,
            {"error": {"message": "invalid_api_key"}},
            "{\"error\":{\"message\":\"invalid_api_key\"}}",
        )

    monkeypatch.setattr(mod, "_curl_json", fake_curl)
    out = await mod._probe_llm_credential("openai", "sk-bad", {})
    assert out["status"] == "error"
    assert out["http_status"] == 401
    assert "invalid_api_key" in out["message"]
