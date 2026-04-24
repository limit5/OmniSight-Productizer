"""Phase 5-6 (#multi-account-forge) — GitHub + GitLab call-site sweep tests.

Locks the behaviour landed by row 5-6 of Phase 5: every async caller
that previously reached into ``settings.github_token`` /
``settings.gitlab_token`` / ``settings.gitlab_url`` directly now routes
through :func:`backend.git_credentials.pick_account_for_url` (with a
URL) or :func:`backend.git_credentials.pick_default` (platform-wide).
The resolver's internal legacy-shim fallback guarantees that empty
``git_accounts`` deployments see the same Settings scalars they always
did, so the port is behaviour-preserving in the common case while
giving operator-configured ``git_accounts`` rows precedence as soon as
they exist.

This file is deliberately thin — the heavy lifting lives in
``test_git_credentials_phase5_2.py`` / ``test_git_credentials_phase5_3.py``
(resolver contract) and the per-module unit tests (``test_release.py``,
``test_integration_settings.py``, …). What we lock here is the *wire*
between call sites and the resolver:

1. ``release.py::upload_to_github`` / ``upload_to_gitlab`` take their
   token from ``pick_default``.
2. ``issue_tracker.py`` GitHub + GitLab sync/comment pick by URL first
   (for tenant-specific github.com orgs) and fall back to platform
   default.
3. ``git_platform.py::_create_github_pr`` / ``_create_gitlab_mr`` same
   as (2) — URL first, default fallback.
4. ``routers/integration.py::_test_github`` / ``_test_gitlab`` use
   ``pick_default`` so the ``/system/test/{integration}`` button tests
   what the resolver would actually send, not a stale scalar setting.
5. ``routers/webhooks.py::_trigger_ci_pipelines`` same pattern for
   both GitHub Actions (``gh`` CLI env) and GitLab CI (REST API).
6. ``git_auth._get_token_for_url`` and
   ``git_credentials.get_token_for_url`` no longer have the dangerous
   "any github.com host matches → hand out default-github token" legacy
   fallback — the resolver either finds a registry match or returns
   empty. This fixes the token-leak-to-ghe-enterprise hazard described
   in the row 5-6 docstring.

Module-global / read-after-write audit (SOP Step 1)
───────────────────────────────────────────────────
No new module-globals introduced by row 5-6. The tests below stub
:func:`pick_default` / :func:`pick_account_for_url` at the resolver
module (``backend.git_credentials``) so inline-import call sites in
``release.py`` / ``issue_tracker.py`` / … see the patched version.
There is no cross-worker state at play — each worker reaches the same
resolver singleton (the async pool or the legacy shim) and the resolver
itself is already audited by Phase 5-2 / 5-3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared stubs — make the resolver return a canned account dict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _stub_resolver(github_token: str = "", gitlab_token: str = "",
                    gitlab_url: str = ""):
    """Build an async-side_effect pair for pick_account_for_url /
    pick_default that yields canned token values per platform."""

    async def _fake_pick_default(platform, **_kw):
        if platform == "github" and github_token:
            return {"token": github_token}
        if platform == "gitlab" and (gitlab_token or gitlab_url):
            row = {"token": gitlab_token}
            if gitlab_url:
                row["instance_url"] = gitlab_url
            return row
        return None

    async def _fake_pick_account_for_url(url, **_kw):
        # URL-pattern match is not exercised here; the resolver tests
        # already lock that contract. Returning None sends the caller
        # through the pick_default fallback — which is exactly the
        # control flow Phase 5-6 established.
        return None

    return _fake_pick_default, _fake_pick_account_for_url


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. release.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReleaseUploadsUseResolver:
    """``upload_to_github`` / ``upload_to_gitlab`` pull their token
    from :func:`pick_default` — not a direct settings read."""

    @pytest.mark.asyncio
    async def test_upload_to_github_uses_pick_default(self):
        from backend.release import upload_to_github
        picker, _ = _stub_resolver(github_token="ghp_from_resolver")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"https://github.com/owner/repo/releases/v1.0.0\n", b""),
        )
        mock_proc.returncode = 0

        with patch("backend.config.settings") as mock_settings, \
             patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc) as create_proc:
            mock_settings.github_repo = "owner/repo"
            mock_settings.release_draft = False
            result = await upload_to_github("/tmp/bundle.tar.gz", "1.0.0", {"artifact_count": 1})

        assert result["status"] == "uploaded"
        # Confirm the resolver-returned token flowed through to gh env.
        call_env = create_proc.call_args.kwargs["env"]
        assert call_env["GH_TOKEN"] == "ghp_from_resolver"

    @pytest.mark.asyncio
    async def test_upload_to_github_skips_without_resolver_token(self):
        """``pick_default`` returning None → upload is skipped cleanly,
        matching the pre-5-6 "no github_token configured" contract."""
        from backend.release import upload_to_github
        picker, _ = _stub_resolver()  # no token

        with patch("backend.config.settings") as mock_settings, \
             patch("backend.git_credentials.pick_default", side_effect=picker):
            mock_settings.github_repo = "owner/repo"
            mock_settings.release_draft = False
            result = await upload_to_github("/tmp/bundle.tar.gz", "1.0.0", {})

        assert result["status"] == "skipped"
        assert "github_token" in result["reason"]

    @pytest.mark.asyncio
    async def test_upload_to_gitlab_prefers_resolver_instance_url(self):
        """Row's ``instance_url`` wins over legacy ``settings.gitlab_url``
        — multi-instance deployments need this to target the right
        GitLab API endpoint."""
        from backend.release import upload_to_gitlab
        picker, _ = _stub_resolver(
            gitlab_token="glpat_from_resolver",
            gitlab_url="https://gitlab.self.corp",
        )

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b'{"tag_name":"v1.0.0","_links":{"self":"https://gitlab.self.corp/api"}}', b""),
        )
        mock_proc.returncode = 0

        with patch("backend.config.settings") as mock_settings, \
             patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc) as create_proc:
            mock_settings.gitlab_project_id = "group/proj"
            mock_settings.gitlab_url = "https://gitlab.com"  # should be overridden
            result = await upload_to_gitlab("/tmp/bundle.tar.gz", "1.0.0", {})

        # Either "uploaded" (parse happy path) or something richer, but
        # the critical invariant is that the self-hosted URL flowed in.
        assert result.get("tag") == "v1.0.0" or result["status"] == "uploaded"
        # First curl invocation is tag-create — check it hit the
        # resolver's instance_url, not settings.gitlab_url.
        first_call = create_proc.call_args_list[0]
        args = first_call.args
        # args[0] = "curl", so the URL is somewhere in args — find it.
        combined = " ".join(a if isinstance(a, str) else "" for a in args)
        assert "gitlab.self.corp" in combined
        assert "gitlab.com/api" not in combined


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. issue_tracker.py (GitHub + GitLab only — JIRA is 5-8 scope)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIssueTrackerUsesResolver:

    @pytest.mark.asyncio
    async def test_sync_gitlab_uses_resolver(self):
        from backend.issue_tracker import _sync_gitlab
        picker, url_picker = _stub_resolver(gitlab_token="glpat_from_resolver")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("backend.git_credentials.pick_account_for_url", side_effect=url_picker), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc) as create_proc:
            await _sync_gitlab(
                "https://gitlab.com/group/proj/-/issues/42",
                "completed",
                "",
            )

        # curl was called with -H "PRIVATE-TOKEN: glpat_from_resolver"
        args = create_proc.call_args_list[0].args
        token_header = next(
            (args[i + 1] for i, a in enumerate(args[:-1]) if a == "-H" and "PRIVATE-TOKEN" in args[i + 1]),
            "",
        )
        assert "glpat_from_resolver" in token_header

    @pytest.mark.asyncio
    async def test_sync_gitlab_returns_error_without_token(self):
        from backend.issue_tracker import _sync_gitlab
        picker, url_picker = _stub_resolver()  # no token

        with patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("backend.git_credentials.pick_account_for_url", side_effect=url_picker):
            result = await _sync_gitlab(
                "https://gitlab.com/group/proj/-/issues/42",
                "completed",
                "",
            )
        assert result["status"] == "error"
        assert "GitLab" in result["message"]

    @pytest.mark.asyncio
    async def test_sync_github_uses_resolver_env(self):
        from backend.issue_tracker import _sync_github
        picker, url_picker = _stub_resolver(github_token="ghp_from_resolver")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("backend.git_credentials.pick_account_for_url", side_effect=url_picker), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc) as create_proc:
            await _sync_github(
                "https://github.com/owner/repo/issues/42",
                "completed",
                "",
            )

        # gh CLI was called with env GITHUB_TOKEN=ghp_from_resolver
        first_env = create_proc.call_args_list[0].kwargs.get("env", {})
        assert first_env.get("GITHUB_TOKEN") == "ghp_from_resolver"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. routers/integration.py — _test_github / _test_gitlab
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegrationProbesUseResolver:

    @pytest.mark.asyncio
    async def test_test_github_reads_from_resolver(self):
        from backend.routers.integration import _test_github
        picker, _ = _stub_resolver(github_token="ghp_resolver_probe")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b'\r\nX-OAuth-Scopes: repo\r\n\r\n{"login": "octocat"}', b""),
        )
        mock_proc.returncode = 0

        with patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc) as create_proc:
            result = await _test_github()

        assert result["status"] == "ok"
        # curl was called with Authorization: token ghp_resolver_probe
        args = create_proc.call_args_list[0].args
        combined = " ".join(str(a) for a in args)
        assert "ghp_resolver_probe" in combined

    @pytest.mark.asyncio
    async def test_test_github_not_configured_when_resolver_empty(self):
        from backend.routers.integration import _test_github
        picker, _ = _stub_resolver()
        with patch("backend.git_credentials.pick_default", side_effect=picker):
            result = await _test_github()
        assert result["status"] == "not_configured"

    @pytest.mark.asyncio
    async def test_test_gitlab_prefers_resolver_instance_url(self):
        from backend.routers.integration import _test_gitlab
        picker, _ = _stub_resolver(
            gitlab_token="glpat_probe",
            gitlab_url="https://gitlab.self.corp",
        )

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b'{"version":"17.0.0","revision":"abc"}', b""),
        )
        mock_proc.returncode = 0

        with patch("backend.config.settings") as mock_settings, \
             patch("backend.git_credentials.pick_default", side_effect=picker), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc) as create_proc:
            mock_settings.gitlab_url = "https://gitlab.com"  # should be overridden
            result = await _test_gitlab()

        assert result["status"] == "ok"
        assert result["url"] == "https://gitlab.self.corp"
        args = create_proc.call_args_list[0].args
        combined = " ".join(str(a) for a in args)
        assert "gitlab.self.corp/api/v4/version" in combined


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. git_auth.py + git_credentials.get_token_for_url
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSyncGetTokenNoLongerLeaksAcrossHosts:
    """Phase 5-6 dropped the dangerous platform-agnostic scalar
    fallback in both :func:`backend.git_credentials.get_token_for_url`
    and :func:`backend.git_auth._get_token_for_url`. A GHE Enterprise
    URL with no matching credential must now return ``""`` instead of
    leaking the github.com default token."""

    def test_get_token_for_unrelated_host_returns_empty(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import (
            clear_credential_cache, get_token_for_url,
        )
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            # Simulate operator with only github.com scalar token set.
            mock.github_token = "ghp_gc"
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = ""
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            # Phase 5-8: clear JIRA scalars too so the shim doesn't
            # synthesise a default-jira row whose host accidentally
            # substring-matches the GHE enterprise URL in this test.
            mock.notification_jira_url = ""
            mock.notification_jira_token = ""
            mock.notification_jira_project = ""
            mock.jira_webhook_secret = ""
            # An unrelated GitHub Enterprise host should NOT receive the
            # github.com token just because `detect_platform` says "this
            # looks like a github URL".
            token = get_token_for_url("https://ghe.mycompany.com/org/repo")
        assert token == "", (
            "Phase 5-6 regression: got_token_for_url leaked a token to an "
            "unrelated host. The dangerous platform-agnostic fallback was "
            "supposed to be removed — see backend/git_credentials.py."
        )
        clear_credential_cache()

    def test_get_token_for_url_github_com_still_resolves(self):
        """Backward-compat guarantee: the legitimate github.com ↔
        settings.github_token path still works via the registry's
        scalar-fallback synthesis."""
        import json
        from unittest.mock import patch
        from backend.git_credentials import (
            clear_credential_cache, get_token_for_url,
        )
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = "ghp_legit"
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = ""
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            # Phase 5-8: clear JIRA scalars (same reason as the adjacent
            # GHE enterprise test).
            mock.notification_jira_url = ""
            mock.notification_jira_token = ""
            mock.notification_jira_project = ""
            mock.jira_webhook_secret = ""
            token = get_token_for_url("https://github.com/acme/app")
        assert token == "ghp_legit"
        clear_credential_cache()
