"""Phase 5-2 (#multi-account-forge) — credential registry refactor tests.

Locks the behaviour contracts landed by the Phase 5-2 refactor of
``backend/git_credentials.py``:

1. **Virtual-row shape parity** — :func:`_virtual_account_row` (shim
   path) and :func:`_row_to_dict` (pool path) produce dicts with the
   same key set so downstream callers cannot tell them apart.
2. **Deprecation warning** — :func:`_build_registry` emits a single
   ``logger.warning`` line per process on first invocation.
3. **Async fallback semantics** — :func:`get_credential_registry_async`
   falls back to the legacy shim when the pool is not initialised or
   ``git_accounts`` is empty for the current tenant.
4. **Tenant scope** — :func:`_resolve_tenant` honours the explicit
   ``tenant_id`` kwarg, then ``db_context.current_tenant_id()``,
   then ``t-default``.
5. **URL-pattern resolution** — :func:`pick_account_for_url` matches
   ``url_patterns`` entries via :mod:`fnmatch`, falls back to host
   exact match, then to :func:`pick_default`.
6. **Default resolution** — :func:`pick_default` prefers
   ``is_default=TRUE`` rows; falls back to first-of-platform.
7. **ID lookup** — :func:`pick_by_id` is tenant-scoped (does not
   leak rows from other tenants).

Module-global state audit (SOP Step 1, qualified answer #1)
───────────────────────────────────────────────────────────
Tests manipulate two module-globals in :mod:`backend.git_credentials`:

* ``_CREDENTIALS_CACHE`` / ``_LEGACY_WARN_EMITTED`` — reset between
  tests via :func:`clear_credential_cache` /
  :func:`_reset_deprecation_warn_for_tests` so each test starts from
  a clean state. No cross-test pollution possible.
* :data:`backend.db_context._tenant_var` — set/unset via
  ``backend.db_context.set_tenant_id`` inside each test; the
  contextvar's default is ``None``, so bleed-through between tests
  is bounded.

No real pool is created in these tests; the async path's behaviour
is exercised by asserting the ``RuntimeError`` fallback path (pool
not initialised) and, for one end-to-end happy-path test, by mocking
``db_pool.get_pool`` to return a stub.

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
No write path is changed by Phase 5-2. Tests exercise read paths
only; none of them rely on ``A write → B read`` serialisation
ordering that would become visible under pool concurrency.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures — isolate cache + warn flag per test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _fresh_credential_module_state():
    from backend import git_credentials as gc
    gc.clear_credential_cache()
    gc._reset_deprecation_warn_for_tests()
    yield
    gc.clear_credential_cache()
    gc._reset_deprecation_warn_for_tests()


@pytest.fixture
def _empty_settings_mock():
    """Patch ``backend.git_credentials.settings`` with all empty legacy
    fields. Each test overrides the fields it cares about."""
    with patch("backend.git_credentials.settings") as mock:
        mock.github_token = ""
        mock.gitlab_token = ""
        mock.gitlab_url = ""
        mock.git_ssh_key_path = ""
        mock.gerrit_enabled = False
        mock.gerrit_ssh_host = ""
        mock.gerrit_url = ""
        mock.gerrit_ssh_port = 29418
        mock.gerrit_project = ""
        mock.gerrit_webhook_secret = ""
        mock.git_credentials_file = ""
        mock.git_ssh_key_map = ""
        mock.github_token_map = ""
        mock.gitlab_token_map = ""
        mock.gerrit_instances = ""
        mock.github_webhook_secret = ""
        mock.gitlab_webhook_secret = ""
        # Phase 5-8 (#multi-account-forge): shim now reads JIRA scalars
        # too. Empty them here so the fixture stays an "empty settings"
        # baseline and tests explicitly opt-in to synthesising a
        # default-jira row via per-test overrides.
        mock.notification_jira_url = ""
        mock.notification_jira_token = ""
        mock.notification_jira_project = ""
        mock.jira_webhook_secret = ""
        yield mock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Virtual-row shape parity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_EXPECTED_ROW_KEYS = frozenset({
    "id",
    "tenant_id",
    "platform",
    "url",               # legacy compat alias for instance_url
    "instance_url",
    "label",
    "username",
    "token",             # legacy plaintext alias
    "ssh_key",           # legacy plaintext alias
    "ssh_host",
    "ssh_port",
    "project",
    "webhook_secret",    # legacy plaintext alias
    "encrypted_token",
    "encrypted_ssh_key",
    "encrypted_webhook_secret",
    "url_patterns",
    "auth_type",
    "is_default",
    "enabled",
    "metadata",
    "last_used_at",
    "created_at",
    "updated_at",
    "version",
})


def test_virtual_account_row_has_canonical_key_set():
    """Shim rows must expose every key real ``git_accounts`` rows expose."""
    from backend.git_credentials import _virtual_account_row
    row = _virtual_account_row(
        entry_id="test-1",
        platform="github",
        instance_url="https://github.com",
        token="ghp_plain",
    )
    assert set(row.keys()) == _EXPECTED_ROW_KEYS
    # Plaintext aliases carry the value; canonical encrypted columns
    # stay empty because the shim has no Fernet ciphertext.
    assert row["token"] == "ghp_plain"
    assert row["encrypted_token"] == ""
    assert row["platform"] == "github"
    assert row["enabled"] is True
    assert row["is_default"] is False
    assert row["url_patterns"] == []
    assert row["metadata"] == {}


def test_virtual_row_is_independent_copies():
    """``url_patterns`` / ``metadata`` defaults must not be shared
    between returned rows — aliasing defaults across virtual rows
    would be a bug waiting to happen once row 5-3 starts mutating them."""
    from backend.git_credentials import _virtual_account_row
    a = _virtual_account_row(entry_id="a", platform="github")
    b = _virtual_account_row(entry_id="b", platform="gitlab")
    a["url_patterns"].append("github.com/*")
    a["metadata"]["flag"] = True
    assert b["url_patterns"] == []
    assert b["metadata"] == {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Legacy shim + deprecation warn
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_build_registry_emits_deprecation_warn_once(
    caplog, _empty_settings_mock,
):
    """The shim must emit exactly one deprecation warning per process
    (reset by the autouse fixture in this test module)."""
    from backend.git_credentials import _build_registry
    _empty_settings_mock.github_token = "ghp_warn_probe"
    _empty_settings_mock.git_ssh_key_path = "~/.ssh/id_x"
    with caplog.at_level(logging.WARNING, logger="backend.git_credentials"):
        _build_registry()
        _build_registry()
        _build_registry()
    warn_lines = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "Phase 5-2 backward-compat shim" in rec.getMessage()
    ]
    assert len(warn_lines) == 1, (
        "Expected exactly one deprecation warning per process, got "
        f"{len(warn_lines)}: {[r.getMessage() for r in warn_lines]}"
    )


def test_build_registry_virtual_rows_use_canonical_shape(
    _empty_settings_mock,
):
    """Scalar-fallback legacy rows must emerge as full virtual
    ``git_accounts`` rows, not the old abbreviated dict shape."""
    from backend.git_credentials import _build_registry
    _empty_settings_mock.github_token = "ghp_scalar"
    _empty_settings_mock.git_ssh_key_path = "~/.ssh/id_scalar"
    rows = _build_registry()
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == _EXPECTED_ROW_KEYS
    assert row["id"] == "default-github"
    assert row["platform"] == "github"
    assert row["token"] == "ghp_scalar"
    # Scalar-fallback rows are auto-promoted to is_default=True so a
    # single-account legacy deploy resolves via pick_default() without
    # operator intervention.
    assert row["is_default"] is True
    assert row["label"].endswith("(legacy scalar)")


def test_sync_get_credential_registry_preserves_legacy_callers(
    _empty_settings_mock,
):
    """Legacy sync callers keep working — ``token`` plaintext + ``url``
    alias + ``platform`` + ``id`` all present."""
    import json
    from backend.git_credentials import (
        clear_credential_cache, get_credential_registry,
    )
    clear_credential_cache()
    _empty_settings_mock.github_token_map = json.dumps({"github.enterprise.com": "ghp_enterprise"})
    reg = get_credential_registry()
    hit = [r for r in reg if r["id"] == "github-github-enterprise-com"]
    assert hit, f"expected legacy-map row not present in registry: {reg}"
    assert hit[0]["token"] == "ghp_enterprise"
    assert hit[0]["url"] == "https://github.enterprise.com"
    assert hit[0]["instance_url"] == "https://github.enterprise.com"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tenant scope resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_resolve_tenant_explicit_beats_contextvar():
    from backend.git_credentials import _resolve_tenant
    from backend import db_context
    db_context.set_tenant_id("t-context")
    try:
        assert _resolve_tenant("t-explicit") == "t-explicit"
    finally:
        db_context.set_tenant_id(None)


def test_resolve_tenant_contextvar_beats_default():
    from backend.git_credentials import _resolve_tenant
    from backend import db_context
    db_context.set_tenant_id("t-context")
    try:
        assert _resolve_tenant(None) == "t-context"
    finally:
        db_context.set_tenant_id(None)


def test_resolve_tenant_falls_back_to_t_default():
    from backend.git_credentials import _resolve_tenant
    from backend import db_context
    db_context.set_tenant_id(None)
    assert _resolve_tenant(None) == "t-default"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async path — shim fallback when pool not initialised
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_async_registry_falls_back_to_shim_when_no_pool(
    _empty_settings_mock,
):
    """With no pool initialised, the async registry read must degrade
    gracefully to the legacy shim (returning empty list here, but the
    shape contract is the caller mustn't see a RuntimeError)."""
    from backend.git_credentials import get_credential_registry_async
    from backend import db_pool
    # Ensure no pool is active.
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token = "ghp_async_fallback"
    _empty_settings_mock.git_ssh_key_path = ""
    rows = await get_credential_registry_async()
    assert isinstance(rows, list)
    assert any(r["token"] == "ghp_async_fallback" for r in rows), (
        "Async fallback path should surface the legacy scalar github_token "
        f"through the shim. Got rows: {[r.get('id') for r in rows]}"
    )


@pytest.mark.asyncio
async def test_pick_account_for_url_empty_url_returns_none():
    from backend.git_credentials import pick_account_for_url
    assert await pick_account_for_url("") is None
    assert await pick_account_for_url(None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_pick_account_for_url_host_match_via_shim(_empty_settings_mock):
    """Without a pool, the URL resolver must still work against shim rows."""
    import json
    from backend.git_credentials import pick_account_for_url
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token_map = json.dumps({
        "github.enterprise.internal": "ghp_ent",
    })
    entry = await pick_account_for_url(
        "https://github.enterprise.internal/acme/app.git"
    )
    assert entry is not None
    assert entry["token"] == "ghp_ent"
    assert entry["platform"] == "github"


@pytest.mark.asyncio
async def test_pick_account_for_url_ssh_form(_empty_settings_mock):
    """``git@host:org/repo.git`` must resolve to the same account as
    ``https://host/org/repo.git``."""
    import json
    from backend.git_credentials import pick_account_for_url
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token_map = json.dumps({
        "github.internal": "ghp_ssh",
    })
    entry = await pick_account_for_url("git@github.internal:org/repo.git")
    assert entry is not None
    assert entry["token"] == "ghp_ssh"


@pytest.mark.asyncio
async def test_pick_account_for_url_no_match_returns_none(_empty_settings_mock):
    from backend.git_credentials import pick_account_for_url
    from backend import db_pool
    db_pool._reset_for_tests()
    # No legacy creds configured at all.
    entry = await pick_account_for_url("https://totally-unrelated.host/x.git")
    assert entry is None


@pytest.mark.asyncio
async def test_pick_default_via_shim(_empty_settings_mock):
    """``pick_default`` must surface the scalar-legacy is_default row."""
    from backend.git_credentials import pick_default
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token = "ghp_scalar_default"
    _empty_settings_mock.git_ssh_key_path = "~/.ssh/id_default"
    entry = await pick_default("github")
    assert entry is not None
    assert entry["token"] == "ghp_scalar_default"
    assert entry["is_default"] is True


@pytest.mark.asyncio
async def test_pick_default_falls_back_to_first_of_platform(
    _empty_settings_mock,
):
    """When no row is marked is_default, picker returns the first
    row of that platform — so single-account legacy-map deploys
    still have a resolvable default without an operator flag."""
    import json
    from backend.git_credentials import pick_default
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token_map = json.dumps({
        "github.com": "ghp_first_of_platform",
    })
    entry = await pick_default("github")
    assert entry is not None
    assert entry["token"] == "ghp_first_of_platform"


@pytest.mark.asyncio
async def test_pick_default_unknown_platform_returns_none(
    _empty_settings_mock,
):
    from backend.git_credentials import pick_default
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token = "ghp_x"
    entry = await pick_default("gitea")  # not in the CHECK enum
    assert entry is None


@pytest.mark.asyncio
async def test_pick_by_id_without_pool_searches_shim(
    _empty_settings_mock,
):
    from backend.git_credentials import pick_by_id
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token = "ghp_id_probe"
    entry = await pick_by_id("default-github")
    assert entry is not None
    assert entry["token"] == "ghp_id_probe"


@pytest.mark.asyncio
async def test_pick_by_id_empty_id_returns_none():
    from backend.git_credentials import pick_by_id
    assert await pick_by_id("") is None
    assert await pick_by_id(None) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_pick_by_id_miss_returns_none(_empty_settings_mock):
    from backend.git_credentials import pick_by_id
    from backend import db_pool
    db_pool._reset_for_tests()
    _empty_settings_mock.github_token = "ghp_x"
    entry = await pick_by_id("nonexistent-id")
    assert entry is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async path — happy path with stubbed pool + git_accounts rows
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeRow(dict):
    """Minimal ``asyncpg.Record``-compatible stand-in for unit tests."""
    pass


class _FakeConn:
    def __init__(self, rows: list[_FakeRow]):
        self._rows = rows
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRow]:
        self.queries.append((sql, args))
        # Filter on tenant_id = $1; ignore other filters for the test.
        tid = args[0]
        return [r for r in self._rows if r["tenant_id"] == tid]

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        self.queries.append((sql, args))
        tid = args[0]
        target_id = args[1] if len(args) > 1 else None
        for r in self._rows:
            if r["tenant_id"] == tid and (target_id is None or r["id"] == target_id):
                return r
        return None


class _FakePool:
    def __init__(self, rows: list[_FakeRow]):
        self._rows = rows
        self.last_conn = _FakeConn(rows)

    def acquire(self):
        conn = _FakeConn(self._rows)
        self.last_conn = conn

        class _CM:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


def _encrypted(plain: str) -> str:
    """Helper — encrypt a plaintext secret with the live secret_store
    so :func:`_row_to_dict` can decrypt it back during the test."""
    from backend.secret_store import encrypt
    return encrypt(plain)


def _make_row(
    *,
    account_id: str,
    platform: str,
    instance_url: str,
    token_plain: str = "",
    ssh_key_plain: str = "",
    webhook_plain: str = "",
    url_patterns: list[str] | None = None,
    is_default: bool = False,
    tenant_id: str = "t-default",
    ssh_host: str = "",
    ssh_port: int = 0,
    project: str = "",
) -> _FakeRow:
    import json
    return _FakeRow({
        "id": account_id,
        "tenant_id": tenant_id,
        "platform": platform,
        "instance_url": instance_url,
        "label": "",
        "username": "",
        "encrypted_token": _encrypted(token_plain) if token_plain else "",
        "encrypted_ssh_key": _encrypted(ssh_key_plain) if ssh_key_plain else "",
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "project": project,
        "encrypted_webhook_secret": _encrypted(webhook_plain) if webhook_plain else "",
        "url_patterns": json.dumps(url_patterns or []),
        "auth_type": "pat",
        "is_default": is_default,
        "enabled": True,
        "metadata": "{}",
        "last_used_at": None,
        "created_at": 1.0,
        "updated_at": 1.0,
        "version": 0,
    })


@pytest.mark.asyncio
async def test_pool_registry_decrypts_and_shapes_rows(monkeypatch):
    """With a stub pool returning git_accounts rows, the async registry
    must decrypt each row's ciphertext and expose plaintext + the full
    canonical key set."""
    rows = [
        _make_row(
            account_id="ga-acme",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_acme",
            is_default=True,
        ),
    ]
    fake_pool = _FakePool(rows)

    from backend import git_credentials as gc
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    out = await gc.get_credential_registry_async(tenant_id="t-default")
    assert len(out) == 1
    row = out[0]
    assert set(row.keys()) == _EXPECTED_ROW_KEYS
    assert row["token"] == "ghp_acme"                # decrypted
    assert row["encrypted_token"] != ""              # ciphertext preserved
    assert row["is_default"] is True
    assert row["platform"] == "github"


@pytest.mark.asyncio
async def test_pool_pick_account_url_pattern_match(monkeypatch):
    """URL-pattern matching wins over platform-default fallback."""
    rows = [
        _make_row(
            account_id="ga-personal",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_personal",
            is_default=True,
        ),
        _make_row(
            account_id="ga-corp",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_corp",
            url_patterns=["github.com/acme-corp/*"],
            is_default=False,
        ),
    ]
    fake_pool = _FakePool(rows)

    from backend import git_credentials as gc
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    entry = await gc.pick_account_for_url(
        "https://github.com/acme-corp/app.git",
        tenant_id="t-default",
    )
    assert entry is not None
    assert entry["id"] == "ga-corp"
    assert entry["token"] == "ghp_corp"


@pytest.mark.asyncio
async def test_pool_pick_account_platform_default_fallback(monkeypatch):
    """A URL with no pattern match falls through to the platform default."""
    rows = [
        _make_row(
            account_id="ga-personal",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_personal",
            is_default=True,
        ),
        _make_row(
            account_id="ga-corp",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_corp",
            url_patterns=["github.com/acme-corp/*"],
            is_default=False,
        ),
    ]
    fake_pool = _FakePool(rows)

    from backend import git_credentials as gc
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    entry = await gc.pick_account_for_url(
        "https://github.com/someone-else/repo.git",
        tenant_id="t-default",
    )
    assert entry is not None
    # No pattern match → fallback to is_default=TRUE row.
    assert entry["id"] == "ga-personal"
    assert entry["token"] == "ghp_personal"


@pytest.mark.asyncio
async def test_pool_pick_by_id_tenant_scoped(monkeypatch):
    """pick_by_id must not leak rows across tenants."""
    rows = [
        _make_row(
            account_id="ga-shared",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_a",
            tenant_id="t-a",
        ),
        _make_row(
            account_id="ga-shared",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_b",
            tenant_id="t-b",
        ),
    ]
    fake_pool = _FakePool(rows)

    from backend import git_credentials as gc
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    entry_a = await gc.pick_by_id("ga-shared", tenant_id="t-a")
    entry_b = await gc.pick_by_id("ga-shared", tenant_id="t-b")
    assert entry_a is not None and entry_b is not None
    assert entry_a["token"] == "ghp_a"
    assert entry_b["token"] == "ghp_b"
    # Nonexistent id for either tenant returns None.
    assert await gc.pick_by_id("no-such-id", tenant_id="t-a") is None


@pytest.mark.asyncio
async def test_pool_empty_tenant_falls_back_to_shim(
    monkeypatch, _empty_settings_mock,
):
    """A tenant whose ``git_accounts`` rows are all for a different
    tenant must still get the legacy shim output (not an empty list).
    This is the ramp behaviour for Phase 5 before row 5-5 auto-
    migration moves legacy ``.env`` into ``git_accounts``."""
    rows = [
        _make_row(
            account_id="ga-other",
            platform="github",
            instance_url="https://github.com",
            token_plain="ghp_for_other_tenant",
            tenant_id="t-someone-else",
        ),
    ]
    fake_pool = _FakePool(rows)

    from backend import git_credentials as gc
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    _empty_settings_mock.github_token = "ghp_legacy_fallback"
    _empty_settings_mock.git_ssh_key_path = ""
    out = await gc.get_credential_registry_async(tenant_id="t-default")
    assert any(r["token"] == "ghp_legacy_fallback" for r in out), (
        "Expected shim fallback when git_accounts has no rows for this "
        f"tenant; got: {[r.get('id') for r in out]}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decrypt failure handling (defensive)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_row_to_dict_handles_decrypt_failure_gracefully(monkeypatch):
    """A corrupted ciphertext column must not raise — it returns an
    empty plaintext and logs a warning, so a single bad row can't take
    down the whole registry read."""
    rows = [
        _FakeRow({
            "id": "ga-bad",
            "tenant_id": "t-default",
            "platform": "github",
            "instance_url": "https://github.com",
            "label": "",
            "username": "",
            "encrypted_token": "not-valid-fernet",
            "encrypted_ssh_key": "",
            "ssh_host": "",
            "ssh_port": 0,
            "project": "",
            "encrypted_webhook_secret": "",
            "url_patterns": "[]",
            "auth_type": "pat",
            "is_default": False,
            "enabled": True,
            "metadata": "{}",
            "last_used_at": None,
            "created_at": 1.0,
            "updated_at": 1.0,
            "version": 0,
        }),
    ]
    fake_pool = _FakePool(rows)

    from backend import git_credentials as gc
    monkeypatch.setattr("backend.db_pool.get_pool", lambda: fake_pool)

    out = await gc.get_credential_registry_async(tenant_id="t-default")
    assert len(out) == 1
    assert out[0]["token"] == ""
    assert out[0]["encrypted_token"] == "not-valid-fernet"
