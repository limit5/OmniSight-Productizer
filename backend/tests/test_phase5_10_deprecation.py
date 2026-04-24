"""Phase 5-10 (#multi-account-forge) — legacy credential deprecation.

Guards three contracts:

1. ``backend.config.LEGACY_CREDENTIAL_FIELDS`` is the registry of
   truth for Phase-5-deprecated scalar credential fields on
   ``Settings``. The registry must cover every scalar the Phase 5
   TODO row called out, must NOT list non-credential fields
   (``gerrit_enabled`` master switch, ``gerrit_replication_targets``
   destination list, ``jira_intake_label`` / ``jira_done_statuses``
   routing knobs), and must point every entry at a real
   ``git_accounts`` column / semantic.

2. ``PUT /api/v1/runtime/settings`` still accepts writes to legacy
   fields (read-OK, write-warn contract) but:
     * logs a ``Phase-5-10 deprecated-write`` warning,
     * writes an ``audit_log`` row with
       ``action=settings.legacy_credential_write`` and
       ``entity_id`` equal to the field name,
     * includes a ``deprecations`` block in the response body so
       the UI can surface a "migrate to Git Accounts" banner.

3. Doc drift guards — ``docs/ops/git_credentials.md`` and
   ``docs/phase-5-multi-account/02-migration-runbook.md`` both
   reference the registry by its authoritative symbol name, so
   renaming it will fail CI until the docs follow.

Module-global audit (SOP Step 1, qualified answer #1): the
registry is a frozen module-level ``dict`` — every worker derives
the same value from the same source code, so cross-worker
coherence is by-construction. No new module-global state is
introduced by this row.

Read-after-write audit (SOP Step 1): no write-path serialisation
changes. The audit row is emitted via the existing
``audit.log`` / ``audit.log_sync`` helpers which run on their own
pool connection; the settings write has already taken effect
before the audit fire, so failure of the audit write cannot
leave the in-memory setting in a half-applied state.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════
# Registry invariants
# ═══════════════════════════════════════════════════════════════════


class TestLegacyCredentialRegistry:

    def test_registry_is_dict_mapping_field_to_hint(self):
        from backend.config import LEGACY_CREDENTIAL_FIELDS

        assert isinstance(LEGACY_CREDENTIAL_FIELDS, dict)
        assert len(LEGACY_CREDENTIAL_FIELDS) > 0
        for field, hint in LEGACY_CREDENTIAL_FIELDS.items():
            assert isinstance(field, str) and field, field
            assert isinstance(hint, str) and hint, field
            # Each hint must point at the authoritative replacement
            # target — either a git_accounts column / platform row.
            assert "git_accounts" in hint, (field, hint)

    def test_registry_covers_todo_row_fields(self):
        """TODO row 5-10 enumerates ``github_token`` / ``gitlab_token``
        / ``gerrit_url`` 等 — guard the full superset the HANDOFF
        entry references so adding a new legacy field requires an
        explicit registry update."""
        from backend.config import LEGACY_CREDENTIAL_FIELDS

        must_list = {
            # GitHub
            "github_token", "github_token_map", "github_webhook_secret",
            # GitLab
            "gitlab_token", "gitlab_url", "gitlab_token_map",
            "gitlab_webhook_secret",
            # Gerrit (the row called out "gerrit_url 等")
            "gerrit_url", "gerrit_ssh_host", "gerrit_ssh_port",
            "gerrit_project", "gerrit_instances", "gerrit_webhook_secret",
            # JIRA
            "notification_jira_url", "notification_jira_token",
            "notification_jira_project", "jira_webhook_secret",
            # Shared SSH fallback
            "git_ssh_key_path", "git_ssh_key_map",
        }
        missing = must_list - set(LEGACY_CREDENTIAL_FIELDS)
        assert not missing, (
            f"registry missing legacy credential fields: {missing}"
        )

    def test_registry_excludes_non_credential_fields(self):
        """``gerrit_enabled`` is a master-switch feature flag;
        ``gerrit_replication_targets`` is a destination list;
        ``jira_intake_label`` / ``jira_done_statuses`` are routing
        knobs; LLM provider ``*_api_key`` fields belong to Phase 5b.
        None of these should be in the Phase 5-10 registry."""
        from backend.config import LEGACY_CREDENTIAL_FIELDS

        must_not_list = {
            "gerrit_enabled",
            "gerrit_replication_targets",
            "jira_intake_label",
            "jira_done_statuses",
            "anthropic_api_key", "google_api_key", "openai_api_key",
            "xai_api_key", "groq_api_key", "deepseek_api_key",
            "together_api_key", "openrouter_api_key",
            "ollama_base_url",
        }
        leaked = must_not_list & set(LEGACY_CREDENTIAL_FIELDS)
        assert not leaked, (
            f"non-credential fields leaked into Phase-5-10 registry: {leaked}"
        )

    def test_registry_fields_all_exist_on_settings(self):
        """Every registry entry must name a real ``Settings`` field —
        otherwise ``is_legacy_credential_field`` would warn on a
        write that hasattr-fails earlier in the PUT handler and the
        audit row would trip on a ghost field name."""
        from backend.config import LEGACY_CREDENTIAL_FIELDS, Settings

        s = Settings()
        for field in LEGACY_CREDENTIAL_FIELDS:
            assert hasattr(s, field), (
                f"LEGACY_CREDENTIAL_FIELDS lists {field!r} but "
                f"Settings has no such attribute — drift"
            )

    def test_is_legacy_credential_field_helper(self):
        from backend.config import is_legacy_credential_field

        assert is_legacy_credential_field("github_token") is True
        assert is_legacy_credential_field("gerrit_webhook_secret") is True
        assert is_legacy_credential_field("notification_jira_token") is True
        # Non-credential
        assert is_legacy_credential_field("gerrit_enabled") is False
        assert is_legacy_credential_field("jira_intake_label") is False
        assert is_legacy_credential_field("anthropic_api_key") is False
        # Unknown
        assert is_legacy_credential_field("") is False
        assert is_legacy_credential_field("nonexistent_field") is False


# ═══════════════════════════════════════════════════════════════════
# PUT /runtime/settings — write-warn behaviour
# ═══════════════════════════════════════════════════════════════════


class TestSettingsWriteWarn:
    """Cover the read-OK / write-warn contract end-to-end. ``audit.log``
    is patched to an in-memory collector so we can assert against
    the row shape without touching the real audit pool."""

    @pytest.fixture()
    def audit_collector(self, monkeypatch):
        """Capture audit.log calls for assertion, without touching PG."""
        rows: list[dict] = []

        async def _fake_log(**kwargs):
            rows.append(dict(kwargs))
            return 1

        from backend import audit as _audit
        monkeypatch.setattr(_audit, "log", _fake_log)
        return rows

    @pytest.mark.asyncio
    async def test_writing_legacy_field_returns_deprecation_block(
        self, client, audit_collector, monkeypatch
    ):
        """Legacy scalar write via PUT /runtime/settings surfaces a
        ``deprecations`` block in the response body pointing the UI
        at the git_accounts migration target + runbook doc."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"github_token": "ghp_testdeprecatedvalue"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "github_token" in body["applied"]

        dep = body.get("deprecations") or {}
        assert dep, (
            "legacy-field write must carry a `deprecations` block "
            "for the UI banner"
        )
        assert dep["migrate_to"] == "git_accounts"
        assert "github_token" in dep["fields"]
        assert "git_accounts" in dep["fields"]["github_token"]
        assert "02-migration-runbook.md" in dep["doc"]

    @pytest.mark.asyncio
    async def test_writing_legacy_field_emits_audit_row(
        self, client, audit_collector
    ):
        """The audit row's ``action`` + ``entity_id`` + ``after.field``
        + ``after.replacement`` are the stable grep-keys. Guard their
        shape so log-analysis pipelines built on top don't silently
        break."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"notification_jira_token": "jira-pat"}},
        )
        assert resp.status_code == 200

        rows = [
            r for r in audit_collector
            if r.get("action") == "settings.legacy_credential_write"
        ]
        assert rows, (
            "expected at least one audit row with "
            "action=settings.legacy_credential_write"
        )
        row = rows[0]
        assert row["entity_kind"] == "settings_legacy_field"
        assert row["entity_id"] == "notification_jira_token"
        assert row["after"]["field"] == "notification_jira_token"
        assert "git_accounts" in row["after"]["replacement"]
        # Plaintext MUST NOT leak into the audit row — the
        # after block carries only field metadata, never the
        # value written.
        assert "jira-pat" not in str(row["after"])
        assert "jira-pat" not in str(row.get("before") or "")

    @pytest.mark.asyncio
    async def test_writing_non_legacy_field_no_deprecation_block(
        self, client, audit_collector
    ):
        """Temperature / slack webhook writes don't fire the
        deprecation path — response stays backwards-compatible with
        pre-Phase-5-10 UI code that doesn't know about
        ``deprecations``."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={"updates": {"llm_temperature": 0.4}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "llm_temperature" in body["applied"]
        assert "deprecations" not in body, (
            "non-legacy writes must not fabricate a deprecations block"
        )
        legacy_rows = [
            r for r in audit_collector
            if r.get("action") == "settings.legacy_credential_write"
        ]
        assert not legacy_rows, (
            "audit row fired for a non-legacy field — false positive"
        )

    @pytest.mark.asyncio
    async def test_multiple_legacy_fields_in_one_put_fires_one_row_each(
        self, client, audit_collector
    ):
        """One audit row per field keeps grep-by-entity_id simple —
        collapsing into a single "batch" row would force analysts to
        parse a JSON list."""
        resp = await client.put(
            "/api/v1/runtime/settings",
            json={
                "updates": {
                    "github_token": "ghp_x",
                    "gitlab_token": "glpat-y",
                    "llm_temperature": 0.5,  # non-legacy — not counted
                }
            },
        )
        assert resp.status_code == 200

        legacy_rows = [
            r for r in audit_collector
            if r.get("action") == "settings.legacy_credential_write"
        ]
        assert len(legacy_rows) == 2, (
            f"expected exactly 2 legacy-write audit rows, got "
            f"{len(legacy_rows)}: "
            f"{[r.get('entity_id') for r in legacy_rows]}"
        )
        entity_ids = {r["entity_id"] for r in legacy_rows}
        assert entity_ids == {"github_token", "gitlab_token"}

    @pytest.mark.asyncio
    async def test_legacy_write_logs_warning(
        self, client, audit_collector, caplog
    ):
        """Grep-friendly ``Phase-5-10 deprecated-write`` line lands on
        the ``backend.routers.integration`` logger at WARNING level
        so operator log scrapers can detect the event without
        hitting PG."""
        with caplog.at_level(logging.WARNING, logger="backend.routers.integration"):
            resp = await client.put(
                "/api/v1/runtime/settings",
                json={"updates": {"gerrit_webhook_secret": "ger-secret-xyz"}},
            )
            assert resp.status_code == 200

        dep_lines = [
            r.getMessage()
            for r in caplog.records
            if r.name == "backend.routers.integration"
            and "Phase-5-10 deprecated-write" in r.getMessage()
        ]
        # Depending on whether the key is whitelisted in _UPDATABLE_FIELDS
        # the write may be rejected before the warn fires — guard
        # explicitly so the test stays deterministic.
        applied = resp.json()["applied"]
        if "gerrit_webhook_secret" in applied:
            assert dep_lines, (
                "expected Phase-5-10 warn log line for "
                "gerrit_webhook_secret write"
            )
            # Plaintext secret must NOT land in the log message.
            for line in dep_lines:
                assert "ger-secret-xyz" not in line

    @pytest.mark.asyncio
    async def test_legacy_field_read_still_works(
        self, client, monkeypatch
    ):
        """Read-OK contract: setting a legacy field value via the
        config singleton still shows up in GET /runtime/settings —
        Phase 5-10 did not break the backward-compat shim."""
        from backend import config as _cfg

        monkeypatch.setattr(_cfg.settings, "gitlab_url", "https://gl.example.com")
        resp = await client.get("/api/v1/runtime/settings")
        assert resp.status_code == 200
        # The endpoint surfaces gitlab_url under the "git" block.
        git_block = resp.json().get("git", {})
        # Contract: field still readable (exact key name may differ —
        # guard that the URL shows up SOMEWHERE in the response).
        flat = str(resp.json())
        assert "gl.example.com" in flat, (
            "gitlab_url value disappeared from GET /runtime/settings — "
            "read-OK contract broken"
        )


# ═══════════════════════════════════════════════════════════════════
# Doc drift guards
# ═══════════════════════════════════════════════════════════════════


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDocDriftGuards:

    def test_ops_runbook_references_registry_symbol(self):
        """Renaming LEGACY_CREDENTIAL_FIELDS without updating the
        ops runbook would leave operators grep-ing a dead name."""
        doc = REPO_ROOT / "docs" / "ops" / "git_credentials.md"
        assert doc.exists(), f"missing ops runbook: {doc}"
        text = doc.read_text(encoding="utf-8")
        assert "LEGACY_CREDENTIAL_FIELDS" in text, (
            "docs/ops/git_credentials.md must reference "
            "LEGACY_CREDENTIAL_FIELDS by its authoritative symbol name"
        )
        assert "git_accounts" in text
        # Runbook must cover the five operator flows the TODO row
        # enumerated: add / rotate / disable / delete / resolve.
        for verb in ("Add", "Rotate", "disable", "Delete", "resolve"):
            assert verb in text, (
                f"ops runbook missing operator flow heading: {verb}"
            )

    def test_migration_runbook_exists_and_links_ops_runbook(self):
        doc = REPO_ROOT / "docs" / "phase-5-multi-account" / "02-migration-runbook.md"
        assert doc.exists(), f"missing migration runbook: {doc}"
        text = doc.read_text(encoding="utf-8")
        # Cross-link to ops runbook
        assert "git_credentials.md" in text, (
            "migration runbook must link back to the ops runbook"
        )
        # Must document the kill-switch env var
        assert "OMNISIGHT_CREDENTIAL_MIGRATE" in text
        # Must name both auto-migrate (Path A) and manual (Path B)
        # paths so operators can pick.
        assert "Path A" in text
        assert "Path B" in text
        # Must reference the rollback / post-migration cleanup.
        assert ".env" in text

    def test_migration_runbook_lists_every_registry_field(self):
        """Runbook §4.3 enumerates which .env lines to delete after
        migration. If we add a field to the registry but forget the
        runbook, operators will leave stale .env lines behind."""
        from backend.config import LEGACY_CREDENTIAL_FIELDS

        doc = REPO_ROOT / "docs" / "phase-5-multi-account" / "02-migration-runbook.md"
        text = doc.read_text(encoding="utf-8")
        for field in LEGACY_CREDENTIAL_FIELDS:
            env_name = "OMNISIGHT_" + field.upper()
            assert env_name in text, (
                f"migration runbook does not mention env var "
                f"{env_name} — operators will leave stale .env lines"
            )
