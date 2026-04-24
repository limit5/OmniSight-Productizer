"""Phase 5-8 (#multi-account-forge) — JIRA call-site sweep tests.

Locks the behaviour landed by row 5-8 of Phase 5: every async caller
that previously reached into ``settings.notification_jira_token`` /
``settings.notification_jira_url`` / ``settings.jira_webhook_secret``
directly now routes through
:func:`backend.git_credentials.pick_account_for_url` /
:func:`backend.git_credentials.pick_default` /
:func:`backend.git_credentials.get_webhook_secret_for_host_async` so
multi-account / multi-tenant deployments target the right JIRA
credential instead of the process-global ``Settings`` singleton.

What we lock here:

1. :func:`backend.issue_tracker._sync_jira` and ``_comment_jira`` —
   status transition + comment paths resolve via the registry, respect
   per-account ``instance_url`` for multi-instance JIRA deploys, and
   platform-post-filter against catch-all url_patterns on non-jira
   rows.
2. :func:`backend.git_credentials.get_webhook_secret_for_host_async`
   grew a ``jira`` branch in its scalar fallback chain so
   ``jira_webhook`` can call it uniformly.
3. :func:`backend.git_credentials._build_registry` (legacy shim) now
   synthesises a ``default-jira`` virtual row from
   ``notification_jira_*`` so ``pick_default('jira')`` works in shim
   mode (SQLite dev / pool-not-up) without each caller carrying its
   own scalar fallback.
4. ``backend/routers/webhooks.py::jira_webhook`` HMAC verify reads the
   secret through the async resolver — per-tenant isolation means a
   tenant-A JIRA secret in ``tenant_id='t-A'`` is not visible to the
   default-tenant webhook lookup.

Module-global / read-after-write audit (SOP Step 1)
───────────────────────────────────────────────────
No new module-globals introduced by row 5-8. The tests below stub
:func:`pick_default` / :func:`pick_account_for_url` /
:func:`get_webhook_secret_for_host_async` at the resolver module
(``backend.git_credentials``) so inline-import call sites in
``issue_tracker.py`` / ``routers/webhooks.py`` see the patched version.
There is no cross-worker state at play — each worker reaches the same
resolver singleton (the async pool or the legacy shim) and the
resolver itself is already audited by Phase 5-2 / 5-3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared canned JIRA account row (matches _row_to_dict shape).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _jira_account(
    *,
    id: str = "ga-corp-jira",
    instance_url: str = "https://jira.corp.com",
    token: str = "",
    webhook_secret: str = "",
    project: str = "",
    url_patterns: list[str] | None = None,
    is_default: bool = False,
    tenant_id: str = "t-default",
) -> dict:
    """A canned ``git_accounts(platform='jira')`` row dict matching
    :func:`backend.git_credentials._row_to_dict` shape."""
    return {
        "id": id,
        "tenant_id": tenant_id,
        "platform": "jira",
        "url": instance_url,
        "instance_url": instance_url,
        "label": f"{instance_url} (test)",
        "username": "",
        "token": token,
        "ssh_key": "",
        "ssh_host": "",
        "ssh_port": 0,
        "project": project,
        "webhook_secret": webhook_secret,
        "encrypted_token": "",
        "encrypted_ssh_key": "",
        "encrypted_webhook_secret": "",
        "url_patterns": list(url_patterns) if url_patterns else [],
        "auth_type": "pat",
        "is_default": is_default,
        "enabled": True,
        "metadata": {},
        "last_used_at": None,
        "created_at": 0.0,
        "updated_at": 0.0,
        "version": 0,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. issue_tracker.py — _sync_jira / _comment_jira use resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSyncJiraUsesResolver:
    """``_sync_jira`` pulls the token + base URL from
    :func:`pick_account_for_url` → :func:`pick_default` — not from
    ``settings.notification_jira_token``.
    """

    @pytest.mark.asyncio
    async def test_sync_jira_uses_resolver_token(self):
        """Happy path: resolver returns a JIRA account, token flows to
        both the list-transitions GET and the POST transition curl."""
        from backend.issue_tracker import _sync_jira

        account = _jira_account(
            token="jira_pat_from_resolver",
            instance_url="https://jira.corp.com",
            is_default=True,
        )

        async def _url_picker(url, **_kw):
            return None  # force fallback to pick_default

        async def _default_picker(platform, **_kw):
            assert platform == "jira"
            return account

        # Two proc calls: list-transitions (GET) then POST transition.
        def _seq_proc(*_a, **_kw):
            m = AsyncMock()
            # First call: list transitions → returns one matching "Done".
            if not _seq_proc.calls:
                m.communicate = AsyncMock(
                    return_value=(
                        b'{"transitions":[{"id":"31","name":"Done"}]}',
                        b"",
                    ),
                )
            else:
                # Transition POST → 204 (success).
                m.communicate = AsyncMock(return_value=(b"{}\n204", b""))
            m.returncode = 0
            _seq_proc.calls += 1
            return m
        _seq_proc.calls = 0

        with patch(
            "backend.git_credentials.pick_account_for_url",
            side_effect=_url_picker,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_default_picker,
        ), patch(
            "asyncio.create_subprocess_exec",
            side_effect=_seq_proc,
        ) as create_proc:
            result = await _sync_jira(
                "https://jira.corp.com/browse/PROJ-42",
                "completed",
                "",
            )

        assert result["status"] == "ok"
        assert result["issue"] == "PROJ-42"
        # Both curl calls must carry the resolver token and hit the
        # resolved base URL.
        for call in create_proc.call_args_list:
            args = call.args
            combined = " ".join(str(a) for a in args)
            assert "jira_pat_from_resolver" in combined
            # The URL argument is among args — assert the resolved host.
            assert "jira.corp.com" in combined

    @pytest.mark.asyncio
    async def test_sync_jira_returns_error_without_token(self):
        """Resolver yields no account → skipped with a clear error.
        This replaces the pre-5-8 ``if not settings.notification_jira_token``
        branch."""
        from backend.issue_tracker import _sync_jira

        async def _url_picker(url, **_kw):
            return None

        async def _default_picker(platform, **_kw):
            return None  # no token configured

        with patch(
            "backend.git_credentials.pick_account_for_url",
            side_effect=_url_picker,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_default_picker,
        ):
            result = await _sync_jira(
                "https://jira.corp.com/browse/PROJ-1", "completed", "",
            )
        assert result["status"] == "error"
        assert "Jira token" in result["message"]

    @pytest.mark.asyncio
    async def test_sync_jira_prefers_resolved_instance_url(self):
        """Resolver's ``instance_url`` wins over the URL's inferred host.
        Multi-instance deployments need this so a PROJ-1 on the corp
        JIRA routes through the corp instance's API base, not
        ``https://jira.other.com`` just because the issue URL happened
        to be typed with that host in a comment."""
        from backend.issue_tracker import _sync_jira

        # Operator-configured instance_url differs from the issue URL's
        # host — the resolver is the source of truth.
        account = _jira_account(
            token="corp_pat",
            instance_url="https://jira-v2.corp.com",
            is_default=True,
        )

        async def _url_picker(url, **_kw):
            return None

        async def _default_picker(platform, **_kw):
            return account

        def _seq_proc(*_a, **_kw):
            m = AsyncMock()
            if not _seq_proc.calls:
                m.communicate = AsyncMock(
                    return_value=(
                        b'{"transitions":[{"id":"11","name":"Done"}]}',
                        b"",
                    ),
                )
            else:
                m.communicate = AsyncMock(return_value=(b"{}\n200", b""))
            m.returncode = 0
            _seq_proc.calls += 1
            return m
        _seq_proc.calls = 0

        with patch(
            "backend.git_credentials.pick_account_for_url",
            side_effect=_url_picker,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_default_picker,
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=_seq_proc,
        ) as create_proc:
            result = await _sync_jira(
                "https://jira-old.corp.com/browse/PROJ-42",
                "completed",
                "",
            )

        assert result["status"] == "ok"
        # Both calls must hit jira-v2.corp.com (the resolver URL), not
        # jira-old.corp.com (the issue URL host).
        for call in create_proc.call_args_list:
            combined = " ".join(str(a) for a in call.args)
            assert "jira-v2.corp.com" in combined
            assert "jira-old.corp.com" not in combined

    @pytest.mark.asyncio
    async def test_sync_jira_platform_post_filter(self):
        """If ``pick_account_for_url`` returns a non-jira row (because a
        catch-all url_pattern on a GitHub account happened to match a
        JIRA URL), the sync call MUST fall through to
        ``pick_default('jira')`` rather than use the GitHub token.

        Regression guard against accidental cross-platform credential
        leaks — the resolver's step 1-3 are registry-wide, not platform-
        filtered, so the caller has to post-filter.
        """
        from backend.issue_tracker import _sync_jira

        bogus_github = {
            "platform": "github",
            "token": "ghp_WRONG_should_never_reach_jira",
            "instance_url": "https://github.com",
        }
        real_jira = _jira_account(
            token="real_jira_token",
            instance_url="https://jira.corp.com",
            is_default=True,
        )

        async def _url_picker(url, **_kw):
            return bogus_github  # simulated misconfiguration

        async def _default_picker(platform, **_kw):
            assert platform == "jira"  # proving we fell through
            return real_jira

        def _seq_proc(*_a, **_kw):
            m = AsyncMock()
            if not _seq_proc.calls:
                m.communicate = AsyncMock(
                    return_value=(
                        b'{"transitions":[{"id":"31","name":"Done"}]}',
                        b"",
                    ),
                )
            else:
                m.communicate = AsyncMock(return_value=(b"{}\n200", b""))
            m.returncode = 0
            _seq_proc.calls += 1
            return m
        _seq_proc.calls = 0

        with patch(
            "backend.git_credentials.pick_account_for_url",
            side_effect=_url_picker,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_default_picker,
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=_seq_proc,
        ) as create_proc:
            result = await _sync_jira(
                "https://jira.corp.com/browse/PROJ-1", "completed", "",
            )

        assert result["status"] == "ok"
        for call in create_proc.call_args_list:
            combined = " ".join(str(a) for a in call.args)
            assert "real_jira_token" in combined
            assert "ghp_WRONG_should_never_reach_jira" not in combined


class TestCommentJiraUsesResolver:
    """``_comment_jira`` mirrors the resolver pattern so rotating a
    JIRA PAT via Phase-5-4 CRUD takes effect on the next comment call
    without restarting the backend."""

    @pytest.mark.asyncio
    async def test_comment_jira_uses_resolver(self):
        from backend.issue_tracker import _comment_jira

        async def _url_picker(url, **_kw):
            return None

        async def _default_picker(platform, **_kw):
            return _jira_account(
                token="comment_token_from_resolver",
                instance_url="https://jira.corp.com",
                is_default=True,
            )

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch(
            "backend.git_credentials.pick_account_for_url",
            side_effect=_url_picker,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_default_picker,
        ), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc,
        ) as create_proc:
            result = await _comment_jira(
                "https://jira.corp.com/browse/PROJ-5",
                "nice work",
            )

        assert result["status"] == "ok"
        combined = " ".join(str(a) for a in create_proc.call_args.args)
        assert "comment_token_from_resolver" in combined
        assert "jira.corp.com" in combined

    @pytest.mark.asyncio
    async def test_comment_jira_returns_error_without_token(self):
        from backend.issue_tracker import _comment_jira

        async def _url_picker(url, **_kw):
            return None

        async def _default_picker(platform, **_kw):
            return None

        with patch(
            "backend.git_credentials.pick_account_for_url",
            side_effect=_url_picker,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_default_picker,
        ):
            result = await _comment_jira(
                "https://jira.corp.com/browse/PROJ-5", "body",
            )
        assert result["status"] == "error"
        assert "Jira token" in result["message"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. get_webhook_secret_for_host_async — JIRA branch + tenant isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWebhookSecretAsyncJira:
    """Phase 5-8 extended ``get_webhook_secret_for_host_async`` with a
    JIRA branch so ``jira_webhook`` doesn't need its own scalar read.
    The per-tenant scope is what prevents cross-tenant credential
    leak — each tenant's secret is in its own ``git_accounts`` row,
    filtered by ``WHERE tenant_id = $N`` in the pool query."""

    @pytest.mark.asyncio
    async def test_jira_platform_default_match(self):
        """Empty host + platform=jira → resolver goes straight to
        ``pick_default('jira')`` which returns the operator-marked or
        auto-migrated JIRA row's secret."""
        from backend.git_credentials import get_webhook_secret_for_host_async

        async def _registry(*_a, **_kw):
            return []  # skip per-host loop

        async def _pick_default(platform, **_kw):
            assert platform == "jira"
            return _jira_account(
                webhook_secret="jira-platform-default-secret",
                is_default=True,
            )

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_pick_default,
        ):
            secret = await get_webhook_secret_for_host_async("", "jira")
        assert secret == "jira-platform-default-secret"

    @pytest.mark.asyncio
    async def test_jira_scalar_fallback(self):
        """Empty registry + empty default → falls back to
        ``settings.jira_webhook_secret``. Closes the single-instance
        deployment path where the operator only has the legacy scalar
        configured."""
        from backend.git_credentials import get_webhook_secret_for_host_async

        async def _registry(*_a, **_kw):
            return []

        async def _pick_default(*_a, **_kw):
            return None

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_pick_default,
        ), patch("backend.git_credentials.settings") as mock_settings:
            mock_settings.gerrit_webhook_secret = ""
            mock_settings.github_webhook_secret = ""
            mock_settings.gitlab_webhook_secret = ""
            mock_settings.jira_webhook_secret = "legacy-scalar-secret"
            secret = await get_webhook_secret_for_host_async("", "jira")
        assert secret == "legacy-scalar-secret"

    @pytest.mark.asyncio
    async def test_jira_tenant_isolation_via_registry_scope(self):
        """Tenant-A's JIRA row is invisible to a tenant-B webhook
        lookup. This is the core multi-tenant isolation guarantee:
        registry reads are ``WHERE tenant_id = $1`` scoped, so asking
        for tenant B's registry returns tenant B's rows only — even
        if tenant A has a valid JIRA secret configured.
        """
        from backend.git_credentials import get_webhook_secret_for_host_async

        tenant_a_account = _jira_account(
            id="ga-tenant-a-jira",
            tenant_id="t-A",
            webhook_secret="secret-for-tenant-A",
            is_default=True,
        )
        tenant_b_account = _jira_account(
            id="ga-tenant-b-jira",
            tenant_id="t-B",
            webhook_secret="secret-for-tenant-B",
            is_default=True,
        )

        # Simulate the tenant-scoped registry read: only the rows whose
        # ``tenant_id`` matches the kwarg/contextvar are returned.
        async def _registry(tenant_id=None, **_kw):
            if tenant_id == "t-A":
                return [tenant_a_account]
            if tenant_id == "t-B":
                return [tenant_b_account]
            return []

        async def _pick_default(platform, tenant_id=None, **_kw):
            if tenant_id == "t-A":
                return tenant_a_account
            if tenant_id == "t-B":
                return tenant_b_account
            return None

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_pick_default,
        ):
            a_secret = await get_webhook_secret_for_host_async(
                "", "jira", tenant_id="t-A",
            )
            b_secret = await get_webhook_secret_for_host_async(
                "", "jira", tenant_id="t-B",
            )

        assert a_secret == "secret-for-tenant-A"
        assert b_secret == "secret-for-tenant-B"
        # The critical invariant: tenant B's lookup must NEVER return
        # tenant A's secret (and vice versa). If the resolver forgot
        # the ``WHERE tenant_id = $1`` filter this would leak.
        assert a_secret != b_secret
        assert "tenant-A" not in b_secret
        assert "tenant-B" not in a_secret

    @pytest.mark.asyncio
    async def test_jira_per_host_match_filters_by_platform(self):
        """A gerrit row and a jira row both on ``self.corp.com`` — the
        jira webhook lookup must return the jira row's secret, not
        accidentally leak the gerrit secret via exact-host match."""
        from backend.git_credentials import get_webhook_secret_for_host_async

        # Both rows share the same URL host, different platforms.
        gerrit_row = dict(_jira_account(
            instance_url="https://self.corp.com",
        ))
        gerrit_row["platform"] = "gerrit"
        gerrit_row["id"] = "ga-gerrit"
        gerrit_row["webhook_secret"] = "gerrit-secret-do-not-use"
        gerrit_row["ssh_host"] = "self.corp.com"

        jira_row = _jira_account(
            id="ga-jira",
            instance_url="https://self.corp.com",
            webhook_secret="jira-secret-correct",
        )

        async def _registry(*_a, **_kw):
            return [gerrit_row, jira_row]

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ):
            secret = await get_webhook_secret_for_host_async(
                "self.corp.com", "jira",
            )
        assert secret == "jira-secret-correct"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. routers/webhooks.py::jira_webhook — verify uses async resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestJiraWebhookSecretResolution:
    """The JIRA webhook bearer check now reads the secret through
    :func:`get_webhook_secret_for_host_async` so operator rotations via
    Phase-5-4 CRUD take effect without a backend restart, AND tenant A's
    secret is not visible to tenant B's lookup path.

    These tests use FastAPI ``TestClient`` so the full request →
    resolver → ``compare_digest`` → response flow is exercised.
    """

    @pytest.fixture
    def app_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.routers.webhooks import router

        app = FastAPI()
        app.include_router(router)
        yield TestClient(app)

    def test_webhook_authenticates_with_registry_secret(self, app_client):
        """The bearer token matches the registry-resolved secret →
        request is accepted. The scalar ``settings.jira_webhook_secret``
        is empty, proving the resolver path is what's gating auth."""
        registry_secret = "registry-jira-secret-abc"

        async def _resolver(host, platform, **_kw):
            assert platform == "jira"
            return registry_secret

        with patch(
            "backend.git_credentials.get_webhook_secret_for_host_async",
            side_effect=_resolver,
        ), patch(
            "backend.routers.webhooks.settings",
        ) as mock_settings, patch(
            "backend.routers.integration._overlay_runtime_settings",
            return_value=None,
        ):
            mock_settings.jira_webhook_secret = ""  # force resolver path
            r = app_client.post(
                "/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
                headers={"Authorization": f"Bearer {registry_secret}"},
            )

        # 200 (no matching internal task → "ok" / "no_status_change").
        # The critical contract is NOT 401 and NOT 503.
        assert r.status_code == 200, r.text

    def test_webhook_rejects_wrong_secret(self, app_client):
        """Bearer token does not match the resolver's secret → 401."""
        async def _resolver(host, platform, **_kw):
            return "correct-secret"

        with patch(
            "backend.git_credentials.get_webhook_secret_for_host_async",
            side_effect=_resolver,
        ), patch(
            "backend.routers.webhooks.settings",
        ) as mock_settings, patch(
            "backend.routers.integration._overlay_runtime_settings",
            return_value=None,
        ):
            mock_settings.jira_webhook_secret = ""
            r = app_client.post(
                "/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
                headers={"Authorization": "Bearer wrong-secret"},
            )
        assert r.status_code == 401

    def test_webhook_503_when_no_secret_configured(self, app_client):
        """Resolver yields empty AND the scalar is empty → the webhook
        refuses with 503 (not configured)."""
        async def _resolver(host, platform, **_kw):
            return ""

        with patch(
            "backend.git_credentials.get_webhook_secret_for_host_async",
            side_effect=_resolver,
        ), patch(
            "backend.routers.webhooks.settings",
        ) as mock_settings, patch(
            "backend.routers.integration._overlay_runtime_settings",
            return_value=None,
        ):
            mock_settings.jira_webhook_secret = ""
            r = app_client.post(
                "/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
                headers={"Authorization": "Bearer anything"},
            )
        assert r.status_code == 503

    def test_webhook_scalar_fallback_when_resolver_raises(self, app_client):
        """If the resolver raises (e.g. pool not up on a shim-mode
        boot), the webhook falls back to ``settings.jira_webhook_secret``
        so single-instance deployments keep working."""

        async def _resolver_raises(host, platform, **_kw):
            raise RuntimeError("simulated pool hiccup")

        scalar_secret = "fallback-scalar-secret"

        with patch(
            "backend.git_credentials.get_webhook_secret_for_host_async",
            side_effect=_resolver_raises,
        ), patch(
            "backend.routers.webhooks.settings",
        ) as mock_settings, patch(
            "backend.routers.integration._overlay_runtime_settings",
            return_value=None,
        ):
            mock_settings.jira_webhook_secret = scalar_secret
            r = app_client.post(
                "/webhooks/jira",
                json={"webhookEvent": "jira:issue_updated"},
                headers={"Authorization": f"Bearer {scalar_secret}"},
            )
        assert r.status_code == 200, r.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. _build_registry legacy-shim — default-jira virtual row
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLegacyShimJiraRow:
    """Phase 5-8 extended the legacy shim to synthesise a
    ``default-jira`` virtual row from ``notification_jira_*``. This
    gives ``pick_default('jira')`` a uniform answer regardless of
    whether the pool is up — SQLite dev / pool-not-up deployments get
    the same resolver contract as pool-backed ones."""

    def test_shim_adds_default_jira_row_when_scalar_set(self):
        from backend.git_credentials import (
            _build_registry, clear_credential_cache,
        )
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            # Bare minimum to hit the JIRA branch only.
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = ""
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_ssh_key_path = ""
            mock.notification_jira_url = "https://jira.legacy.corp.com"
            mock.notification_jira_token = "legacy-jira-token"
            mock.notification_jira_project = "OMNI"
            mock.jira_webhook_secret = "legacy-whs"
            registry = _build_registry()
        clear_credential_cache()

        jira_rows = [r for r in registry if r["platform"] == "jira"]
        assert len(jira_rows) == 1, (
            "legacy shim must synthesise exactly one default-jira row "
            "from notification_jira_token"
        )
        row = jira_rows[0]
        assert row["id"] == "default-jira"
        assert row["is_default"] is True
        assert row["token"] == "legacy-jira-token"
        assert row["instance_url"] == "https://jira.legacy.corp.com"
        assert row["project"] == "OMNI"
        assert row["webhook_secret"] == "legacy-whs"

    def test_shim_omits_jira_row_when_no_scalar(self):
        """No legacy JIRA settings → no virtual row. Prevents a
        ``pick_default('jira')`` call from returning a dummy empty
        row on a fresh install."""
        from backend.git_credentials import (
            _build_registry, clear_credential_cache,
        )
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = ""
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_ssh_key_path = ""
            mock.notification_jira_url = ""
            mock.notification_jira_token = ""
            mock.notification_jira_project = ""
            mock.jira_webhook_secret = ""
            registry = _build_registry()
        clear_credential_cache()

        jira_rows = [r for r in registry if r["platform"] == "jira"]
        assert jira_rows == []
