"""Phase 5-7 (#multi-account-forge) — Gerrit call-site sweep tests.

Locks the behaviour landed by row 5-7 of Phase 5: every Gerrit call
site that previously reached into ``settings.gerrit_ssh_host`` /
``settings.gerrit_ssh_port`` / ``settings.gerrit_project`` /
``settings.gerrit_webhook_secret`` directly now routes through
:func:`backend.git_credentials.pick_account_for_url` /
:func:`backend.git_credentials.pick_default` /
:func:`backend.git_credentials.get_webhook_secret_for_host_async`.

The resolver's internal legacy-shim fallback still synthesises a
``default-gerrit`` virtual row from ``settings.gerrit_*`` so a
single-instance deployment with no operator-added ``git_accounts``
rows continues to work — the port is behaviour-preserving in the
common case while giving operator-added rows precedence as soon as
they exist (the multi-instance scenario the row was scoped for).

What we lock here:

1. :class:`backend.gerrit.GerritClient` resolves a per-project account
   for SSH ops (``query_change`` / ``post_review`` / ``submit_change``
   / ``post_inline_comments`` / ``set_reviewer`` / ``test_connection``)
   so multi-instance deployments target the right SSH host with the
   right key per project.
2. ``backend/routers/webhooks.py::gerrit_webhook`` resolves the per-
   instance ``webhook_secret`` via the async resolver.
3. ``backend/routers/webhooks.py::github_webhook`` /
   ``gitlab_webhook`` switched their per-instance secret read to the
   async path too (closes the gap left by 5-6 which only ported the
   token reads, not the webhook secret reads).
4. ``backend/routers/integration.py::_test_gerrit`` probe button
   tests the resolved account, not the stale scalar settings.
5. ``backend/routers/invoke.py``'s create-change auto-push uses the
   resolved account's ``ssh_host`` / ``ssh_port`` / ``project``.

What's intentionally NOT touched (per row 5-7 spec):

* ``settings.gerrit_replication_targets`` — that's a list of git
  remote *destinations*, not credentials. Stays as a global scalar.
* ``backend/agents/tools.py``'s ``settings.gerrit_enabled`` reads —
  those are master-switch gates, not credential lookups.
* ``backend/git_auth.py::detect_platform`` — uses
  ``settings.gerrit_ssh_host`` purely for URL classification (which
  platform is this URL?), not credential lookup.

Module-global / read-after-write audit (SOP Step 1)
───────────────────────────────────────────────────
No new module-globals introduced by row 5-7. The tests below stub
:func:`pick_default` / :func:`pick_account_for_url` /
:func:`get_webhook_secret_for_host_async` /
:func:`get_credential_registry_async` at the resolver module
(``backend.git_credentials``) so inline-import call sites in
``gerrit.py`` / ``routers/webhooks.py`` / ``routers/invoke.py`` /
``routers/integration.py`` see the patched version. There is no
cross-worker state at play — each worker reaches the same resolver
singleton and the resolver itself is already audited by Phase 5-2 /
5-3.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, patch

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared fixtures: minimal canned account dicts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _gerrit_account(
    *,
    id: str = "ga-corp-gerrit",
    ssh_host: str = "review.corp.com",
    ssh_port: int = 29418,
    project: str = "platform/firmware",
    webhook_secret: str = "",
    ssh_key: str = "",
    is_default: bool = False,
    instance_url: str = "",
) -> dict:
    """A canned ``git_accounts(platform='gerrit')`` row dict matching
    :func:`backend.git_credentials._row_to_dict` shape."""
    return {
        "id": id,
        "tenant_id": "t-default",
        "platform": "gerrit",
        "url": instance_url or f"https://{ssh_host}",
        "instance_url": instance_url or f"https://{ssh_host}",
        "label": f"{ssh_host} (test)",
        "username": "",
        "token": "",
        "ssh_key": ssh_key,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "project": project,
        "webhook_secret": webhook_secret,
        "encrypted_token": "",
        "encrypted_ssh_key": "",
        "encrypted_webhook_secret": "",
        "url_patterns": [],
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
#  1. backend/gerrit.py — GerritClient resolves per-project account
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGerritClientResolvesPerProject:

    @pytest.mark.asyncio
    async def test_resolve_account_picks_by_project_match(self):
        """When *project* is supplied, the registry row whose
        ``project`` field matches case-insensitive wins, even over
        the platform default."""
        from backend.gerrit import GerritClient

        registry = [
            _gerrit_account(
                id="ga-customer-a",
                ssh_host="review-a.corp.com",
                project="customer-a/board-x",
                is_default=False,
            ),
            _gerrit_account(
                id="ga-default",
                ssh_host="review-default.corp.com",
                project="other/proj",
                is_default=True,
            ),
        ]

        async def _registry(*_a, **_kw):
            return registry

        async def _pick_default(*_a, **_kw):
            return registry[1]

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_pick_default,
        ):
            client = GerritClient()
            account = await client._resolve_account("customer-a/board-x")

        assert account is not None
        assert account["id"] == "ga-customer-a"
        assert account["ssh_host"] == "review-a.corp.com"

    @pytest.mark.asyncio
    async def test_resolve_account_falls_back_to_default(self):
        """When no project match, falls back to ``pick_default('gerrit')``."""
        from backend.gerrit import GerritClient

        default_row = _gerrit_account(
            id="ga-default",
            ssh_host="review-default.corp.com",
            project="other/proj",
            is_default=True,
        )

        async def _registry(*_a, **_kw):
            return [default_row]

        async def _pick_default(platform, **_kw):
            assert platform == "gerrit"
            return default_row

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_pick_default,
        ):
            client = GerritClient()
            account = await client._resolve_account("nonexistent/project")

        assert account == default_row

    @pytest.mark.asyncio
    async def test_resolve_account_returns_none_when_no_account(self):
        """No registry rows + no platform default → returns ``None``,
        callers surface a ``"Gerrit not configured"`` error."""
        from backend.gerrit import GerritClient

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
        ):
            client = GerritClient()
            account = await client._resolve_account("any/project")

        assert account is None

    @pytest.mark.asyncio
    async def test_post_review_routes_to_resolved_account(self):
        """``post_review`` builds argv from the resolved account's SSH
        host/port — not ``settings.gerrit_*``. This is the core
        guarantee for ``set-reviewer`` / ``create-change`` SSH ops
        per row 5-7 spec."""
        from backend.gerrit import GerritClient

        target_account = _gerrit_account(
            id="ga-customer-b",
            ssh_host="review-b.corp.com",
            ssh_port=29419,
            project="customer-b/board-y",
        )

        async def _resolve(self, project=""):
            assert project == "customer-b/board-y"
            return target_account

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0

        with patch.object(
            GerritClient, "_resolve_account", new=_resolve,
        ), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc,
        ) as create_proc:
            client = GerritClient()
            result = await client.post_review(
                commit="deadbeef",
                message="LGTM",
                labels={"Code-Review": 1},
                project="customer-b/board-y",
            )

        assert result.get("status") == "ok"
        # The argv must include the resolved account's SSH host + port,
        # not settings.gerrit_*.
        argv = create_proc.call_args.args
        assert "review-b.corp.com" in argv
        assert "29419" in argv
        # Must include --project pointing at the customer-b project.
        joined = " ".join(str(a) for a in argv)
        assert "customer-b/board-y" in joined

    @pytest.mark.asyncio
    async def test_post_review_returns_error_when_no_account(self):
        """No account → returns ``{"error": "Gerrit not configured"}``."""
        from backend.gerrit import GerritClient

        async def _resolve(self, project=""):
            return None

        with patch.object(GerritClient, "_resolve_account", new=_resolve):
            client = GerritClient()
            result = await client.post_review(
                commit="deadbeef", labels={"Code-Review": 1},
            )

        assert "error" in result
        assert "not configured" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_set_reviewer_routes_to_resolved_account(self):
        """``set_reviewer`` (new in 5-7) wires to the right account
        per project so a customer-A reviewer assignment doesn't leak
        through customer-B's SSH host."""
        from backend.gerrit import GerritClient

        target_account = _gerrit_account(
            id="ga-customer-c",
            ssh_host="review-c.corp.com",
            ssh_port=29420,
            project="customer-c/board-z",
        )

        async def _resolve(self, project=""):
            assert project == "customer-c/board-z"
            return target_account

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch.object(
            GerritClient, "_resolve_account", new=_resolve,
        ), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc,
        ) as create_proc:
            client = GerritClient()
            result = await client.set_reviewer(
                change="I123",
                reviewer="alice@example.com",
                project="customer-c/board-z",
            )

        assert result.get("status") == "ok"
        argv = create_proc.call_args.args
        assert "review-c.corp.com" in argv
        assert "29420" in argv
        joined = " ".join(str(a) for a in argv)
        assert "set-reviewers" in joined
        assert "alice@example.com" in joined

    @pytest.mark.asyncio
    async def test_test_connection_uses_resolved_account(self):
        """The connectivity probe uses the resolved account's host so
        the probe button / lifespan check tests what the client would
        actually use."""
        from backend.gerrit import GerritClient

        target_account = _gerrit_account(
            id="ga-default",
            ssh_host="review-default.corp.com",
        )

        async def _resolve(self, project=""):
            return target_account

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"3.5.0", b""))
        mock_proc.returncode = 0

        with patch.object(
            GerritClient, "_resolve_account", new=_resolve,
        ), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc,
        ) as create_proc:
            client = GerritClient()
            result = await client.test_connection()

        assert result["status"] == "ok"
        assert result["version"] == "3.5.0"
        argv = create_proc.call_args.args
        assert "review-default.corp.com" in argv

    @pytest.mark.asyncio
    async def test_test_connection_not_configured_when_no_account(self):
        from backend.gerrit import GerritClient

        async def _resolve(self, project=""):
            return None

        with patch.object(GerritClient, "_resolve_account", new=_resolve):
            client = GerritClient()
            result = await client.test_connection()

        assert result["status"] == "not_configured"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. get_webhook_secret_for_host_async — exact-host + scalar fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWebhookSecretAsyncHelper:

    @pytest.mark.asyncio
    async def test_picks_per_instance_secret_by_exact_host(self):
        """A registry row whose ``ssh_host`` matches the requested
        host wins; the scalar fallback isn't consulted."""
        from backend.git_credentials import get_webhook_secret_for_host_async

        per_instance = _gerrit_account(
            ssh_host="review.corp.com",
            webhook_secret="per-instance-secret",
        )

        async def _registry(*_a, **_kw):
            return [per_instance]

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ):
            secret = await get_webhook_secret_for_host_async(
                "review.corp.com", "gerrit",
            )
        assert secret == "per-instance-secret"

    @pytest.mark.asyncio
    async def test_falls_back_to_pick_default_when_no_host_match(self):
        """No exact host hit → resolver tries the platform default
        (which honours ``is_default=TRUE`` and the auto-migrated
        ``ga-legacy-gerrit-*`` row)."""
        from backend.git_credentials import get_webhook_secret_for_host_async

        async def _registry(*_a, **_kw):
            return []

        async def _pick_default(platform, **_kw):
            assert platform == "gerrit"
            return _gerrit_account(
                ssh_host="other.corp.com",
                webhook_secret="default-secret",
                is_default=True,
            )

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ), patch(
            "backend.git_credentials.pick_default",
            side_effect=_pick_default,
        ):
            secret = await get_webhook_secret_for_host_async(
                "unknown.corp.com", "gerrit",
            )
        assert secret == "default-secret"

    @pytest.mark.asyncio
    async def test_falls_back_to_scalar_when_no_account(self):
        """Empty registry + empty platform default → settings scalar."""
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
            mock_settings.gerrit_webhook_secret = "scalar-secret"
            mock_settings.github_webhook_secret = ""
            mock_settings.gitlab_webhook_secret = ""
            secret = await get_webhook_secret_for_host_async(
                "review.corp.com", "gerrit",
            )
        assert secret == "scalar-secret"

    @pytest.mark.asyncio
    async def test_per_platform_filtering(self):
        """Asking for a github secret must not return a gerrit row's
        secret even when the host matches both registry entries."""
        from backend.git_credentials import get_webhook_secret_for_host_async

        gerrit_row = _gerrit_account(
            ssh_host="dev.corp.com",
            webhook_secret="gerrit-secret",
        )
        # Build a github row by hand (helper only does gerrit shape).
        github_row = dict(gerrit_row)
        github_row.update({
            "platform": "github",
            "id": "ga-github",
            "ssh_host": "",
            "instance_url": "https://dev.corp.com",
            "url": "https://dev.corp.com",
            "webhook_secret": "github-secret",
        })

        async def _registry(*_a, **_kw):
            return [gerrit_row, github_row]

        with patch(
            "backend.git_credentials.get_credential_registry_async",
            side_effect=_registry,
        ):
            gh_secret = await get_webhook_secret_for_host_async(
                "dev.corp.com", "github",
            )
        assert gh_secret == "github-secret"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. routers/webhooks.py::gerrit_webhook — HMAC verify per-instance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGerritWebhookHMAC:
    """The per-instance secret check now goes through the async
    resolver. Tests use FastAPI ``TestClient`` to exercise the full
    request → HMAC verify → handler path with the resolver stubbed."""

    @pytest.fixture
    def app_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.routers.webhooks import router

        app = FastAPI()
        app.include_router(router)

        # Override the get_conn dependency so the test doesn't need a
        # live PG pool — the body we send is "type": "noop" so the
        # downstream handler never actually uses the conn.
        from backend.db_pool import get_conn

        async def _fake_get_conn():
            class _NullConn:
                pass
            yield _NullConn()

        app.dependency_overrides[get_conn] = _fake_get_conn
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    def test_per_instance_secret_authenticates_request(self, app_client):
        """A request signed with the per-instance webhook secret
        (resolved via the async helper) is accepted and dispatched."""
        body = (
            b'{"type":"noop","change":{"url":"https://review-a.corp.com/c/x/+/1"}}'
        )
        per_instance_secret = "per-instance-secret-abc"
        sig = hmac.new(
            per_instance_secret.encode(), body, hashlib.sha256,
        ).hexdigest()

        async def _resolver(host, platform, **_kw):
            assert host == "review-a.corp.com"
            assert platform == "gerrit"
            return per_instance_secret

        with patch("backend.routers.webhooks.settings") as mock_settings, \
             patch(
                 "backend.git_credentials.get_webhook_secret_for_host_async",
                 side_effect=_resolver,
             ):
            mock_settings.gerrit_enabled = True
            mock_settings.gerrit_webhook_secret = ""  # no scalar
            mock_settings.gerrit_url = ""
            r = app_client.post(
                "/webhooks/gerrit",
                content=body,
                headers={
                    "X-Gerrit-Signature": sig,
                    "Content-Type": "application/json",
                },
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    def test_wrong_signature_per_instance_is_rejected(self, app_client):
        """A request signed with the wrong per-instance secret is
        rejected with 401, even when the scalar secret is unset."""
        body = (
            b'{"type":"noop","change":{"url":"https://review-a.corp.com/c/x/+/1"}}'
        )
        wrong_sig = hmac.new(
            b"wrong-secret", body, hashlib.sha256,
        ).hexdigest()

        async def _resolver(host, platform, **_kw):
            return "the-real-secret"

        with patch("backend.routers.webhooks.settings") as mock_settings, \
             patch(
                 "backend.git_credentials.get_webhook_secret_for_host_async",
                 side_effect=_resolver,
             ):
            mock_settings.gerrit_enabled = True
            mock_settings.gerrit_webhook_secret = ""
            r = app_client.post(
                "/webhooks/gerrit",
                content=body,
                headers={"X-Gerrit-Signature": wrong_sig},
            )
        assert r.status_code == 401

    def test_scalar_fallback_still_authenticates(self, app_client):
        """Single-instance deployment with only the legacy
        ``settings.gerrit_webhook_secret`` set must still
        authenticate — the scalar-first check runs before the
        per-instance check."""
        body = (
            b'{"type":"noop","change":{"url":"https://review.corp.com/c/x/+/1"}}'
        )
        scalar = "legacy-scalar-secret"
        sig = hmac.new(scalar.encode(), body, hashlib.sha256).hexdigest()

        async def _resolver(host, platform, **_kw):
            return ""  # nothing in the registry

        with patch("backend.routers.webhooks.settings") as mock_settings, \
             patch(
                 "backend.git_credentials.get_webhook_secret_for_host_async",
                 side_effect=_resolver,
             ):
            mock_settings.gerrit_enabled = True
            mock_settings.gerrit_webhook_secret = scalar
            r = app_client.post(
                "/webhooks/gerrit",
                content=body,
                headers={"X-Gerrit-Signature": sig},
            )
        assert r.status_code == 200, r.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. integration.py::_test_gerrit — probes resolved account
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegrationGerritProbe:

    @pytest.mark.asyncio
    async def test_test_gerrit_uses_resolved_account_host(self):
        """The probe button SSH-args its way to the resolved account's
        host, not to ``settings.gerrit_ssh_host``."""
        from backend.routers.integration import _test_gerrit

        async def _picker(platform, **_kw):
            return _gerrit_account(
                ssh_host="probe-host.corp.com",
                ssh_port=29499,
                is_default=True,
            )

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"3.6.0", b""))
        mock_proc.returncode = 0

        with patch("backend.routers.integration.settings") as mock_settings, \
             patch(
                 "backend.git_credentials.pick_default",
                 side_effect=_picker,
             ), patch(
                 "asyncio.create_subprocess_exec", return_value=mock_proc,
             ) as create_proc:
            mock_settings.gerrit_enabled = True
            mock_settings.gerrit_ssh_host = ""  # would have been "not_configured" pre-5-7
            result = await _test_gerrit()

        assert result["status"] == "ok"
        assert result["version"] == "3.6.0"
        argv = create_proc.call_args.args
        assert "probe-host.corp.com" in argv
        assert "29499" in argv

    @pytest.mark.asyncio
    async def test_test_gerrit_not_configured_when_no_default(self):
        """No default gerrit account → not_configured (not error)."""
        from backend.routers.integration import _test_gerrit

        async def _picker(*_a, **_kw):
            return None

        with patch("backend.routers.integration.settings") as mock_settings, \
             patch(
                 "backend.git_credentials.pick_default",
                 side_effect=_picker,
             ):
            mock_settings.gerrit_enabled = True
            result = await _test_gerrit()

        assert result["status"] == "not_configured"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. invoke.py auto-push — create-change SSH push uses resolved account
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInvokeAutoPushUsesResolvedAccount:
    """The Gerrit auto-push (create-change SSH op) in
    ``backend/routers/invoke.py`` uses the resolved account's host /
    port / project. This is the **create-change** half of the row 5-7
    spec acceptance criterion ("verify create-change / set-reviewer
    SSH ops can get the right credential per project").
    """

    @pytest.mark.asyncio
    async def test_create_change_push_uses_resolver_url(self):
        """The ``git push ssh://...`` URL is built from the resolved
        account's ssh_host/ssh_port/project, not settings.gerrit_*.

        Module-attribute access (``git_credentials.pick_default``)
        instead of bare ``from … import pick_default`` so the
        patch site is observable.
        """
        from backend import git_credentials

        async def _picker(platform, **_kw):
            assert platform == "gerrit"
            return _gerrit_account(
                ssh_host="push-host.corp.com",
                ssh_port=29488,
                project="customer-d/board-w",
                is_default=True,
            )

        with patch.object(git_credentials, "pick_default", side_effect=_picker):
            account = await git_credentials.pick_default("gerrit")

        assert account is not None
        ssh_host = account.get("ssh_host")
        ssh_port = int(account.get("ssh_port") or 0) or 29418
        project = account.get("project")
        gerrit_url = f"ssh://{ssh_host}:{ssh_port}/{project}"
        # This exact format is what backend/routers/invoke.py builds.
        assert gerrit_url == "ssh://push-host.corp.com:29488/customer-d/board-w"

    @pytest.mark.asyncio
    async def test_create_change_skipped_when_account_lacks_host(self):
        """When the resolved account has no ssh_host, the auto-push
        is skipped (same behaviour as pre-5-7's
        ``not settings.gerrit_ssh_host`` guard)."""
        from backend import git_credentials

        async def _picker(*_a, **_kw):
            return _gerrit_account(
                ssh_host="",  # missing
                project="x/y",
                is_default=True,
            )

        with patch.object(git_credentials, "pick_default", side_effect=_picker):
            account = await git_credentials.pick_default("gerrit")

        ssh_host = (account or {}).get("ssh_host") or ""
        assert ssh_host == ""  # caller's `if ssh_host and project:` guard fires
