"""Tests for backend/gerrit.py and Gerrit-related tools."""

from backend.gerrit import GerritClient
from backend.git_auth import detect_platform


class TestGerritPlatformDetection:

    def test_detect_gerrit_when_configured(self):
        """When gerrit_ssh_host matches, detect as gerrit."""
        from backend.config import settings
        original = settings.gerrit_ssh_host
        try:
            settings.gerrit_ssh_host = "gerrit.sora.services"
            assert detect_platform("ssh://gerrit.sora.services:29418/project") == "gerrit"
            assert detect_platform("git@gerrit.sora.services:project/core.git") == "gerrit"
        finally:
            settings.gerrit_ssh_host = original

    def test_detect_not_gerrit_when_unconfigured(self):
        """When gerrit_ssh_host is empty, unknown URLs return 'unknown'."""
        from backend.config import settings
        original = settings.gerrit_ssh_host
        try:
            settings.gerrit_ssh_host = ""
            assert detect_platform("ssh://gerrit.sora.services:29418/project") == "unknown"
        finally:
            settings.gerrit_ssh_host = original

    def test_github_still_works(self):
        assert detect_platform("https://github.com/org/repo.git") == "github"

    def test_gitlab_still_works(self):
        assert detect_platform("https://gitlab.com/org/repo.git") == "gitlab"


class TestGerritClient:

    def test_ssh_args_for_account(self):
        """Phase 5-7: ``_ssh_args_for`` builds argv from a resolved
        ``git_accounts`` row, not ``settings.gerrit_*`` scalars."""
        client = GerritClient()
        account = {
            "ssh_host": "review.example.com",
            "ssh_port": 29418,
            "ssh_key": "",
            "project": "core",
        }
        args = client._ssh_args_for(account)
        assert "review.example.com" in args
        assert "29418" in args
        assert "BatchMode=yes" in args
        assert args[0] == "ssh"

    def test_ssh_args_for_account_uses_per_account_key(self):
        """Per-account ``ssh_key`` wins over the global
        ``settings.git_ssh_key_path`` (multi-account credential
        isolation)."""
        client = GerritClient()
        account = {
            "ssh_host": "review.example.com",
            "ssh_port": 29418,
            "ssh_key": "/tmp/per-account-key",
        }
        args = client._ssh_args_for(account)
        assert "-i" in args
        idx = args.index("-i")
        assert args[idx + 1].endswith("per-account-key")


class TestGerritToolRestrictions:

    def test_review_score_limited(self):
        """AI reviewers can only give +1 or -1."""
        from backend.agents.tools import gerrit_submit_review
        from backend.config import settings
        import asyncio
        orig = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = True
            result = asyncio.run(gerrit_submit_review.ainvoke({"commit": "abc123", "score": 2, "message": ""}))
            assert "[BLOCKED]" in result
        finally:
            settings.gerrit_enabled = orig

    def test_review_score_minus2_blocked(self):
        from backend.agents.tools import gerrit_submit_review
        from backend.config import settings
        import asyncio
        orig = settings.gerrit_enabled
        try:
            settings.gerrit_enabled = True
            result = asyncio.run(gerrit_submit_review.ainvoke({"commit": "abc123", "score": -2, "message": ""}))
            assert "[BLOCKED]" in result
        finally:
            settings.gerrit_enabled = orig


# ──────────────────────────────────────────────────────────────────────
# OP-1190 — daemon prewarm-cache regression tests.
#
# The bridge daemon's per-event handler threads call ``asyncio.run(...)``
# to enter a fresh event loop. The asyncpg pool initialised in the
# daemon's main loop cannot be acquired from those worker loops without
# raising ``RuntimeError: Task got Future attached to a different loop``.
# ``GerritClient.prewarm_for_daemon`` pre-resolves the registry +
# default account in the main loop so that ``_resolve_account`` can
# serve worker threads synchronously from the class-level cache.
# ──────────────────────────────────────────────────────────────────────


class TestGerritClientPrewarmCache:

    def setup_method(self):
        # Always start each test with a clean cache.
        GerritClient.invalidate_prewarm_cache()

    def teardown_method(self):
        GerritClient.invalidate_prewarm_cache()

    def test_cache_starts_empty(self):
        """Default state — no prewarm has run."""
        assert GerritClient._cached_registry is None
        assert GerritClient._cached_default is None

    def test_cached_resolve_returns_none_when_cache_empty(self):
        """Without prewarm, the cache-only resolver returns None
        (caller falls through to async DB path)."""
        client = GerritClient()
        assert client._resolve_account_from_cache("") is None
        assert client._resolve_account_from_cache("any-project") is None

    def test_cached_resolve_returns_default_when_no_project(self):
        """With cache populated and no project filter, return the
        cached default account."""
        default_row = {
            "platform": "gerrit", "acct_id": "ga-default",
            "ssh_host": "review.example.com", "is_default": True,
        }
        GerritClient._cached_registry = [default_row]
        GerritClient._cached_default = default_row

        client = GerritClient()
        result = client._resolve_account_from_cache("")
        assert result is default_row
        assert result["acct_id"] == "ga-default"

    def test_cached_resolve_project_match_wins_over_default(self):
        """Project-aware lookup beats the default — exactly mirrors
        the async path's precedence."""
        default_row = {
            "platform": "gerrit", "acct_id": "ga-default",
            "ssh_host": "review.example.com",
            "project": "", "is_default": True,
        }
        project_row = {
            "platform": "gerrit", "acct_id": "ga-special",
            "ssh_host": "review2.example.com",
            "project": "omnisight/Special",
        }
        GerritClient._cached_registry = [default_row, project_row]
        GerritClient._cached_default = default_row

        client = GerritClient()
        result = client._resolve_account_from_cache("omnisight/Special")
        assert result is project_row, "project-aware lookup must beat default"

    def test_cached_resolve_project_match_case_insensitive(self):
        """Project match is case-insensitive, matching async path."""
        project_row = {
            "platform": "gerrit", "acct_id": "ga-mixed",
            "project": "Org/Repo-Name",
        }
        GerritClient._cached_registry = [project_row]
        GerritClient._cached_default = None

        client = GerritClient()
        assert client._resolve_account_from_cache("org/repo-name") is project_row
        assert client._resolve_account_from_cache("ORG/REPO-NAME") is project_row
        assert client._resolve_account_from_cache("Org/Repo-Name") is project_row

    def test_cached_resolve_no_project_match_falls_back_to_default(self):
        """If the project filter doesn't match any cached row, return
        the cached default rather than failing."""
        default_row = {
            "platform": "gerrit", "acct_id": "ga-default", "is_default": True,
        }
        other_row = {
            "platform": "gerrit", "acct_id": "ga-other",
            "project": "other/repo",
        }
        GerritClient._cached_registry = [default_row, other_row]
        GerritClient._cached_default = default_row

        client = GerritClient()
        result = client._resolve_account_from_cache("nonexistent/project")
        assert result is default_row

    def test_cached_resolve_filters_non_gerrit_rows(self):
        """The registry contains rows for multiple platforms; the
        cache-aware lookup must filter to platform=gerrit only."""
        github_row = {
            "platform": "github", "acct_id": "ga-github",
            "project": "org/repo",
        }
        gerrit_row = {
            "platform": "gerrit", "acct_id": "ga-gerrit",
            "project": "org/repo",
        }
        GerritClient._cached_registry = [github_row, gerrit_row]
        GerritClient._cached_default = None

        client = GerritClient()
        # Same project name on both platforms — must return gerrit.
        result = client._resolve_account_from_cache("org/repo")
        assert result is gerrit_row

    def test_invalidate_clears_cache(self):
        """``invalidate_prewarm_cache`` resets both attributes."""
        GerritClient._cached_registry = [{"platform": "gerrit"}]
        GerritClient._cached_default = {"platform": "gerrit"}

        GerritClient.invalidate_prewarm_cache()

        assert GerritClient._cached_registry is None
        assert GerritClient._cached_default is None

    def test_resolve_account_serves_from_cache_without_db(self, monkeypatch):
        """OP-1190 the regression test: with the cache populated,
        ``_resolve_account`` must NOT call into asyncpg via
        ``get_credential_registry_async`` or ``pick_default``."""
        import asyncio
        from backend import gerrit as gerrit_mod

        cached_row = {
            "platform": "gerrit", "acct_id": "ga-cached",
            "ssh_host": "review.example.com", "is_default": True,
        }
        GerritClient._cached_registry = [cached_row]
        GerritClient._cached_default = cached_row

        # Sabotage the async path — if _resolve_account falls through
        # to the DB, these will raise instead of silently passing.
        async def _boom(*a, **kw):
            raise AssertionError(
                "_resolve_account fell through to async DB path despite "
                "populated cache — this is the OP-1190 regression"
            )

        monkeypatch.setattr(
            "backend.git_credentials.get_credential_registry_async", _boom,
        )
        monkeypatch.setattr(
            "backend.git_credentials.pick_default", _boom,
        )

        client = GerritClient()
        result = asyncio.run(client._resolve_account(""))
        assert result is cached_row, "must serve from cache"

        # Same when called with a project filter.
        result_p = asyncio.run(
            client._resolve_account("any/project")
        )
        # No project match in cache → returns cached default.
        assert result_p is cached_row

    def test_resolve_account_works_from_different_event_loop(self):
        """OP-1190 root-cause regression: the daemon's per-event thread
        creates a fresh event loop via ``asyncio.run(...)``. Once the
        cache is populated (typically by the main loop's prewarm),
        ``_resolve_account`` must succeed from a different loop without
        touching the asyncpg pool."""
        import asyncio
        import threading

        cached_row = {
            "platform": "gerrit", "acct_id": "ga-from-cache",
        }
        GerritClient._cached_registry = [cached_row]
        GerritClient._cached_default = cached_row

        result_holder: dict = {}

        def worker():
            client = GerritClient()
            try:
                # Mirrors the daemon's _runner: a fresh asyncio.run
                # in a worker thread, creating a NEW event loop.
                result_holder["account"] = asyncio.run(
                    client._resolve_account("")
                )
            except Exception as exc:
                result_holder["error"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "worker thread hung"
        assert "error" not in result_holder, \
            f"worker thread raised: {result_holder.get('error')!r}"
        assert result_holder["account"] is cached_row


# ──────────────────────────────────────────────────────────────────────
# OP-1193 — prewarm builds cache from ENV identity, not DB merger-bot row.
#
# OP-1190 introduced the cache; OP-1193 changes the SOURCE. The daemon
# is its own identity (env-vared OMNISIGHT_GERRIT_SSH_HOST +
# OMNISIGHT_GIT_SSH_KEY_PATH), distinct from any DB row. Previously
# (OP-1190 implementation) ``prewarm_for_daemon`` called
# ``pick_default("gerrit")`` which returned the merger-bot row, causing
# SSH auth failure (``user@host: Permission denied (publickey)``) when
# the daemon used those creds for read-only queries.
# ──────────────────────────────────────────────────────────────────────


class TestGerritClientPrewarmFromEnv:

    def setup_method(self):
        GerritClient.invalidate_prewarm_cache()

    def teardown_method(self):
        GerritClient.invalidate_prewarm_cache()

    def _set_settings(self, monkeypatch, **kw):
        """Helper to override settings.gerrit_* + git_ssh_key_path."""
        from backend.config import settings
        for k, v in kw.items():
            monkeypatch.setattr(settings, k, v, raising=False)

    def test_prewarm_uses_settings_when_present(self, monkeypatch):
        """When settings.gerrit_ssh_host etc. are populated, prewarm
        builds the cached row from them rather than calling pick_default."""
        import asyncio
        from backend import git_credentials

        # Sabotage pick_default + registry to make sure they're NOT called.
        async def _boom(*a, **kw):
            raise AssertionError(
                "prewarm_for_daemon called the DB path — OP-1193 regression"
            )
        monkeypatch.setattr(git_credentials, "pick_default", _boom)
        monkeypatch.setattr(
            git_credentials, "get_credential_registry_async", _boom,
        )

        self._set_settings(
            monkeypatch,
            gerrit_ssh_host="claude-bot@sora.services",
            gerrit_ssh_port=29418,
            git_ssh_key_path="/home/user/.config/omnisight/gerrit-claude-bot-ed25519",
            gerrit_project="omnisight/OmniSight-Productizer",
        )

        asyncio.run(GerritClient.prewarm_for_daemon())

        assert GerritClient._cached_default is not None
        cached = GerritClient._cached_default
        assert cached["id"] == "daemon-env"
        assert cached["platform"] == "gerrit"
        assert cached["ssh_host"] == "claude-bot@sora.services"
        assert cached["ssh_port"] == 29418
        assert cached["ssh_key"] == "/home/user/.config/omnisight/gerrit-claude-bot-ed25519"
        assert cached["project"] == "omnisight/OmniSight-Productizer"
        assert cached["is_default"] is True
        assert GerritClient._cached_registry == [cached]

    def test_prewarm_falls_back_to_env_when_settings_empty(self, monkeypatch):
        """If settings fields are empty strings, env vars are consulted."""
        import asyncio
        self._set_settings(
            monkeypatch,
            gerrit_ssh_host="",
            gerrit_ssh_port=0,
            git_ssh_key_path="",
            gerrit_project="",
        )
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_HOST", "claude-bot@env-host")
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_PORT", "29999")
        monkeypatch.setenv("OMNISIGHT_GIT_SSH_KEY_PATH", "/tmp/fake-key")
        monkeypatch.setenv("OMNISIGHT_GERRIT_PROJECT", "env/Project")

        asyncio.run(GerritClient.prewarm_for_daemon())

        cached = GerritClient._cached_default
        assert cached is not None
        assert cached["ssh_host"] == "claude-bot@env-host"
        assert cached["ssh_port"] == 29999
        assert cached["ssh_key"] == "/tmp/fake-key"
        assert cached["project"] == "env/Project"

    def test_prewarm_empty_ssh_host_logs_warn_and_leaves_cache_empty(
        self, monkeypatch, caplog,
    ):
        """Without an SSH host, prewarm warns and leaves cache None.
        The daemon then falls back to the async DB path on demand."""
        import asyncio
        import logging
        self._set_settings(
            monkeypatch, gerrit_ssh_host="", gerrit_ssh_port=0,
            git_ssh_key_path="", gerrit_project="",
        )
        monkeypatch.delenv("OMNISIGHT_GERRIT_SSH_HOST", raising=False)
        monkeypatch.delenv("OMNISIGHT_GERRIT_SSH_PORT", raising=False)
        monkeypatch.delenv("OMNISIGHT_GIT_SSH_KEY_PATH", raising=False)
        monkeypatch.delenv("OMNISIGHT_GERRIT_PROJECT", raising=False)

        with caplog.at_level(logging.WARNING, logger="backend.gerrit"):
            asyncio.run(GerritClient.prewarm_for_daemon())

        assert GerritClient._cached_default is None
        assert GerritClient._cached_registry is None
        assert any(
            "no SSH host configured" in rec.message
            for rec in caplog.records
        ), f"expected warning log; got {[r.message for r in caplog.records]}"

    def test_prewarm_default_port_when_not_specified(self, monkeypatch):
        """Default port 29418 when no port set in settings or env."""
        import asyncio
        self._set_settings(
            monkeypatch,
            gerrit_ssh_host="x@y", gerrit_ssh_port=0,
            git_ssh_key_path="", gerrit_project="",
        )
        monkeypatch.delenv("OMNISIGHT_GERRIT_SSH_PORT", raising=False)

        asyncio.run(GerritClient.prewarm_for_daemon())

        cached = GerritClient._cached_default
        assert cached is not None
        assert cached["ssh_port"] == 29418

    def test_prewarm_settings_dominate_env_when_both_present(self, monkeypatch):
        """Settings field takes precedence over env (matches the
        legacy default-gerrit shim's settings-first ordering)."""
        import asyncio
        self._set_settings(
            monkeypatch,
            gerrit_ssh_host="settings@host",
            gerrit_ssh_port=29418,
            git_ssh_key_path="",
            gerrit_project="",
        )
        monkeypatch.setenv("OMNISIGHT_GERRIT_SSH_HOST", "env@host")

        asyncio.run(GerritClient.prewarm_for_daemon())

        cached = GerritClient._cached_default
        assert cached["ssh_host"] == "settings@host"

    def test_ssh_args_for_cached_account_produces_correct_argv(
        self, monkeypatch, tmp_path,
    ):
        """The cached row's _ssh_args_for should produce argv that matches
        what an operator would type to ssh manually: ssh -i <key> -p 29418
        -o opts <user@host>."""
        import asyncio
        key_file = tmp_path / "fake-key"
        key_file.write_text("# stub")
        self._set_settings(
            monkeypatch,
            gerrit_ssh_host="claude-bot@sora.services",
            gerrit_ssh_port=29418,
            git_ssh_key_path=str(key_file),
            gerrit_project="omnisight/OmniSight-Productizer",
        )

        asyncio.run(GerritClient.prewarm_for_daemon())

        client = GerritClient()
        cached = client._resolve_account_from_cache("")
        assert cached is not None
        argv = client._ssh_args_for(cached)
        # Must produce a valid ssh command targeting user@host with the key.
        assert argv[0] == "ssh"
        assert "-i" in argv
        assert str(key_file) in argv
        assert "-p" in argv
        assert "29418" in argv
        assert "claude-bot@sora.services" in argv
        assert "BatchMode=yes" in argv
        assert "StrictHostKeyChecking=accept-new" in argv

    def test_no_db_calls_in_prewarm_path(self, monkeypatch):
        """OP-1193 regression: the new prewarm must not import from
        backend.git_credentials at all. (The old impl did.)"""
        import asyncio
        from backend import git_credentials

        get_called = {"flag": False}
        pick_called = {"flag": False}

        async def _watch_registry(*a, **kw):
            get_called["flag"] = True
            return []

        async def _watch_pick(*a, **kw):
            pick_called["flag"] = True
            return None

        monkeypatch.setattr(
            git_credentials, "get_credential_registry_async", _watch_registry,
        )
        monkeypatch.setattr(git_credentials, "pick_default", _watch_pick)
        self._set_settings(
            monkeypatch,
            gerrit_ssh_host="x@y", gerrit_ssh_port=29418,
            git_ssh_key_path="/tmp/k", gerrit_project="p",
        )

        asyncio.run(GerritClient.prewarm_for_daemon())

        assert not get_called["flag"], "prewarm must not call get_credential_registry_async"
        assert not pick_called["flag"], "prewarm must not call pick_default"
