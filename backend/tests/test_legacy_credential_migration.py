"""Phase 5-5 (#multi-account-forge) — legacy → ``git_accounts``
auto-migration tests.

Three layers, mirroring the layout of test_git_credentials_phase5_2.py
+ test_git_accounts_crud.py:

1. Pure-unit ``_plan_rows`` tests (no PG, no pool) — verify the
   precedence rules between scalar fallbacks and ``*_token_map`` /
   ``gerrit_instances`` JSON inputs, and the deterministic-id slugger.
2. Pure-unit ``migrate_legacy_credentials_once`` tests with a stub
   pool — verify the kill-switch, the empty-Settings no-op, the
   no-pool fallback, and the idempotency check (table-not-empty).
3. PG live contract tests via ``pg_test_pool`` — verify a real
   end-to-end migration writes the expected ``git_accounts`` rows
   with correct platform / labels / fingerprint shape, that re-running
   is a true no-op (idempotency), and that two concurrent calls
   collapse to one row per deterministic id (worker-race safety).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest


# Mixed sync (``_plan_rows`` unit tests) + async tests in this module
# — apply ``@pytest.mark.asyncio`` per-test rather than module-wide so
# the sync tests don't trip the pytest-asyncio mark warning.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers — Settings monkeypatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _patched_settings(**overrides: Any):
    """Patch ``backend.legacy_credential_migration.settings`` with all
    legacy fields blanked out, then apply the overrides the caller
    cares about. Returns the ``unittest.mock.patch`` context manager
    so callers can ``with _patched_settings(...) as mock: ...``.
    """
    p = patch("backend.legacy_credential_migration.settings")
    mock = p.start()
    mock.github_token = ""
    mock.github_token_map = ""
    mock.gitlab_token = ""
    mock.gitlab_url = ""
    mock.gitlab_token_map = ""
    mock.gerrit_enabled = False
    mock.gerrit_url = ""
    mock.gerrit_ssh_host = ""
    mock.gerrit_ssh_port = 29418
    mock.gerrit_project = ""
    mock.gerrit_webhook_secret = ""
    mock.gerrit_instances = ""
    mock.notification_jira_url = ""
    mock.notification_jira_token = ""
    mock.notification_jira_project = ""
    mock.jira_webhook_secret = ""
    mock.git_ssh_key_path = ""
    for k, v in overrides.items():
        setattr(mock, k, v)
    return p, mock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. _plan_rows — pure-unit precedence + slugger contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_slug_lowercases_and_dashes():
    from backend.legacy_credential_migration import _slug
    assert _slug("github.com") == "github-com"
    assert _slug("GITHUB.COM") == "github-com"
    assert _slug("gitlab.client.example.org") == "gitlab-client-example-org"
    assert _slug("foo!bar?baz") == "foo-bar-baz"
    assert _slug("") == "unknown"


def test_host_from_url_handles_various_forms():
    from backend.legacy_credential_migration import _host_from_url
    assert _host_from_url("https://gitlab.internal.com") == "gitlab.internal.com"
    assert _host_from_url("https://gitlab.internal.com/") == "gitlab.internal.com"
    assert _host_from_url("https://Gitlab.Co/") == "gitlab.co"
    assert _host_from_url("gitlab.bare-host") == "gitlab.bare-host"
    assert _host_from_url("") == ""


def test_plan_rows_empty_settings_returns_empty_list():
    p, _ = _patched_settings()
    try:
        from backend.legacy_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_github_token_only_becomes_default_for_github_com():
    p, _ = _patched_settings(github_token="ghp_scalar_aaaa")
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == "ga-legacy-github-github-com"
        assert r["platform"] == "github"
        assert r["instance_url"] == "https://github.com"
        assert r["label"] == "github.com (legacy)"
        assert r["token"] == "ghp_scalar_aaaa"
        assert r["is_default"] is True
        assert r["enabled"] is True
        assert r["source"] == "github_token"
    finally:
        p.stop()


def test_plan_rows_github_token_map_creates_one_row_per_host_no_default():
    p, _ = _patched_settings(github_token_map=json.dumps({
        "github.com": "ghp_a", "github.enterprise.com": "ghp_b",
    }))
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 2
        ids = {r["id"] for r in rows}
        assert ids == {
            "ga-legacy-github-github-com",
            "ga-legacy-github-github-enterprise-com",
        }
        # None marked default — matches legacy shim behaviour for map entries.
        assert all(r["is_default"] is False for r in rows)
        assert all(r["platform"] == "github" for r in rows)
        # Labels include "(legacy)" tag.
        labels = sorted(r["label"] for r in rows)
        assert labels == ["github.com (legacy)", "github.enterprise.com (legacy)"]
    finally:
        p.stop()


def test_plan_rows_scalar_skipped_when_map_already_covers_github_com():
    """If ``github_token_map`` already has a ``github.com`` entry, the
    scalar ``github_token`` must NOT be migrated as a second row for
    the same host — preserves the legacy shim's "scalar is fallback
    only when map is empty for that platform" semantic and avoids
    duplicate ids."""
    p, _ = _patched_settings(
        github_token="ghp_scalar_should_be_skipped",
        github_token_map=json.dumps({"github.com": "ghp_from_map"}),
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        assert rows[0]["token"] == "ghp_from_map"
        assert rows[0]["source"] == "github_token_map[github.com]"
        # Default flag stays False — neither map entries nor a
        # short-circuited scalar take the default slot.
        assert rows[0]["is_default"] is False
    finally:
        p.stop()


def test_plan_rows_scalar_migrated_when_map_covers_only_other_host():
    """If the map only covers github.enterprise.com, the scalar
    ``github_token`` should still migrate as the github.com default."""
    p, _ = _patched_settings(
        github_token="ghp_scalar_for_github_com",
        github_token_map=json.dumps({"github.enterprise.com": "ghp_ent"}),
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 2
        scalars = [r for r in rows if r["source"] == "github_token"]
        assert len(scalars) == 1
        assert scalars[0]["id"] == "ga-legacy-github-github-com"
        assert scalars[0]["is_default"] is True
        assert scalars[0]["token"] == "ghp_scalar_for_github_com"
    finally:
        p.stop()


def test_plan_rows_gitlab_uses_gitlab_url_for_host_inference():
    p, _ = _patched_settings(
        gitlab_token="glpat_xx",
        gitlab_url="https://gitlab.internal.example.com",
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == "ga-legacy-gitlab-gitlab-internal-example-com"
        assert r["instance_url"] == "https://gitlab.internal.example.com"
        assert r["label"] == "gitlab.internal.example.com (legacy)"
        assert r["is_default"] is True
        assert r["source"] == "gitlab_token"
    finally:
        p.stop()


def test_plan_rows_gitlab_defaults_to_gitlab_com_when_url_empty():
    p, _ = _patched_settings(gitlab_token="glpat_yy")
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        assert rows[0]["id"] == "ga-legacy-gitlab-gitlab-com"
        assert rows[0]["instance_url"] == "https://gitlab.com"
    finally:
        p.stop()


def test_plan_rows_gerrit_instances_one_row_per_entry():
    p, _ = _patched_settings(gerrit_instances=json.dumps([
        {"id": "g1", "ssh_host": "gerrit-a.example.com", "ssh_port": 29418,
         "project": "p/a", "webhook_secret": "ws-a"},
        {"id": "g2", "ssh_host": "gerrit-b.example.com", "ssh_port": 22,
         "project": "p/b", "webhook_secret": "ws-b",
         "url": "https://gerrit-b.example.com"},
    ]))
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 2
        by_host = {r["ssh_host"]: r for r in rows}
        assert "gerrit-a.example.com" in by_host
        assert "gerrit-b.example.com" in by_host
        a = by_host["gerrit-a.example.com"]
        assert a["id"] == "ga-legacy-gerrit-gerrit-a-example-com"
        assert a["ssh_port"] == 29418
        assert a["project"] == "p/a"
        assert a["webhook_secret"] == "ws-a"
        assert a["is_default"] is False
        b = by_host["gerrit-b.example.com"]
        assert b["instance_url"] == "https://gerrit-b.example.com"
        assert b["ssh_port"] == 22
    finally:
        p.stop()


def test_plan_rows_gerrit_scalar_only_when_enabled_and_no_instance_collision():
    p, _ = _patched_settings(
        gerrit_enabled=True,
        gerrit_ssh_host="gerrit-fallback.example.com",
        gerrit_ssh_port=29418,
        gerrit_url="https://gerrit-fallback.example.com",
        gerrit_project="p/x",
        gerrit_webhook_secret="ws-fb",
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == "ga-legacy-gerrit-gerrit-fallback-example-com"
        assert r["is_default"] is True
        assert r["source"] == "gerrit_scalar"
    finally:
        p.stop()


def test_plan_rows_gerrit_scalar_skipped_if_disabled():
    p, _ = _patched_settings(
        gerrit_enabled=False,
        gerrit_ssh_host="gerrit-disabled.example.com",
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_gerrit_scalar_skipped_if_already_in_instances():
    p, _ = _patched_settings(
        gerrit_enabled=True,
        gerrit_ssh_host="gerrit-shared.example.com",
        gerrit_instances=json.dumps([
            {"ssh_host": "gerrit-shared.example.com", "ssh_port": 29418},
        ]),
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        # The instances entry wins (came first); scalar dedup'd out.
        assert rows[0]["source"].startswith("gerrit_instances")
    finally:
        p.stop()


def test_plan_rows_jira_only_when_url_and_token_both_present():
    p, _ = _patched_settings(
        notification_jira_url="https://jira.example.com",
        notification_jira_token="jt_xxxx",
        notification_jira_project="OMNI",
        jira_webhook_secret="jws",
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == "ga-legacy-jira-jira-example-com"
        assert r["platform"] == "jira"
        assert r["token"] == "jt_xxxx"
        assert r["project"] == "OMNI"
        assert r["webhook_secret"] == "jws"
        assert r["is_default"] is True
        assert r["source"] == "notification_jira"
    finally:
        p.stop()


def test_plan_rows_jira_skipped_when_token_missing():
    p, _ = _patched_settings(notification_jira_url="https://jira.example.com")
    try:
        from backend.legacy_credential_migration import _plan_rows
        assert _plan_rows() == []
    finally:
        p.stop()


def test_plan_rows_invalid_json_blob_is_warned_and_skipped(caplog):
    """Malformed ``github_token_map`` JSON is logged + treated as empty
    rather than crashing the boot."""
    import logging
    p, _ = _patched_settings(github_token_map="{not-json")
    try:
        from backend.legacy_credential_migration import _plan_rows
        with caplog.at_level(logging.WARNING):
            assert _plan_rows() == []
        assert any("failed to parse JSON map blob" in r.message
                   for r in caplog.records)
    finally:
        p.stop()


def test_plan_rows_all_sources_at_once_produces_expected_counts():
    """End-to-end planning across every legacy source — gives Phase 5-5
    a single point that exercises the precedence + dedup interactions
    between sources."""
    p, _ = _patched_settings(
        github_token="ghp_scalar",  # NOT migrated (map covers github.com)
        github_token_map=json.dumps({
            "github.com": "ghp_a",
            "github.enterprise.com": "ghp_b",
        }),
        gitlab_token="glpat_scalar",  # IS migrated (map empty for inferred host)
        gitlab_url="https://gitlab.internal.com",
        gitlab_token_map=json.dumps({"gitlab.client.com": "glpat_client"}),
        gerrit_instances=json.dumps([
            {"id": "g1", "ssh_host": "gerrit-a.com",
             "ssh_port": 29418, "project": "p/a", "webhook_secret": "wsa"},
        ]),
        gerrit_enabled=True,
        gerrit_ssh_host="gerrit-fallback.com",
        notification_jira_url="https://jira.example.com",
        notification_jira_token="jt",
    )
    try:
        from backend.legacy_credential_migration import _plan_rows
        rows = _plan_rows()
        # 2 github (from map; scalar dedup'd) + 2 gitlab (1 map + 1 scalar
        # for different host) + 2 gerrit (1 instance + 1 scalar fallback)
        # + 1 jira = 7
        assert len(rows) == 7
        platforms = sorted(r["platform"] for r in rows)
        assert platforms == [
            "gerrit", "gerrit", "github", "github", "gitlab", "gitlab", "jira",
        ]
    finally:
        p.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. migrate_legacy_credentials_once — kill-switch / no-pool / no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_migrate_kill_switch_short_circuits(monkeypatch):
    """``OMNISIGHT_CREDENTIAL_MIGRATE=skip`` must bypass even when
    legacy creds are present — operator escape hatch."""
    monkeypatch.setenv("OMNISIGHT_CREDENTIAL_MIGRATE", "skip")
    p, _ = _patched_settings(github_token="ghp_should_not_migrate")
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        out = await migrate_legacy_credentials_once()
        assert out["migrated"] == 0
        assert out["skipped_reason"] == "env:OMNISIGHT_CREDENTIAL_MIGRATE=skip"
        assert out["sources"] == []
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_migrate_no_pool_returns_skipped(monkeypatch):
    """SQLite dev mode: pool not initialised → migration is a no-op
    rather than crashing."""
    monkeypatch.delenv("OMNISIGHT_CREDENTIAL_MIGRATE", raising=False)

    def _no_pool():
        raise RuntimeError("pool not init")

    monkeypatch.setattr(
        "backend.legacy_credential_migration.get_pool", _no_pool, raising=False,
    )
    # Patch the dynamically-imported get_pool inside the function.
    import backend.db_pool
    monkeypatch.setattr(backend.db_pool, "get_pool", _no_pool)

    p, _ = _patched_settings(github_token="ghp_xx")
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        out = await migrate_legacy_credentials_once()
        assert out["migrated"] == 0
        assert out["skipped_reason"] == "no_pool"
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_migrate_no_legacy_credentials_skipped(monkeypatch):
    """Empty Settings → migration plan empty → return reason
    ``no_legacy_credentials``. No pool acquire attempted in this branch
    (verified by stubbing the pool to raise on ``acquire``)."""
    monkeypatch.delenv("OMNISIGHT_CREDENTIAL_MIGRATE", raising=False)

    class _StubPool:
        def acquire(self_inner):  # noqa: D401 — pretends to be pool
            raise AssertionError(
                "Pool acquire should not be called when there's nothing "
                "to migrate"
            )

    import backend.db_pool
    monkeypatch.setattr(backend.db_pool, "get_pool", lambda: _StubPool())

    p, _ = _patched_settings()  # all empty
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        out = await migrate_legacy_credentials_once()
        assert out["migrated"] == 0
        assert out["skipped_reason"] == "no_legacy_credentials"
    finally:
        p.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Live PG contract tests via pg_test_pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _live_db(pg_test_pool, monkeypatch):
    """Fresh ``git_accounts`` slate + ``t-default`` tenant seeded.

    Mirrors the shape of :func:`test_git_accounts_crud._ga_db` so the
    PG-live tests here run against the real schema + audit chain.
    Kill-switch env is unset so the migration runs through unless a
    test explicitly opts back into ``skip``.
    """
    monkeypatch.delenv("OMNISIGHT_CREDENTIAL_MIGRATE", raising=False)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "('t-default', 'Default', 'starter') "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
        )
    yield pg_test_pool
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
        )


@pytest.mark.asyncio
async def test_pg_migrate_writes_rows_with_encrypted_token(_live_db):
    """End-to-end: scalar github + scalar gitlab + scalar gerrit + jira
    all migrate into ``git_accounts`` with proper encryption + correct
    flags."""
    p, _ = _patched_settings(
        github_token="ghp_e2e_aaaa",
        gitlab_token="glpat_e2e_bbbb",
        gitlab_url="https://gitlab.e2e.com",
        gerrit_enabled=True,
        gerrit_ssh_host="gerrit.e2e.com",
        gerrit_ssh_port=29418,
        gerrit_project="p/e2e",
        notification_jira_url="https://jira.e2e.com",
        notification_jira_token="jt_e2e_cccc",
    )
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        out = await migrate_legacy_credentials_once()
        assert out["migrated"] == 4
        assert out["skipped_reason"] is None
        assert sorted(out["sources"]) == [
            "gerrit_scalar", "github_token", "gitlab_token", "notification_jira",
        ]
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, platform, instance_url, label, encrypted_token, "
            "is_default, ssh_host FROM git_accounts ORDER BY platform, id"
        )
    assert len(rows) == 4
    by_id = {r["id"]: r for r in rows}
    assert "ga-legacy-github-github-com" in by_id
    assert "ga-legacy-gitlab-gitlab-e2e-com" in by_id
    assert "ga-legacy-gerrit-gerrit-e2e-com" in by_id
    assert "ga-legacy-jira-jira-e2e-com" in by_id

    gh = by_id["ga-legacy-github-github-com"]
    assert gh["platform"] == "github"
    assert gh["instance_url"] == "https://github.com"
    assert gh["label"] == "github.com (legacy)"
    assert gh["is_default"] is True
    # Token is encrypted at rest.
    assert gh["encrypted_token"] != ""
    assert "ghp_e2e_aaaa" not in (gh["encrypted_token"] or "")
    # Decrypt round-trips back to the plaintext.
    from backend.secret_store import decrypt
    assert decrypt(gh["encrypted_token"]) == "ghp_e2e_aaaa"

    ger = by_id["ga-legacy-gerrit-gerrit-e2e-com"]
    assert ger["ssh_host"] == "gerrit.e2e.com"
    assert ger["is_default"] is True


@pytest.mark.asyncio
async def test_pg_migrate_audit_log_emitted_per_row(_live_db):
    """Each successful insert writes one ``credential_auto_migrate``
    audit row with ``actor=system/migration``."""
    p, _ = _patched_settings(
        github_token="ghp_audit_aaaa",
        notification_jira_url="https://jira.example.com",
        notification_jira_token="jt_audit_bbbb",
    )
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        out = await migrate_legacy_credentials_once()
        assert out["migrated"] == 2
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        audit_rows = await conn.fetch(
            "SELECT action, entity_kind, entity_id, actor "
            "FROM audit_log WHERE action = 'credential_auto_migrate' "
            "ORDER BY entity_id"
        )
    assert len(audit_rows) == 2
    actors = {r["actor"] for r in audit_rows}
    assert actors == {"system/migration"}
    entity_kinds = {r["entity_kind"] for r in audit_rows}
    assert entity_kinds == {"git_account"}
    entity_ids = {r["entity_id"] for r in audit_rows}
    assert entity_ids == {
        "ga-legacy-github-github-com",
        "ga-legacy-jira-jira-example-com",
    }


@pytest.mark.asyncio
async def test_pg_migrate_idempotent_second_run_no_op(_live_db):
    """Re-running the migration after a row was already inserted must
    not duplicate, even if Settings still carries the legacy creds.
    Skip reason is ``git_accounts_non_empty`` (not ``no_legacy_*``) so
    operators can grep the boot log to tell the two cases apart."""
    p, _ = _patched_settings(github_token="ghp_idem_aaaa")
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        first = await migrate_legacy_credentials_once()
        assert first["migrated"] == 1
        second = await migrate_legacy_credentials_once()
        assert second["migrated"] == 0
        assert second["skipped_reason"] == "git_accounts_non_empty"
        assert second["candidates"] == 1
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM git_accounts")
    assert n == 1


@pytest.mark.asyncio
async def test_pg_migrate_concurrent_workers_only_one_winner(_live_db):
    """Two simultaneous calls (worker race simulation) must collapse
    to a single inserted row per deterministic id — relies on
    ``INSERT ... ON CONFLICT (id) DO NOTHING``.

    Note: the second call observes the table-already-non-empty branch
    AND skips — that's fine; the deterministic-id guard is the SECOND
    line of defence (covering the rare case where two workers somehow
    both pass the empty-table check before either has committed). This
    test exercises the first line; the unit test below exercises the
    second by monkey-patching the empty-table check.
    """
    import asyncio
    p, _ = _patched_settings(github_token="ghp_race_aaaa")
    try:
        from backend.legacy_credential_migration import (
            migrate_legacy_credentials_once,
        )
        results = await asyncio.gather(
            migrate_legacy_credentials_once(),
            migrate_legacy_credentials_once(),
        )
    finally:
        p.stop()

    total = sum(r["migrated"] for r in results)
    # Either A migrates 1 and B sees the table non-empty (skip), OR
    # vice versa. Sum is exactly 1 — never 2.
    assert total == 1

    async with _live_db.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM git_accounts")
    assert n == 1


@pytest.mark.asyncio
async def test_pg_migrate_on_conflict_do_nothing_blocks_duplicate(
    _live_db, monkeypatch,
):
    """Direct test of the deterministic-id + ON CONFLICT defence:
    pre-insert a row with the same id, then run the migration with
    the empty-table guard mocked out — the migration must NOT
    duplicate the existing row.

    This is the second line of defence — even if two workers somehow
    race past the ``_table_has_any_row`` check (e.g. transaction
    isolation hides each other's pending inserts), the deterministic
    id collision blocks duplicates at write time."""
    # Pre-seed via the migration itself, then truncate audit_log so the
    # second pass can be observed independently. Then bypass the
    # idempotency check so we exercise the ON-CONFLICT path.
    p, _ = _patched_settings(github_token="ghp_seed_aaaa")
    try:
        from backend import legacy_credential_migration as lcm
        first = await lcm.migrate_legacy_credentials_once()
        assert first["migrated"] == 1
    finally:
        p.stop()
    async with _live_db.acquire() as conn:
        await conn.execute("TRUNCATE audit_log RESTART IDENTITY CASCADE")

    # Now bypass the ``_table_has_any_row`` check and re-run with the
    # SAME settings. The deterministic id must collide and DO NOTHING.
    monkeypatch.setattr(
        "backend.legacy_credential_migration._table_has_any_row",
        lambda conn: _async_false(),
    )
    p, _ = _patched_settings(github_token="ghp_seed_aaaa")
    try:
        from backend import legacy_credential_migration as lcm
        second = await lcm.migrate_legacy_credentials_once()
        # No new row inserted — ON CONFLICT silently dropped it.
        assert second["migrated"] == 0
        assert second["candidates"] == 1
    finally:
        p.stop()

    async with _live_db.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM git_accounts")
        audit_count = await conn.fetchval(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action = 'credential_auto_migrate'"
        )
    # Exactly one row, exactly zero new audit rows in the second run
    # (the loser silently skipped audit emit).
    assert n == 1
    assert audit_count == 0


async def _async_false() -> bool:
    """Helper: async coroutine that resolves to False (used to bypass
    the table-non-empty idempotency check in the test above)."""
    return False
