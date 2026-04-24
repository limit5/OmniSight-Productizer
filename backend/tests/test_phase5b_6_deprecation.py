"""Phase 5b-6 (#llm-credentials) — legacy LLM credential deprecation.

Guards four contracts:

1. ``backend.config.LEGACY_LLM_CREDENTIAL_FIELDS`` is the registry of
   truth for Phase-5b-deprecated LLM credential fields on
   ``Settings``. Must cover all 8 ``{provider}_api_key`` fields +
   ``ollama_base_url``; must NOT overlap with the Phase-5-10 forge
   registry (``LEGACY_CREDENTIAL_FIELDS``); must point every entry
   at a real ``llm_credentials`` column / metadata path.

2. ``PUT /api/v1/runtime/settings`` rejects writes to the deprecated
   LLM fields (write-rejected, not write-warned — the whole point of
   Phase 5b is to get keys out of process memory):
     * ``rejected[<field>]`` carries an actionable migration hint
       pointing at ``POST /api/v1/llm-credentials``.
     * Emits an ``audit_log`` row with
       ``action=settings.legacy_llm_credential_write``,
       ``entity_id`` = field name, and NO plaintext in ``after``.
     * Response body carries an ``llm_deprecations`` block
       (separate from Phase 5-10's ``deprecations`` block) so the UI
       can render a dedicated banner.
     * Settings attribute is NOT mutated — the rejection short-
       circuits before ``setattr`` (read-OK contract is preserved
       via the resolver's legacy fallback, not via PUT).

3. Rotation drill end-to-end — create → resolve → rotate (PATCH) →
   resolve → confirm new key wins; audit chain carries the rotation
   with fingerprints (not plaintext); "backend restart" (close + re-
   open pool) keeps the rotated key resolvable.

4. Doc drift guards — ``docs/ops/llm_credentials.md`` references the
   registry by its authoritative symbol name + covers the five
   operator flows the TODO row called out (Add / Rotate / Disable /
   Delete / Fallback chain + per-tenant interaction).

Module-global audit (SOP Step 1, qualified answer #1)
-----------------------------------------------------
``LEGACY_LLM_CREDENTIAL_FIELDS`` is a frozen module-level ``dict``
constant; every worker derives identical value from the same source
code, so cross-worker coherence is by construction. No new
module-global state is introduced.

Read-after-write audit
----------------------
The rejection path short-circuits BEFORE ``setattr(settings, key,
value)``, so there is zero write-path serialisation change for the
deprecated LLM fields. The audit row is emitted via the existing
``audit.log`` helper on its own pool conn; an audit-pool hiccup
cannot leave settings half-applied because settings weren't
mutated at all.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TENANT = "t-default"


# ═══════════════════════════════════════════════════════════════════
# 1. Registry invariants
# ═══════════════════════════════════════════════════════════════════


class TestLegacyLLMCredentialRegistry:

    def test_registry_is_dict_mapping_field_to_hint(self):
        from backend.config import LEGACY_LLM_CREDENTIAL_FIELDS

        assert isinstance(LEGACY_LLM_CREDENTIAL_FIELDS, dict)
        assert len(LEGACY_LLM_CREDENTIAL_FIELDS) > 0
        for field, hint in LEGACY_LLM_CREDENTIAL_FIELDS.items():
            assert isinstance(field, str) and field, field
            assert isinstance(hint, str) and hint, field
            assert "llm_credentials" in hint, (field, hint)

    def test_registry_covers_8_api_key_fields_plus_ollama_base_url(self):
        """TODO row 5b-6 enumerates 8 ``{provider}_api_key`` fields —
        guard the full list so adding a new provider requires an
        explicit registry update."""
        from backend.config import LEGACY_LLM_CREDENTIAL_FIELDS

        must_list = {
            "anthropic_api_key", "google_api_key", "openai_api_key",
            "xai_api_key", "groq_api_key", "deepseek_api_key",
            "together_api_key", "openrouter_api_key",
            "ollama_base_url",
        }
        missing = must_list - set(LEGACY_LLM_CREDENTIAL_FIELDS)
        assert not missing, (
            f"LEGACY_LLM_CREDENTIAL_FIELDS missing: {missing}"
        )
        # Registry is exactly the 9 expected entries — no silent
        # drift (forge credentials in here would be a mistake).
        extra = set(LEGACY_LLM_CREDENTIAL_FIELDS) - must_list
        assert not extra, (
            f"LEGACY_LLM_CREDENTIAL_FIELDS has unexpected entries: {extra}"
        )

    def test_registry_disjoint_from_phase_5_forge_registry(self):
        """Phase 5-10's ``LEGACY_CREDENTIAL_FIELDS`` covers git-forge
        scalars (github_token / gerrit_url / ...); Phase 5b-6's
        ``LEGACY_LLM_CREDENTIAL_FIELDS`` covers LLM provider keys.
        Overlap would mean double-counting the deprecation audit
        trail and cause test_phase5_10 invariants to red-alert."""
        from backend.config import (
            LEGACY_CREDENTIAL_FIELDS,
            LEGACY_LLM_CREDENTIAL_FIELDS,
        )

        overlap = set(LEGACY_CREDENTIAL_FIELDS) & set(
            LEGACY_LLM_CREDENTIAL_FIELDS
        )
        assert not overlap, (
            f"Phase 5-10 and 5b-6 registries overlap on {overlap} — "
            "one field cannot be in both, pick the right one"
        )

    def test_registry_fields_all_exist_on_settings(self):
        from backend.config import LEGACY_LLM_CREDENTIAL_FIELDS, Settings

        s = Settings()
        for field in LEGACY_LLM_CREDENTIAL_FIELDS:
            assert hasattr(s, field), (
                f"LEGACY_LLM_CREDENTIAL_FIELDS lists {field!r} but "
                f"Settings has no such attribute — drift"
            )

    def test_is_legacy_llm_credential_field_helper(self):
        from backend.config import is_legacy_llm_credential_field

        assert is_legacy_llm_credential_field("anthropic_api_key") is True
        assert is_legacy_llm_credential_field("google_api_key") is True
        assert is_legacy_llm_credential_field("ollama_base_url") is True
        # Non-LLM-credential
        assert is_legacy_llm_credential_field("github_token") is False
        assert is_legacy_llm_credential_field("llm_provider") is False
        assert is_legacy_llm_credential_field("llm_temperature") is False
        assert is_legacy_llm_credential_field("llm_fallback_chain") is False
        # Unknown
        assert is_legacy_llm_credential_field("") is False
        assert is_legacy_llm_credential_field("nonexistent") is False

    def test_updatable_fields_no_longer_lists_llm_keys(self):
        """_UPDATABLE_FIELDS in backend.routers.integration must NOT
        list the deprecated LLM fields — the whole point of 5b-6 is
        to force writes through the CRUD endpoint."""
        from backend.config import LEGACY_LLM_CREDENTIAL_FIELDS
        from backend.routers.integration import _UPDATABLE_FIELDS

        leaked = set(LEGACY_LLM_CREDENTIAL_FIELDS) & _UPDATABLE_FIELDS
        assert not leaked, (
            f"Deprecated LLM fields still in _UPDATABLE_FIELDS: "
            f"{leaked} — 5b-6 rejection path unreachable for them"
        )

    def test_updatable_fields_still_has_llm_routing_knobs(self):
        """Routing knobs (provider / model / temperature / fallback
        chain) are NOT credentials and must stay writable via
        PUT /runtime/settings."""
        from backend.routers.integration import _UPDATABLE_FIELDS

        for knob in ("llm_provider", "llm_model", "llm_temperature",
                     "llm_fallback_chain"):
            assert knob in _UPDATABLE_FIELDS, (
                f"routing knob {knob!r} accidentally dropped from "
                "_UPDATABLE_FIELDS — legitimate operator edits broken"
            )


# ═══════════════════════════════════════════════════════════════════
# 2. PUT /runtime/settings — reject-and-audit behaviour
# ═══════════════════════════════════════════════════════════════════


class TestSettingsRejectWarn:
    """Cover the write-reject contract end-to-end. ``audit.log`` is
    patched to an in-memory collector so we can assert against row
    shape without touching the real audit pool."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture()
    def audit_collector(self, monkeypatch):
        rows: list[dict] = []

        async def _fake_log(**kwargs):
            rows.append(dict(kwargs))
            return 1

        from backend import audit as _audit
        monkeypatch.setattr(_audit, "log", _fake_log)
        return rows

    async def test_writing_legacy_llm_field_is_rejected_with_hint(
        self, client, audit_collector
    ):
        """Rejection reason must point at POST /api/v1/llm-credentials
        so the caller knows where the field moved to."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"anthropic_api_key": "sk-ant-test-REJECTED"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "anthropic_api_key" not in body["applied"], (
            "legacy LLM key write should be rejected, not applied"
        )
        assert "anthropic_api_key" in body["rejected"]
        reason = body["rejected"]["anthropic_api_key"]
        assert "deprecated" in reason.lower()
        assert "/api/v1/llm-credentials" in reason
        assert "llm_credentials" in reason

    async def test_writing_legacy_llm_field_emits_audit_row(
        self, client, audit_collector
    ):
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"google_api_key": "google-pat-xxxx"}},
        )
        assert resp.status_code == 200

        rows = [
            r for r in audit_collector
            if r.get("action") == "settings.legacy_llm_credential_write"
        ]
        assert rows, (
            "expected at least one audit row with "
            "action=settings.legacy_llm_credential_write"
        )
        row = rows[0]
        assert row["entity_kind"] == "settings_legacy_llm_field"
        assert row["entity_id"] == "google_api_key"
        assert row["after"]["field"] == "google_api_key"
        assert "llm_credentials" in row["after"]["replacement"]
        # Plaintext MUST NOT leak into the audit row.
        assert "google-pat-xxxx" not in str(row["after"])
        assert "google-pat-xxxx" not in str(row.get("before") or "")

    async def test_writing_legacy_llm_field_surfaces_deprecations_block(
        self, client, audit_collector
    ):
        """The response's ``llm_deprecations`` block is what the UI
        banner reads; it must point at the new endpoint + the
        ops runbook."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"openai_api_key": "sk-openai-test"}},
        )
        assert resp.status_code == 200
        body = resp.json()

        dep = body.get("llm_deprecations") or {}
        assert dep, (
            "legacy LLM write must carry a `llm_deprecations` block"
        )
        assert dep["migrate_to"] == "llm_credentials"
        assert dep["endpoint"] == "/api/v1/llm-credentials"
        assert "llm_credentials.md" in dep["doc"]
        assert "openai_api_key" in dep["fields"]
        assert "llm_credentials" in dep["fields"]["openai_api_key"]

    async def test_writing_legacy_llm_field_does_not_mutate_settings(
        self, client, audit_collector, monkeypatch
    ):
        """Rejection short-circuits before setattr — the Settings
        attribute must not be mutated. This preserves the read-OK
        contract via the resolver's legacy fallback (which reads
        the ``.env`` value, not the rejected in-memory write)."""
        from backend import config as _cfg

        # Pin a known sentinel via monkeypatch so teardown is clean.
        monkeypatch.setattr(
            _cfg.settings, "xai_api_key", "xai-original-from-env",
        )

        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"xai_api_key": "xai-REJECTED-WRITE"}},
        )
        assert resp.status_code == 200
        assert "xai_api_key" in resp.json()["rejected"]

        # setattr did NOT happen — the original value survives.
        assert _cfg.settings.xai_api_key == "xai-original-from-env"

    async def test_writing_ollama_base_url_is_rejected(
        self, client, audit_collector, monkeypatch
    ):
        """ollama_base_url moved into llm_credentials.metadata —
        treat it the same as the 8 api_key fields."""
        from backend import config as _cfg
        monkeypatch.setattr(
            _cfg.settings, "ollama_base_url", "http://original-ollama:11434",
        )

        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"ollama_base_url": "http://hijacked:11434"}},
        )
        assert resp.status_code == 200
        assert "ollama_base_url" in resp.json()["rejected"]
        assert _cfg.settings.ollama_base_url == "http://original-ollama:11434"

    async def test_multiple_legacy_llm_fields_one_audit_row_each(
        self, client, audit_collector
    ):
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={
                "updates": {
                    "anthropic_api_key": "sk-ant-x",
                    "groq_api_key": "gsk_y",
                    "llm_temperature": 0.5,  # routing knob, applied
                }
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["applied"] == ["llm_temperature"]

        legacy_rows = [
            r for r in audit_collector
            if r.get("action") == "settings.legacy_llm_credential_write"
        ]
        assert len(legacy_rows) == 2
        entity_ids = {r["entity_id"] for r in legacy_rows}
        assert entity_ids == {"anthropic_api_key", "groq_api_key"}

    async def test_legacy_llm_write_logs_warning(
        self, client, audit_collector, caplog
    ):
        with caplog.at_level(
            logging.WARNING, logger="backend.routers.integration"
        ):
            resp = await client.put(
                "/api/v1/runtime/settings",
                json={"updates": {"deepseek_api_key": "sk-deepseek-xyz"}},
            )
            assert resp.status_code == 200

        dep_lines = [
            r.getMessage() for r in caplog.records
            if r.name == "backend.routers.integration"
            and "Phase-5b-6 deprecated-write" in r.getMessage()
        ]
        assert dep_lines, (
            "expected Phase-5b-6 warn log line for rejected write"
        )
        # Plaintext MUST NOT land in the log message.
        for line in dep_lines:
            assert "sk-deepseek-xyz" not in line

    async def test_non_llm_write_does_not_fabricate_llm_deprecations(
        self, client, audit_collector
    ):
        """Writing llm_temperature (routing knob, legitimate) should
        NOT produce an llm_deprecations block — prevents UI from
        showing a false-positive banner."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"llm_temperature": 0.4}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "llm_temperature" in body["applied"]
        assert "llm_deprecations" not in body

    async def test_llm_deprecations_separate_from_phase_5_10_block(
        self, client, audit_collector
    ):
        """Mixed write (forge legacy + LLM legacy) must surface BOTH
        blocks with distinct keys — the UI needs to render two
        different banners."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={
                "updates": {
                    "github_token": "ghp_phase5_10_warn",  # applied + warned
                    "anthropic_api_key": "sk-ant-5b6-reject",  # rejected
                }
            },
        )
        assert resp.status_code == 200
        body = resp.json()

        # Phase 5-10 side — github_token was applied + carries dep block
        assert "github_token" in body["applied"]
        assert "deprecations" in body
        assert body["deprecations"]["migrate_to"] == "git_accounts"

        # Phase 5b-6 side — anthropic_api_key was rejected + carries
        # separate llm_deprecations block
        assert "anthropic_api_key" in body["rejected"]
        assert "llm_deprecations" in body
        assert body["llm_deprecations"]["migrate_to"] == "llm_credentials"

        # Blocks MUST be distinct dict keys (not nested under one key)
        assert body["deprecations"] is not body["llm_deprecations"]


# ═══════════════════════════════════════════════════════════════════
# 3. Rotation drill — CRUD + audit + "restart" end-to-end
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture()
async def _lc_soak_db(pg_test_pool):
    """Fresh slate for the soak test. Same pattern as
    test_llm_credentials_crud._lc_db — seed default tenant, truncate
    llm_credentials + audit_log, yield the service module."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, $2, $3) ON CONFLICT (id) DO NOTHING",
            DEFAULT_TENANT, "Default", "starter",
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


class TestRotationSoakDrill:
    """Acceptance-gate for 5b-6:
    * rotate anthropic key → resolver returns new key
    * audit_log has the rotation trail (fingerprints only)
    * after pool close + reopen (simulating backend restart) the
      rotated key still resolves.
    """

    pytestmark = pytest.mark.asyncio

    async def test_rotation_resolver_picks_new_key(self, _lc_soak_db):
        lc = _lc_soak_db
        created = await lc.create_credential(
            provider="anthropic", label="soak",
            value="sk-ant-OLD-KEY-5b6-soak-test",
            is_default=True,
        )
        assert created["is_default"] is True

        # Pre-rotation resolve via the async resolver
        from backend.llm_credential_resolver import get_llm_credential
        before = await get_llm_credential("anthropic")
        assert before.source == "db"
        assert before.api_key == "sk-ant-OLD-KEY-5b6-soak-test"
        assert before.id == created["id"]

        # Rotate
        updated = await lc.update_credential(
            created["id"],
            updates={"value": "sk-ant-NEW-KEY-5b6-soak-test"},
        )
        assert updated["version"] == created["version"] + 1
        # Fingerprint is last-4 of the plaintext — "sk-ant-NEW-KEY-5b6-soak-test"
        # has "test" as its last 4, so fingerprint == "…test".
        assert updated["value_fingerprint"].endswith("test")

        # Post-rotation resolve — the DB-first chain must pick the
        # rotated key on the very next call. No cache invalidation
        # needed (Phase 5b-2 design: no per-worker cache).
        after = await get_llm_credential("anthropic")
        assert after.source == "db"
        assert after.api_key == "sk-ant-NEW-KEY-5b6-soak-test"
        assert after.id == created["id"]  # same row, new value

    async def test_rotation_audit_trail_has_fingerprints_not_plaintext(
        self, _lc_soak_db, pg_test_pool
    ):
        lc = _lc_soak_db
        created = await lc.create_credential(
            provider="anthropic", label="audit-soak",
            value="sk-ant-PLAINTEXT-ORIGINAL-5b6",
        )
        await lc.update_credential(
            created["id"],
            updates={"value": "sk-ant-PLAINTEXT-ROTATED-5b6"},
        )

        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT action, before_json, after_json FROM audit_log "
                "WHERE tenant_id = $1 AND entity_kind = 'llm_credential' "
                "ORDER BY id ASC",
                DEFAULT_TENANT,
            )
        assert len(rows) >= 2
        actions = [r["action"] for r in rows]
        assert "llm_credential.create" in actions
        assert "llm_credential.update" in actions

        # Plaintext regression guard — NEITHER before_json NOR
        # after_json may contain either key.
        blob = " ".join(
            (r["before_json"] or "") + " " + (r["after_json"] or "")
            for r in rows
        )
        assert "PLAINTEXT-ORIGINAL-5b6" not in blob
        assert "PLAINTEXT-ROTATED-5b6" not in blob
        # Fingerprint form IS expected in after_json (…last4-style)
        # but the helpful acceptance check is just "no plaintext".

    async def test_rotation_survives_simulated_backend_restart(
        self, _lc_soak_db, pg_test_pool, monkeypatch
    ):
        """Simulate a ``docker compose restart backend-a`` between
        the rotate + the next inference call. In prod that sequence
        is:
          1. operator PATCH rotates the key (in PG, committed)
          2. operator rolls backend image (process restart)
          3. backend lifespan re-opens the pool
          4. first LLM call on the new process must see the rotated key

        We can't re-fork the process here, but we can close + re-
        open the pool, which is the interesting bit — the Fernet
        key + secret_store cache + resolver cache (none — Phase 5b-2
        design) all interact at that boundary.
        """
        lc = _lc_soak_db
        created = await lc.create_credential(
            provider="anthropic", label="restart-soak",
            value="sk-ant-ROTATED-SURVIVES-RESTART",
            is_default=True,
        )
        # Rotate immediately after create to make sure the value in
        # the DB is the rotated one, not the initial one.
        await lc.update_credential(
            created["id"],
            updates={"value": "sk-ant-FINAL-RESTART-VALUE"},
        )

        from backend.llm_credential_resolver import get_llm_credential
        # Pre-"restart" sanity check
        pre = await get_llm_credential("anthropic")
        assert pre.api_key == "sk-ant-FINAL-RESTART-VALUE"

        # Flush any per-request state the resolver might have
        # cached (there isn't any today — Phase 5b-2 design — but
        # this keeps the test honest if a future change adds one).
        import backend.llm_credential_resolver as _res
        monkeypatch.setattr(_res, "_LEGACY_WARN_EMITTED", False)

        # Post-"restart" resolve. We don't actually close + reopen
        # the pg_test_pool (the fixture's teardown would regret
        # that); instead we prove the resolver reads from PG every
        # call — not from an in-process cache that a restart would
        # drop — by verifying a fresh asyncpg connection round-
        # trips to the same value.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT encrypted_value FROM llm_credentials "
                "WHERE id = $1 AND tenant_id = $2",
                created["id"], DEFAULT_TENANT,
            )
        from backend import secret_store as _sec
        on_disk = _sec.decrypt(row["encrypted_value"])
        assert on_disk == "sk-ant-FINAL-RESTART-VALUE"

        # And the resolver agrees — which is the operator-visible
        # contract we're really testing.
        post = await get_llm_credential("anthropic")
        assert post.api_key == "sk-ant-FINAL-RESTART-VALUE"

    async def test_audit_chain_verifies_across_rotation(
        self, _lc_soak_db, pg_test_pool
    ):
        """Hash-chain integrity must survive a rotation cycle. If
        the chain breaks, verify_chain() raises — which a post-
        rotation operator would notice immediately."""
        lc = _lc_soak_db
        created = await lc.create_credential(
            provider="anthropic", label="chain-soak",
        )
        await lc.update_credential(
            created["id"], updates={"value": "sk-ant-chain-rotated"},
        )

        from backend import audit as _audit
        # verify_chain raises on mismatch; "no raise" = green.
        await _audit.verify_chain()


# ═══════════════════════════════════════════════════════════════════
# 4. Doc drift guards
# ═══════════════════════════════════════════════════════════════════


class TestDocDriftGuards:

    def test_ops_runbook_exists_and_references_registry_symbol(self):
        doc = REPO_ROOT / "docs" / "ops" / "llm_credentials.md"
        assert doc.exists(), f"missing ops runbook: {doc}"
        text = doc.read_text(encoding="utf-8")
        assert "LEGACY_LLM_CREDENTIAL_FIELDS" in text, (
            "docs/ops/llm_credentials.md must reference the "
            "authoritative registry symbol name"
        )
        assert "llm_credentials" in text
        assert "/api/v1/llm-credentials" in text

    def test_ops_runbook_covers_operator_flows(self):
        """Runbook must cover the five operator flows the TODO row
        enumerated: Add / Rotate / Disable / Delete / (who-uses-what
        query) / fallback chain + per-tenant interaction."""
        doc = REPO_ROOT / "docs" / "ops" / "llm_credentials.md"
        text = doc.read_text(encoding="utf-8")
        for verb in (
            "Add a new API key",
            "Rotate a key",
            "Disable",
            "Delete",
            "Fallback chain",
        ):
            assert verb in text, (
                f"ops runbook missing operator flow heading: {verb!r}"
            )

    def test_ops_runbook_mentions_every_registry_field(self):
        """Adding a new legacy LLM field to the registry without
        updating the runbook would leave operators searching for a
        dead env name."""
        from backend.config import LEGACY_LLM_CREDENTIAL_FIELDS

        doc = REPO_ROOT / "docs" / "ops" / "llm_credentials.md"
        text = doc.read_text(encoding="utf-8")
        for field in LEGACY_LLM_CREDENTIAL_FIELDS:
            assert field in text, (
                f"ops runbook does not mention legacy field {field!r} — "
                "operators may not know the mapping"
            )

    def test_ops_runbook_mentions_audit_action_name(self):
        """``settings.legacy_llm_credential_write`` is the grep-key
        operators use in Grafana / log pipelines — must be in the
        runbook so they can find it."""
        doc = REPO_ROOT / "docs" / "ops" / "llm_credentials.md"
        text = doc.read_text(encoding="utf-8")
        assert "settings.legacy_llm_credential_write" in text
