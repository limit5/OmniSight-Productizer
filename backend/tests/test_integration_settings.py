"""Tests for System Integration Settings (Phase 34)."""

import pytest


class TestSettingsEndpoint:

    @pytest.mark.asyncio
    async def test_get_settings(self, client):
        resp = await client.get("/api/v1/system/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "llm" in data
        assert "git" in data
        assert "gerrit" in data
        assert "jira" in data
        assert "slack" in data
        assert "docker" in data

    @pytest.mark.asyncio
    async def test_settings_masks_tokens(self, client):
        resp = await client.get("/api/v1/system/settings")
        data = resp.json()
        # Tokens should be masked or empty
        git_token = data["git"]["github_token"]
        assert git_token == "" or "***" in git_token

    @pytest.mark.asyncio
    async def test_update_settings_valid(self, client):
        resp = await client.put("/api/v1/system/settings", json={
            "updates": {"llm_temperature": 0.5}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_temperature" in data["applied"]

    @pytest.mark.asyncio
    async def test_update_settings_rejected(self, client):
        resp = await client.put("/api/v1/system/settings", json={
            "updates": {"dangerous_field": "hack"}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "dangerous_field" in data["rejected"]

    @pytest.mark.asyncio
    async def test_update_empty(self, client):
        resp = await client.put("/api/v1/system/settings", json={
            "updates": {}
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_webhooks_block_reports_four_secret_states(
        self, client, monkeypatch
    ):
        """B14 Part D row 234 — the Webhooks tab UI drives its per-field
        status dots off the ``webhooks`` block of ``GET /system/settings``,
        which must carry a "configured"/"" flag for GitHub, GitLab,
        Gerrit, AND Jira. Gerrit is the newly-added row — regressing to
        the prior three-key shape would break the frontend's rotate-only
        Gerrit indicator without any compile-time signal.
        """
        from backend import config as _cfg

        monkeypatch.setattr(_cfg.settings, "github_webhook_secret", "gh-secret")
        monkeypatch.setattr(_cfg.settings, "gitlab_webhook_secret", "")
        monkeypatch.setattr(_cfg.settings, "gerrit_webhook_secret", "ger-secret")
        monkeypatch.setattr(_cfg.settings, "jira_webhook_secret", "")
        resp = await client.get("/api/v1/system/settings")
        assert resp.status_code == 200
        webhooks = resp.json()["webhooks"]
        # All four keys present — contract for the Tab 3 status dots.
        assert set(webhooks.keys()) >= {
            "github_secret", "gitlab_secret", "gerrit_secret", "jira_secret",
        }
        # Mapping: truthy → "configured"; empty → "".
        assert webhooks["github_secret"] == "configured"
        assert webhooks["gitlab_secret"] == ""
        assert webhooks["gerrit_secret"] == "configured"
        assert webhooks["jira_secret"] == ""
        # Plaintext of the actual secret must NEVER leak — the frontend is
        # explicitly designed around the "configured"/"" contract, so a
        # regression that echoes the raw value would both violate the
        # rotate-only invariant and break the status-dot derivation.
        for v in webhooks.values():
            assert "gh-secret" not in v
            assert "ger-secret" not in v

    @pytest.mark.asyncio
    async def test_ci_block_reports_jenkins_api_token_without_leaking(
        self, client, monkeypatch
    ):
        """B14 Part D row 235 — the CI/CD tab drives the Jenkins section
        status dot off the ``ci.jenkins_api_token`` key of ``GET
        /system/settings``. Contract matches the webhooks block: a truthy
        backend secret surfaces as "configured", empty surfaces as ""; the
        plaintext must never leak (Jenkins API tokens are as sensitive as
        webhook secrets — they authenticate pipeline trigger POSTs).

        Jenkins URL + user are NOT secrets, so they remain plaintext (the
        URL is the operator-facing base URL of the Jenkins server and the
        user is just a username). `github_actions_enabled` /
        `jenkins_enabled` / `gitlab_ci_enabled` remain booleans so the
        front-end toggle reflects reality.
        """
        from backend import config as _cfg

        monkeypatch.setattr(_cfg.settings, "ci_github_actions_enabled", True)
        monkeypatch.setattr(_cfg.settings, "ci_jenkins_enabled", True)
        monkeypatch.setattr(
            _cfg.settings, "ci_jenkins_url", "https://jenkins.example.com"
        )
        monkeypatch.setattr(_cfg.settings, "ci_jenkins_user", "ci-bot")
        monkeypatch.setattr(
            _cfg.settings, "ci_jenkins_api_token", "jtok-do-not-leak"
        )
        monkeypatch.setattr(_cfg.settings, "ci_gitlab_enabled", False)
        resp = await client.get("/api/v1/system/settings")
        assert resp.status_code == 200
        ci = resp.json()["ci"]
        # All six keys that the Tab 4 UI reads are present.
        assert set(ci.keys()) >= {
            "github_actions_enabled", "jenkins_enabled", "jenkins_url",
            "jenkins_user", "jenkins_api_token", "gitlab_ci_enabled",
        }
        # Booleans flow through untouched — operator UI mirrors reality.
        assert ci["github_actions_enabled"] is True
        assert ci["jenkins_enabled"] is True
        assert ci["gitlab_ci_enabled"] is False
        # URL + user are not secrets → plaintext.
        assert ci["jenkins_url"] == "https://jenkins.example.com"
        assert ci["jenkins_user"] == "ci-bot"
        # API token collapses to the "configured"/"" contract — and the raw
        # plaintext is never echoed back anywhere in the payload.
        assert ci["jenkins_api_token"] == "configured"
        for v in ci.values():
            if isinstance(v, str):
                assert "jtok-do-not-leak" not in v

    @pytest.mark.asyncio
    async def test_ci_jenkins_api_token_empty_surfaces_empty_string(
        self, client, monkeypatch
    ):
        """B14 Part D row 235 — empty Jenkins API token must surface as ""
        (not "configured"), otherwise the Tab 4 Jenkins status dot would
        lie green on an un-wired pipeline. Isolates the empty-state
        regression separate from the "configured" branch above.
        """
        from backend import config as _cfg

        monkeypatch.setattr(_cfg.settings, "ci_jenkins_api_token", "")
        resp = await client.get("/api/v1/system/settings")
        assert resp.status_code == 200
        ci = resp.json()["ci"]
        assert ci["jenkins_api_token"] == ""


class TestConnectionEndpoints:

    @pytest.mark.asyncio
    async def test_test_ssh(self, client):
        resp = await client.post("/api/v1/system/test/ssh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "error", "not_configured")

    @pytest.mark.asyncio
    async def test_test_gerrit(self, client):
        resp = await client.post("/api/v1/system/test/gerrit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "error", "not_configured")

    @pytest.mark.asyncio
    async def test_test_github(self, client):
        resp = await client.post("/api/v1/system/test/github")
        assert resp.status_code == 200
        # Without token: not_configured
        assert resp.json()["status"] in ("ok", "error", "not_configured")

    @pytest.mark.asyncio
    async def test_test_jira(self, client):
        resp = await client.post("/api/v1/system/test/jira")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_test_slack(self, client):
        resp = await client.post("/api/v1/system/test/slack")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_integration(self, client):
        resp = await client.post("/api/v1/system/test/nonexistent")
        assert resp.status_code == 400


class TestGitForgeTokenProbe:
    """B14 Part A row 3 — non-mutating probe for candidate Git-forge tokens."""

    @pytest.mark.asyncio
    async def test_rejects_unknown_provider(self, client):
        resp = await client.post(
            "/api/v1/system/git-forge/test-token",
            json={"provider": "bitbucket", "token": "whatever"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_token_returns_error(self, client):
        resp = await client.post(
            "/api/v1/system/git-forge/test-token",
            json={"provider": "github", "token": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "required" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_does_not_mutate_settings_on_failure(self, client):
        """Probing with a bad token must NOT overwrite settings.github_token."""
        from backend.config import settings
        before = settings.github_token
        await client.post(
            "/api/v1/system/git-forge/test-token",
            json={"provider": "github", "token": "ghp_obviously_not_valid_xxx"},
        )
        assert settings.github_token == before

    @pytest.mark.asyncio
    async def test_gerrit_empty_ssh_host_returns_error(self, client):
        resp = await client.post(
            "/api/v1/system/git-forge/test-token",
            json={"provider": "gerrit", "ssh_host": "", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "ssh host" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_gerrit_rejects_invalid_port(self):
        """A port outside 1-65535 must surface an error before ssh fires."""
        from backend.routers import integration as ir

        result = await ir._probe_gerrit_ssh("merger-agent-bot@host.example", 70000)
        assert result["status"] == "error"
        assert "port" in result["message"].lower()

        result_zero = await ir._probe_gerrit_ssh("merger-agent-bot@host.example", 0)
        assert result_zero["status"] == "error"

    @pytest.mark.asyncio
    async def test_gerrit_does_not_mutate_settings_on_failure(self, client):
        """Probing with a bad SSH endpoint must NOT overwrite Gerrit settings."""
        from backend.config import settings
        before_enabled = settings.gerrit_enabled
        before_host = settings.gerrit_ssh_host
        before_port = settings.gerrit_ssh_port
        before_url = settings.gerrit_url
        await client.post(
            "/api/v1/system/git-forge/test-token",
            json={
                "provider": "gerrit",
                "ssh_host": "nobody@gerrit.invalid.example",
                "ssh_port": 29418,
                "url": "https://gerrit.invalid.example",
            },
        )
        assert settings.gerrit_enabled == before_enabled
        assert settings.gerrit_ssh_host == before_host
        assert settings.gerrit_ssh_port == before_port
        assert settings.gerrit_url == before_url

    @pytest.mark.asyncio
    async def test_gerrit_ok_path_parses_version(self, monkeypatch):
        """With the ssh subprocess mocked, the OK path surfaces the Gerrit version."""
        from backend.routers import integration as ir

        class _StubProc:
            returncode = 0

            async def communicate(self):
                return b"gerrit version 3.9.2\n", b""

        async def _fake_exec(*_args, **_kwargs):
            return _StubProc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)
        result = await ir._probe_gerrit_ssh(
            "merger-agent-bot@gerrit.example", 29418, "https://gerrit.example",
        )
        assert result["status"] == "ok"
        assert result["version"] == "3.9.2"
        assert result["ssh_host"] == "merger-agent-bot@gerrit.example"
        assert result["ssh_port"] == 29418
        assert result["url"] == "https://gerrit.example"

    @pytest.mark.asyncio
    async def test_gerrit_ssh_failure_bubbles_stderr(self, monkeypatch):
        """SSH failures (bad key, connection refused) must surface as status=error."""
        from backend.routers import integration as ir

        class _StubProc:
            returncode = 255

            async def communicate(self):
                return b"", b"Permission denied (publickey).\n"

        async def _fake_exec(*_args, **_kwargs):
            return _StubProc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)
        result = await ir._probe_gerrit_ssh("nobody@gerrit.example", 29418)
        assert result["status"] == "error"
        assert "Permission denied" in result["message"]

    @pytest.mark.asyncio
    async def test_gerrit_probe_reads_from_request_not_settings(self, monkeypatch):
        """The probe must hit the endpoint from the request body, never settings."""
        from backend.routers import integration as ir

        captured: dict = {}

        class _StubProc:
            returncode = 0

            async def communicate(self):
                return b"gerrit version 3.10.0\n", b""

        async def _fake_exec(*args, **_kwargs):
            captured["args"] = args
            return _StubProc()

        # Point settings at something bogus to prove we ignore it.
        from backend.config import settings
        monkeypatch.setattr(settings, "gerrit_ssh_host", "should-not-be-used.example")
        monkeypatch.setattr(settings, "gerrit_ssh_port", 12345)
        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        result = await ir._probe_gerrit_ssh("bot@fresh.example", 29418)
        assert result["status"] == "ok"
        assert "fresh.example" in " ".join(str(a) for a in captured["args"])
        assert "should-not-be-used" not in " ".join(str(a) for a in captured["args"])

    @pytest.mark.asyncio
    async def test_gitlab_empty_token_returns_error(self, client):
        resp = await client.post(
            "/api/v1/system/git-forge/test-token",
            json={"provider": "gitlab", "token": "", "url": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "required" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_gitlab_rejects_malformed_url(self):
        """A URL without http(s):// must surface an error before curl fires."""
        from backend.routers import integration as ir

        result = await ir._probe_gitlab_token("glpat-fake", "gitlab.example.com")
        assert result["status"] == "error"
        assert "http" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_gitlab_does_not_mutate_settings_on_failure(self, client):
        """Probing with a bad token must NOT overwrite settings.gitlab_token."""
        from backend.config import settings
        before_token = settings.gitlab_token
        before_url = settings.gitlab_url
        await client.post(
            "/api/v1/system/git-forge/test-token",
            json={
                "provider": "gitlab",
                "token": "glpat-obviously-not-valid",
                "url": "https://gitlab.example.com",
            },
        )
        assert settings.gitlab_token == before_token
        assert settings.gitlab_url == before_url

    @pytest.mark.asyncio
    async def test_gitlab_ok_path_parses_version(self, monkeypatch):
        """With the curl subprocess mocked, the OK path surfaces version/url."""
        from backend.routers import integration as ir

        class _StubProc:
            returncode = 0

            async def communicate(self):
                return b'{"version": "16.7.0-ee", "revision": "abc1234"}', b""

        async def _fake_exec(*_args, **_kwargs):
            return _StubProc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)
        result = await ir._probe_gitlab_token(
            "glpat-fake", "https://gitlab.example.com",
        )
        assert result["status"] == "ok"
        assert result["version"] == "16.7.0-ee"
        assert result["revision"] == "abc1234"
        assert result["url"] == "https://gitlab.example.com"

    @pytest.mark.asyncio
    async def test_gitlab_defaults_to_gitlab_com_when_url_blank(self, monkeypatch):
        """Blank URL should resolve to https://gitlab.com in the probe result."""
        from backend.routers import integration as ir

        class _StubProc:
            returncode = 0

            async def communicate(self):
                return b'{"version": "16.7.0"}', b""

        async def _fake_exec(*_args, **_kwargs):
            return _StubProc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)
        result = await ir._probe_gitlab_token("glpat-fake", "")
        assert result["status"] == "ok"
        assert result["url"] == "https://gitlab.com"

    @pytest.mark.asyncio
    async def test_gitlab_error_response_bubbles_message(self, monkeypatch):
        """GitLab 401/403 JSON error bodies must surface as status=error."""
        from backend.routers import integration as ir

        class _StubProc:
            returncode = 0

            async def communicate(self):
                return b'{"message": "401 Unauthorized"}', b""

        async def _fake_exec(*_args, **_kwargs):
            return _StubProc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)
        result = await ir._probe_gitlab_token("glpat-wrong", "https://gitlab.com")
        assert result["status"] == "error"
        assert "401" in result["message"]

    @pytest.mark.asyncio
    async def test_github_ok_path_parses_login(self, monkeypatch):
        """With the curl subprocess mocked, the OK path surfaces login/name/scopes."""
        from backend.routers import integration as ir

        class _StubProc:
            returncode = 0

            async def communicate(self):
                body = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"X-OAuth-Scopes: repo, read:org\r\n"
                    b"Content-Type: application/json\r\n"
                    b"\r\n"
                    b'{"login": "octocat", "name": "The Octocat"}'
                )
                return body, b""

        async def _fake_exec(*_args, **_kwargs):
            return _StubProc()

        monkeypatch.setattr(
            ir.asyncio, "create_subprocess_exec", _fake_exec
        )
        result = await ir._probe_github_token("ghp_fake")
        assert result["status"] == "ok"
        assert result["user"] == "octocat"
        assert result["name"] == "The Octocat"
        assert "repo" in result["scopes"]


class TestGitForgeSshPubkey:
    """B14 Part C row 223 — Gerrit Setup Wizard Step 2 (public key surface)."""

    @pytest.mark.asyncio
    async def test_returns_public_key_and_fingerprint(
        self, client, monkeypatch, tmp_path
    ):
        """With a real .pub file on disk, the endpoint surfaces the key
        line, the SHA256 fingerprint (best-effort), and the resolved path."""
        from backend.config import settings
        from backend.routers import integration as ir

        key_path = tmp_path / "id_ed25519"
        pub_path = tmp_path / "id_ed25519.pub"
        pub_path.write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISAMPLE merger-agent-bot@omnisight\n"
        )

        class _KeygenProc:
            returncode = 0

            async def communicate(self):
                return (
                    b"256 SHA256:abcdef0123456789 merger-agent-bot@omnisight (ED25519)\n",
                    b"",
                )

        async def _fake_exec(*_args, **_kwargs):
            return _KeygenProc()

        monkeypatch.setattr(settings, "git_ssh_key_path", str(key_path))
        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.get("/api/v1/system/git-forge/ssh-pubkey")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["public_key"].startswith("ssh-ed25519 AAAAC3")
        assert data["key_path"] == str(pub_path)
        assert data["key_type"] == "ssh-ed25519"
        assert data["comment"] == "merger-agent-bot@omnisight"
        assert data["fingerprint"] == "SHA256:abcdef0123456789"

    @pytest.mark.asyncio
    async def test_accepts_pub_path_directly(
        self, client, monkeypatch, tmp_path
    ):
        """If git_ssh_key_path is already a .pub path, use it as-is rather
        than appending another .pub suffix."""
        from backend.config import settings
        from backend.routers import integration as ir

        pub_path = tmp_path / "custom_key.pub"
        pub_path.write_text("ssh-rsa AAAAB3 operator@host\n")

        async def _noop_keygen(*_args, **_kwargs):
            raise FileNotFoundError("ssh-keygen not available in this test")

        monkeypatch.setattr(settings, "git_ssh_key_path", str(pub_path))
        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _noop_keygen)

        resp = await client.get("/api/v1/system/git-forge/ssh-pubkey")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["key_path"] == str(pub_path)
        assert data["key_type"] == "ssh-rsa"
        # ssh-keygen failure leaves fingerprint blank but doesn't fail the call.
        assert data["fingerprint"] == ""

    @pytest.mark.asyncio
    async def test_missing_public_key_returns_error(
        self, client, monkeypatch, tmp_path
    ):
        """Point git_ssh_key_path at a location with no .pub → error + hint."""
        from backend.config import settings

        missing = tmp_path / "no_such_key"
        monkeypatch.setattr(settings, "git_ssh_key_path", str(missing))

        resp = await client.get("/api/v1/system/git-forge/ssh-pubkey")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "not found" in data["message"].lower()
        assert data["key_path"] == str(missing) + ".pub"

    @pytest.mark.asyncio
    async def test_unconfigured_path_returns_error(self, client, monkeypatch):
        """An empty git_ssh_key_path must surface a configuration error, not crash."""
        from backend.config import settings
        monkeypatch.setattr(settings, "git_ssh_key_path", "")

        resp = await client.get("/api/v1/system/git-forge/ssh-pubkey")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "not configured" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_never_returns_private_key(
        self, client, monkeypatch, tmp_path
    ):
        """The endpoint must surface only the .pub content — never the
        private key — even when both files exist side-by-side."""
        from backend.config import settings
        from backend.routers import integration as ir

        key_path = tmp_path / "id_ed25519"
        pub_path = tmp_path / "id_ed25519.pub"
        private_marker = "-----BEGIN OPENSSH PRIVATE KEY-----\nDO-NOT-LEAK-ME\n-----END OPENSSH PRIVATE KEY-----\n"
        key_path.write_text(private_marker)
        pub_path.write_text("ssh-ed25519 AAAAPUBLIC merger@omnisight\n")

        class _KeygenProc:
            returncode = 0

            async def communicate(self):
                return b"256 SHA256:zzzz merger@omnisight (ED25519)\n", b""

        async def _fake_exec(*_args, **_kwargs):
            return _KeygenProc()

        monkeypatch.setattr(settings, "git_ssh_key_path", str(key_path))
        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.get("/api/v1/system/git-forge/ssh-pubkey")
        assert resp.status_code == 200
        body = resp.text
        assert "DO-NOT-LEAK-ME" not in body
        assert "BEGIN OPENSSH PRIVATE KEY" not in body
        assert "AAAAPUBLIC" in body


class TestGerritBotVerify:
    """B14 Part C row 224 — Gerrit Setup Wizard Step 3 (``merger-agent-bot``
    group verification).

    The endpoint shells out to
    ``ssh -p {port} {host} gerrit ls-members {group}``. These tests stub
    the subprocess so no real SSH call is made; the assertions focus on
    (1) the ``ok`` path surfacing the member list, (2) configuration gaps
    (empty group / missing group) collapsing to ``status=error`` with a
    useful message, and (3) input validation for host / port.
    """

    @pytest.mark.asyncio
    async def test_verifies_group_and_returns_members(
        self, client, monkeypatch
    ):
        """Happy path: Gerrit prints a tab-separated table → parser
        surfaces member_count + username list for the UI."""
        from backend.routers import integration as ir

        class _Proc:
            returncode = 0

            async def communicate(self):
                # Gerrit ls-members output format (tab separated).
                out = (
                    b"id\tusername\tfull name\temail\n"
                    b"1000001\tmerger-agent-bot\tMerger Agent"
                    b"\tmerger-agent-bot@svc.omnisight.internal\n"
                )
                return out, b""

        captured = {}

        async def _fake_exec(*args, **_kwargs):
            captured["args"] = args
            return _Proc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-bot",
            json={"ssh_host": "gerrit.example.com", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["group"] == "merger-agent-bot"
        assert data["member_count"] == 1
        assert data["members"][0]["username"] == "merger-agent-bot"
        assert data["members"][0]["email"].endswith(
            "@svc.omnisight.internal"
        )
        # Confirm we actually invoked `gerrit ls-members merger-agent-bot`.
        assert "ls-members" in captured["args"]
        assert "merger-agent-bot" in captured["args"]

    @pytest.mark.asyncio
    async def test_empty_group_surfaces_configuration_error(
        self, client, monkeypatch
    ):
        """Group exists but has zero members → `status=error` with a
        pointer to `gerrit set-members` so the operator can fix it."""
        from backend.routers import integration as ir

        class _Proc:
            returncode = 0

            async def communicate(self):
                # Header only — group exists but is empty.
                return b"id\tusername\tfull name\temail\n", b""

        async def _fake_exec(*_args, **_kwargs):
            return _Proc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-bot",
            json={"ssh_host": "gerrit.example.com", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert data["member_count"] == 0
        assert data["members"] == []
        assert "no members" in data["message"].lower()
        assert "set-members" in data["message"]

    @pytest.mark.asyncio
    async def test_missing_group_surfaces_gerrit_stderr(
        self, client, monkeypatch
    ):
        """`gerrit ls-members` exits nonzero if the group is not found
        → surface Gerrit's own message (first 300 chars)."""
        from backend.routers import integration as ir

        class _Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"fatal: Group Not Found : merger-agent-bot\n"

        async def _fake_exec(*_args, **_kwargs):
            return _Proc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-bot",
            json={"ssh_host": "gerrit.example.com", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "group not found" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_ssh_host_rejected_before_subprocess(
        self, client, monkeypatch
    ):
        """Empty ssh_host must short-circuit — no subprocess is spawned
        (otherwise a blank host would blow up with an opaque `ssh` error)."""
        from backend.routers import integration as ir

        called = {"count": 0}

        async def _fake_exec(*_args, **_kwargs):
            called["count"] += 1

            class _P:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return _P()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-bot",
            json={"ssh_host": "", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "host is required" in data["message"].lower()
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_invalid_port_rejected(self, client, monkeypatch):
        """Out-of-range ssh_port must surface the range error without
        attempting the ssh call."""
        from backend.routers import integration as ir

        async def _fake_exec(*_args, **_kwargs):  # pragma: no cover — should not fire
            raise AssertionError("subprocess should not be spawned for invalid port")

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-bot",
            json={"ssh_host": "gerrit.example.com", "ssh_port": 99999},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "between 1 and 65535" in data["message"]

    @pytest.mark.asyncio
    async def test_custom_group_passed_through(
        self, client, monkeypatch
    ):
        """Allow the operator to probe a non-default group name (e.g.
        `ai-reviewer-bots`) so Step 3 UI can be reused for follow-ups."""
        from backend.routers import integration as ir

        captured = {}

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (
                    b"id\tusername\tfull name\temail\n"
                    b"1000001\tlint-bot\tLint Bot\tlint@svc\n",
                    b"",
                )

        async def _fake_exec(*args, **_kwargs):
            captured["args"] = args
            return _Proc()

        monkeypatch.setattr(ir.asyncio, "create_subprocess_exec", _fake_exec)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-bot",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "group": "ai-reviewer-bots",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["group"] == "ai-reviewer-bots"
        # The subprocess should have been invoked with the custom group.
        assert "ai-reviewer-bots" in captured["args"]
        assert "merger-agent-bot" not in captured["args"]


class TestGerritSubmitRuleVerify:
    """B14 Part C row 225 — Gerrit Setup Wizard Step 4 (submit-rule 驗證).

    The endpoint fetches ``refs/meta/config:project.config`` over the
    Gerrit SSH transport (``git fetch`` + ``git show``) and looks for the
    three dual-+2 ACL fragments. Tests stub ``_fetch_gerrit_project_config``
    instead of stubbing three chained subprocesses — the probe's
    pattern-matching is the load-bearing logic here, and the fetch
    helper is covered indirectly by the subprocess-arg assertions.
    """

    _GOOD_CONFIG = """
[project]
    description = Test.

[access "refs/heads/*"]
    label-Code-Review = -2..+2 group ai-reviewer-bots
    label-Code-Review = -2..+2 group non-ai-reviewer
    submit = group non-ai-reviewer

[label "Code-Review"]
    function = NoBlock
"""

    _MISSING_SUBMIT_CONFIG = """
[access "refs/heads/*"]
    label-Code-Review = -2..+2 group ai-reviewer-bots
    label-Code-Review = -2..+2 group non-ai-reviewer
"""

    _MISSING_HUMANS_CONFIG = """
[access "refs/heads/*"]
    label-Code-Review = -2..+2 group ai-reviewer-bots
    submit = group non-ai-reviewer
"""

    _COMMENTED_RULE_CONFIG = """
[access "refs/heads/*"]
#    label-Code-Review = -2..+2 group ai-reviewer-bots
#    label-Code-Review = -2..+2 group non-ai-reviewer
#    submit = group non-ai-reviewer
"""

    @pytest.mark.asyncio
    async def test_happy_path_all_three_checks_pass(self, client, monkeypatch):
        """project.config declares all three ACL fragments → status=ok
        and every check surfaces `ok=True` so the wizard can flip READY."""
        from backend.routers import integration as ir

        async def _fake_fetch(host, port, project):
            return (0, self._GOOD_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "project": "omnisight-productizer",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["project"] == "omnisight-productizer"
        assert data["missing"] == []
        check_ids = {c["id"] for c in data["checks"]}
        assert check_ids == {
            "ai_reviewers_can_vote",
            "humans_can_vote",
            "submit_gated_to_humans",
        }
        assert all(c["ok"] for c in data["checks"])

    @pytest.mark.asyncio
    async def test_missing_submit_gate_surfaces_per_check(
        self, client, monkeypatch
    ):
        """project.config with vote grants but no `submit = group non-ai-reviewer`
        must flag `submit_gated_to_humans` as failing — that's the
        load-bearing fence per CLAUDE.md Safety Rules."""
        from backend.routers import integration as ir

        async def _fake_fetch(host, port, project):
            return (0, self._MISSING_SUBMIT_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "project": "omnisight-productizer",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert data["missing"] == ["submit_gated_to_humans"]
        assert "submit" in data["message"].lower()
        # The other two should be explicitly ok — UI renders them green.
        by_id = {c["id"]: c for c in data["checks"]}
        assert by_id["ai_reviewers_can_vote"]["ok"] is True
        assert by_id["humans_can_vote"]["ok"] is True
        assert by_id["submit_gated_to_humans"]["ok"] is False

    @pytest.mark.asyncio
    async def test_missing_human_grant_surfaces_per_check(
        self, client, monkeypatch
    ):
        """Missing the `non-ai-reviewer` Code-Review grant → `humans_can_vote`
        flagged. Humans literally cannot cast the hard-gate +2 without it."""
        from backend.routers import integration as ir

        async def _fake_fetch(host, port, project):
            return (0, self._MISSING_HUMANS_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "project": "omnisight-productizer",
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "humans_can_vote" in data["missing"]

    @pytest.mark.asyncio
    async def test_commented_out_rule_is_not_a_match(
        self, client, monkeypatch
    ):
        """Comment-scrubbing guards against a stale `.example` config
        landing on refs/meta/config with every rule commented out."""
        from backend.routers import integration as ir

        async def _fake_fetch(host, port, project):
            return (0, self._COMMENTED_RULE_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "project": "omnisight-productizer",
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert set(data["missing"]) == {
            "ai_reviewers_can_vote",
            "humans_can_vote",
            "submit_gated_to_humans",
        }

    @pytest.mark.asyncio
    async def test_git_fetch_failure_is_surfaced_verbatim(
        self, client, monkeypatch
    ):
        """When git fetch fails (missing ref, auth, …) the probe returns
        the stderr so the operator can debug — we truncate to 300 chars
        to avoid spamming the UI with a full stack trace."""
        from backend.routers import integration as ir

        async def _fake_fetch(host, port, project):
            return (
                1,
                "",
                "fatal: Couldn't find remote ref refs/meta/config",
            )

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "project": "omnisight-productizer",
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "couldn't find remote ref" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_missing_ssh_host_short_circuits(self, client, monkeypatch):
        """Empty ssh_host must reject before any subprocess spawns, for
        symmetry with Step 1 / Step 3 validation."""
        from backend.routers import integration as ir

        called = {"count": 0}

        async def _fake_fetch(*_args, **_kwargs):
            called["count"] += 1
            return (0, self._GOOD_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "",
                "ssh_port": 29418,
                "project": "omnisight-productizer",
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "host is required" in data["message"].lower()
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_missing_project_short_circuits(self, client, monkeypatch):
        """Empty project name must reject — an empty Gerrit project would
        expand to `ssh://host:port/` which is a legal URL but meaningless."""
        from backend.routers import integration as ir

        called = {"count": 0}

        async def _fake_fetch(*_args, **_kwargs):
            called["count"] += 1
            return (0, self._GOOD_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 29418,
                "project": "",
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "project is required" in data["message"].lower()
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_malicious_project_name_rejected(
        self, client, monkeypatch
    ):
        """Project names with shell metacharacters / path traversal must
        be rejected with a friendly error — `create_subprocess_exec`
        already neutralises shell injection, but the explicit regex gives
        operators a better message than an opaque git fetch failure."""
        from backend.routers import integration as ir

        called = {"count": 0}

        async def _fake_fetch(*_args, **_kwargs):
            called["count"] += 1
            return (0, self._GOOD_CONFIG, "")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        for bad in (
            "../omnisight-productizer",
            "/etc/passwd",
            "project; rm -rf /",
            "project name with spaces",
        ):
            resp = await client.post(
                "/api/v1/system/git-forge/gerrit/verify-submit-rule",
                json={
                    "ssh_host": "gerrit.example.com",
                    "ssh_port": 29418,
                    "project": bad,
                },
            )
            data = resp.json()
            assert data["status"] == "error", f"should reject {bad!r}"
            assert called["count"] == 0, f"fetch should not fire for {bad!r}"

    @pytest.mark.asyncio
    async def test_invalid_port_rejected(self, client, monkeypatch):
        """Symmetric with Step 3's port validation."""
        from backend.routers import integration as ir

        async def _fake_fetch(*_args, **_kwargs):  # pragma: no cover — must not fire
            raise AssertionError("fetch should not spawn for invalid port")

        monkeypatch.setattr(ir, "_fetch_gerrit_project_config", _fake_fetch)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/verify-submit-rule",
            json={
                "ssh_host": "gerrit.example.com",
                "ssh_port": 99999,
                "project": "omnisight-productizer",
            },
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "between 1 and 65535" in data["message"]


class TestGitTokenMapEndpoint:
    """B14 Part B row 217 — masked GET/PUT of the multi-instance token map."""

    @pytest.mark.asyncio
    async def test_get_returns_empty_lists_when_unset(self, client, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "github_token_map", "")
        monkeypatch.setattr(settings, "gitlab_token_map", "")
        resp = await client.get("/api/v1/system/settings/git/token-map")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"github": [], "gitlab": []}

    @pytest.mark.asyncio
    async def test_get_masks_tokens(self, client, monkeypatch):
        import json
        from backend.config import settings
        monkeypatch.setattr(
            settings, "github_token_map",
            json.dumps({
                "github.enterprise.com": "ghp_aaaaaaaaaaaaaaaaaaaaaa",
                "github.acme.example": "ghp_bbbbbbbbbbbbbbbbbbbbbb",
            }),
        )
        monkeypatch.setattr(
            settings, "gitlab_token_map",
            json.dumps({"https://gitlab.example.com": "glpat-xxxxxxxxxxxxxxxxxx"}),
        )
        resp = await client.get("/api/v1/system/settings/git/token-map")
        assert resp.status_code == 200
        data = resp.json()
        # Stable (sorted) ordering
        assert [e["host"] for e in data["github"]] == [
            "github.acme.example", "github.enterprise.com",
        ]
        for entry in data["github"] + data["gitlab"]:
            token = entry["token_masked"]
            assert token  # non-empty
            # Raw secret must never appear in the masked field
            assert not token.startswith("ghp_a"), token
            assert not token.startswith("ghp_b"), token
            assert "xxxxxxxx" not in token
        # Shape is platform-tagged for UI grouping
        assert all(e["platform"] == "github" for e in data["github"])
        assert all(e["platform"] == "gitlab" for e in data["gitlab"])

    @pytest.mark.asyncio
    async def test_get_tolerates_malformed_json(self, client, monkeypatch):
        """Corrupt JSON in settings should not 500 the endpoint — it just
        surfaces an empty list so the operator can PUT a fresh map."""
        from backend.config import settings
        monkeypatch.setattr(settings, "github_token_map", "not-json{{")
        monkeypatch.setattr(settings, "gitlab_token_map", "[1, 2, 3]")  # wrong shape
        resp = await client.get("/api/v1/system/settings/git/token-map")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"github": [], "gitlab": []}

    @pytest.mark.asyncio
    async def test_put_writes_json_maps_and_masks_response(self, client, monkeypatch):
        import json
        from backend.config import settings
        monkeypatch.setattr(settings, "github_token_map", "")
        monkeypatch.setattr(settings, "gitlab_token_map", "")

        resp = await client.put(
            "/api/v1/system/settings/git/token-map",
            json={
                "github": [
                    {"host": "github.enterprise.com", "token": "ghp_enterprise_secret_value_zzz"},
                ],
                "gitlab": [
                    {"host": "https://gitlab.example.com", "token": "glpat-self-hosted-secret-zz"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        # Response body never contains the raw token
        for platform in ("github", "gitlab"):
            assert len(data[platform]) == 1
            masked = data[platform][0]["token_masked"]
            assert "enterprise_secret" not in masked
            assert "self-hosted-secret" not in masked

        # Settings now hold canonical JSON form
        gh_parsed = json.loads(settings.github_token_map)
        gl_parsed = json.loads(settings.gitlab_token_map)
        assert gh_parsed == {
            "github.enterprise.com": "ghp_enterprise_secret_value_zzz",
        }
        assert gl_parsed == {
            "https://gitlab.example.com": "glpat-self-hosted-secret-zz",
        }

    @pytest.mark.asyncio
    async def test_put_empty_lists_clears_both_maps(self, client, monkeypatch):
        import json
        from backend.config import settings
        monkeypatch.setattr(
            settings, "github_token_map",
            json.dumps({"stale.example.com": "ghp_should_be_cleared_xxxxx"}),
        )
        monkeypatch.setattr(settings, "gitlab_token_map", "")

        resp = await client.put(
            "/api/v1/system/settings/git/token-map",
            json={"github": [], "gitlab": []},
        )
        assert resp.status_code == 200
        # Empty maps serialise to "" (the idiomatic unset value) — not "{}"
        assert settings.github_token_map == ""
        assert settings.gitlab_token_map == ""

    @pytest.mark.asyncio
    async def test_put_blank_token_preserves_existing(self, client, monkeypatch):
        """The masked GET returns '***' instead of real tokens, so the UI
        cannot round-trip the secret. A PUT with a blank token for a known
        host must preserve the stored token rather than overwrite it with
        the mask or an empty string."""
        import json
        from backend.config import settings
        monkeypatch.setattr(
            settings, "github_token_map",
            json.dumps({"github.enterprise.com": "ghp_original_value_preserve_me_123"}),
        )
        monkeypatch.setattr(settings, "gitlab_token_map", "")

        resp = await client.put(
            "/api/v1/system/settings/git/token-map",
            json={
                "github": [{"host": "github.enterprise.com", "token": ""}],
                "gitlab": [],
            },
        )
        assert resp.status_code == 200
        parsed = json.loads(settings.github_token_map)
        assert parsed == {
            "github.enterprise.com": "ghp_original_value_preserve_me_123",
        }

    @pytest.mark.asyncio
    async def test_put_blank_token_drops_brand_new_host(self, client, monkeypatch):
        """A brand-new host submitted with a blank token is silently
        dropped rather than stored with an empty token (which would break
        every credential lookup for that host)."""
        from backend.config import settings
        monkeypatch.setattr(settings, "github_token_map", "")
        monkeypatch.setattr(settings, "gitlab_token_map", "")

        resp = await client.put(
            "/api/v1/system/settings/git/token-map",
            json={
                "github": [{"host": "brand.new.example", "token": ""}],
                "gitlab": [],
            },
        )
        assert resp.status_code == 200
        assert settings.github_token_map == ""
        assert resp.json()["github"] == []

    @pytest.mark.asyncio
    async def test_put_ignores_blank_host_entries(self, client, monkeypatch):
        """A blank host in the payload must be skipped — otherwise an
        empty-string key would collide with every JSON lookup keyed by
        hostname (and the UI wouldn't render it anyway)."""
        from backend.config import settings
        monkeypatch.setattr(settings, "github_token_map", "")
        monkeypatch.setattr(settings, "gitlab_token_map", "")

        resp = await client.put(
            "/api/v1/system/settings/git/token-map",
            json={
                "github": [
                    {"host": "", "token": "ghp_orphan_should_be_dropped"},
                    {"host": "   ", "token": "ghp_whitespace_also_dropped"},
                ],
                "gitlab": [],
            },
        )
        assert resp.status_code == 200
        assert settings.github_token_map == ""

    @pytest.mark.asyncio
    async def test_put_invalidates_credential_cache(self, client, monkeypatch):
        """After a PUT, find_credential_for_url must see the new map
        without a process restart."""
        from backend.config import settings
        from backend import git_credentials as gc

        monkeypatch.setattr(settings, "github_token_map", "")
        monkeypatch.setattr(settings, "gitlab_token_map", "")
        monkeypatch.setattr(settings, "github_token", "")
        monkeypatch.setattr(settings, "gitlab_token", "")
        monkeypatch.setattr(settings, "git_credentials_file", "")
        gc.clear_credential_cache()

        # Seed the cache so the next read is cached
        _ = gc.get_credential_registry()

        await client.put(
            "/api/v1/system/settings/git/token-map",
            json={
                "github": [
                    {"host": "github.fresh.example", "token": "ghp_fresh_cache_bust_value"},
                ],
                "gitlab": [],
            },
        )
        entry = gc.find_credential_for_url("https://github.fresh.example/foo/bar.git")
        assert entry is not None
        assert entry["token"] == "ghp_fresh_cache_bust_value"

    @pytest.mark.asyncio
    async def test_put_last_write_wins_on_duplicate_host(self, client, monkeypatch):
        """Duplicate hosts in a single PUT body merge last-write-wins."""
        import json
        from backend.config import settings
        monkeypatch.setattr(settings, "github_token_map", "")
        monkeypatch.setattr(settings, "gitlab_token_map", "")

        resp = await client.put(
            "/api/v1/system/settings/git/token-map",
            json={
                "github": [
                    {"host": "github.enterprise.com", "token": "ghp_first_entry_token_aaa"},
                    {"host": "github.enterprise.com", "token": "ghp_second_entry_token_bbb"},
                ],
                "gitlab": [],
            },
        )
        assert resp.status_code == 200
        parsed = json.loads(settings.github_token_map)
        assert parsed == {
            "github.enterprise.com": "ghp_second_entry_token_bbb",
        }


class TestVendorSDKCRUD:

    @pytest.mark.asyncio
    async def test_create_vendor_sdk(self, client):
        resp = await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-vendor-crud",
            "label": "Test Vendor",
            "vendor_id": "test-v",
            "toolchain": "aarch64-linux-gnu-gcc",
            "cross_prefix": "aarch64-linux-gnu-",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"
        # Cleanup
        await client.delete("/api/v1/system/vendor/sdks/test-vendor-crud")

    @pytest.mark.asyncio
    async def test_create_duplicate_rejected(self, client):
        await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-dup", "label": "Dup", "vendor_id": "dup",
        })
        resp = await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-dup", "label": "Dup2", "vendor_id": "dup2",
        })
        assert resp.status_code == 409
        await client.delete("/api/v1/system/vendor/sdks/test-dup")

    @pytest.mark.asyncio
    async def test_delete_vendor_sdk(self, client):
        await client.post("/api/v1/system/vendor/sdks", json={
            "platform": "test-del", "label": "Del", "vendor_id": "del",
        })
        resp = await client.delete("/api/v1/system/vendor/sdks/test-del")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_builtin_blocked(self, client):
        resp = await client.delete("/api/v1/system/vendor/sdks/aarch64")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/api/v1/system/vendor/sdks/nonexistent-xyz")
        assert resp.status_code == 404


class TestMaskFunction:

    def test_mask_short(self):
        from backend.routers.integration import _mask
        assert _mask("abc") == "***"
        assert _mask("") == ""

    def test_mask_long(self):
        from backend.routers.integration import _mask
        result = _mask("ghp_abcdefghijklmnop")
        assert result.startswith("ghp")
        assert result.endswith("nop")
        assert "***" in result or "*" in result

    def test_updatable_fields_whitelist(self):
        from backend.routers.integration import _UPDATABLE_FIELDS
        assert "llm_provider" in _UPDATABLE_FIELDS
        assert "gerrit_enabled" in _UPDATABLE_FIELDS
        assert "notification_jira_url" in _UPDATABLE_FIELDS
        # Dangerous fields should NOT be updatable
        assert "app_name" not in _UPDATABLE_FIELDS


class TestComponentExists:

    def test_integration_settings_component(self):
        from pathlib import Path
        comp = Path(__file__).resolve().parent.parent.parent / "components" / "omnisight" / "integration-settings.tsx"
        assert comp.exists()
        content = comp.read_text()
        assert "IntegrationSettings" in content
        assert "SettingsButton" in content


class TestGerritWebhookInfo:
    """B14 Part C row 226 — Gerrit Setup Wizard Step 5 (webhook 設定引導).

    Endpoints:
      * ``GET  /api/v1/system/git-forge/gerrit/webhook-info`` — masked
        view of the inbound webhook URL + secret status.
      * ``POST /api/v1/system/git-forge/gerrit/webhook-secret/generate``
        — mints a fresh ``settings.gerrit_webhook_secret`` and returns
        the plain value exactly once.

    Tests cover (a) URL derivation from base_url + ``X-Forwarded-*``
    headers (cloudflared deploy), (b) secret masking (never returns the
    plain value on GET), (c) idempotent generate that always rotates,
    (d) the rotated value actually persists into ``settings``.
    """

    @pytest.mark.asyncio
    async def test_get_webhook_info_unconfigured(self, client, monkeypatch):
        """Empty ``gerrit_webhook_secret`` → ``secret_configured=False``
        and ``secret_masked=""`` so the wizard surfaces the Generate CTA."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_webhook_secret", "")
        resp = await client.get("/api/v1/system/git-forge/gerrit/webhook-info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["secret_configured"] is False
        assert data["secret_masked"] == ""
        # URL is the load-bearing pasteable string — Gerrit needs it verbatim.
        assert data["webhook_url"].endswith("/api/v1/webhooks/gerrit")
        assert data["signature_header"] == "X-Gerrit-Signature"
        assert data["signature_algorithm"] == "hmac-sha256"
        assert "patchset-created" in data["event_types"]
        assert "comment-added" in data["event_types"]
        assert "change-merged" in data["event_types"]

    @pytest.mark.asyncio
    async def test_get_webhook_info_configured_masks_secret(
        self, client, monkeypatch
    ):
        """Configured secret → ``secret_configured=True`` and only a
        masked preview is returned. The plain value MUST NOT appear in
        the response body — re-revealing on every GET defeats the
        rotation surface."""
        from backend.config import settings as _s
        plain = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        monkeypatch.setattr(_s, "gerrit_webhook_secret", plain)
        resp = await client.get("/api/v1/system/git-forge/gerrit/webhook-info")
        data = resp.json()
        assert data["secret_configured"] is True
        assert data["secret_masked"] != plain
        assert plain not in resp.text  # belt + braces — body never contains plain
        # Mask preserves first 4 + last 4 — operator can cross-check w/o leak.
        assert data["secret_masked"].startswith("abcd")
        assert data["secret_masked"].endswith("89-_")

    @pytest.mark.asyncio
    async def test_get_webhook_info_honours_x_forwarded_headers(
        self, client, monkeypatch
    ):
        """Cloudflared / nginx terminates HTTPS upstream and sets
        ``X-Forwarded-Proto`` / ``X-Forwarded-Host``. Without these the
        URL would be ``http://test/...`` (the test-client's base) which
        is not the URL Gerrit can reach. Honouring the headers gives the
        operator the externally-routable URL."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_webhook_secret", "")
        resp = await client.get(
            "/api/v1/system/git-forge/gerrit/webhook-info",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "omnisight.example.com",
            },
        )
        data = resp.json()
        assert (
            data["webhook_url"]
            == "https://omnisight.example.com/api/v1/webhooks/gerrit"
        )

    @pytest.mark.asyncio
    async def test_generate_webhook_secret_mints_persists_returns_once(
        self, client, monkeypatch
    ):
        """POST /generate: (1) returns a high-entropy plain secret in the
        response body exactly once, (2) persists it onto
        ``settings.gerrit_webhook_secret`` so the inbound webhook
        verifier picks it up, (3) the matching GET surfaces the new
        secret only as a masked preview (no plain re-read)."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_webhook_secret", "")
        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/webhook-secret/generate"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        secret = data["secret"]
        # token_urlsafe(32) → ~43 chars URL-safe base64 → ≥256 bits entropy.
        assert isinstance(secret, str)
        assert len(secret) >= 32
        # Persisted into settings — the webhook verifier reads from here.
        assert _s.gerrit_webhook_secret == secret
        # Subsequent GET masks it, never re-reveals.
        info = await client.get("/api/v1/system/git-forge/gerrit/webhook-info")
        info_body = info.json()
        assert info_body["secret_configured"] is True
        assert info_body["secret_masked"] != secret
        assert secret not in info.text

    @pytest.mark.asyncio
    async def test_generate_rotates_existing_secret(self, client, monkeypatch):
        """Two generates back-to-back must produce two different secrets
        — generate is the rotate primitive, never a no-op."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_webhook_secret", "")
        first = await client.post(
            "/api/v1/system/git-forge/gerrit/webhook-secret/generate"
        )
        second = await client.post(
            "/api/v1/system/git-forge/gerrit/webhook-secret/generate"
        )
        assert first.json()["secret"] != second.json()["secret"]
        # Settings holds the *latest* — the older secret is invalidated.
        assert _s.gerrit_webhook_secret == second.json()["secret"]

    @pytest.mark.asyncio
    async def test_generate_response_carries_paste_ready_metadata(
        self, client, monkeypatch
    ):
        """Generate returns webhook_url + signature_header + algorithm so
        the wizard can render the Gerrit ``[remote "omnisight"]`` config
        snippet without a second round-trip to ``webhook-info``."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_webhook_secret", "")
        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/webhook-secret/generate",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "omnisight.example.com",
            },
        )
        data = resp.json()
        assert (
            data["webhook_url"]
            == "https://omnisight.example.com/api/v1/webhooks/gerrit"
        )
        assert data["signature_header"] == "X-Gerrit-Signature"
        assert data["signature_algorithm"] == "hmac-sha256"
        assert "secret_masked" in data
        # Note must reinforce "save now, no re-reveal" — operators
        # routinely close wizards before pasting, so this copy is
        # load-bearing UX (verified separately in the frontend tests).
        assert "not be shown again" in data["note"].lower() or \
               "save this value" in data["note"].lower()

    def test_mask_secret_short_input_full_mask(self):
        """Inputs ≤8 chars (degenerate / dev placeholder) get fully
        masked rather than leaking 8/n of the secret."""
        from backend.routers.integration import _mask_secret
        assert _mask_secret("") == ""
        assert _mask_secret("short") == "*****"
        assert _mask_secret("12345678") == "********"

    def test_mask_secret_long_input_keeps_prefix_and_suffix(self):
        from backend.routers.integration import _mask_secret
        masked = _mask_secret("abcdefghijklmnopqrstuvwxyz")
        assert masked.startswith("abcd")
        assert masked.endswith("wxyz")
        assert "…" in masked  # ellipsis in middle, no plain leak

    def test_derive_webhook_url_falls_back_to_base_url(self):
        """When no X-Forwarded-* headers are present, the helper
        falls back to ``Request.base_url`` so direct-to-backend
        deployments (no proxy) still get a usable URL."""
        from backend.routers.integration import _derive_webhook_url
        from starlette.requests import Request as StarletteRequest

        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("api.internal.example.com", 8000),
            "path": "/api/v1/system/git-forge/gerrit/webhook-info",
            "headers": [],
            "root_path": "",
            "query_string": b"",
        }
        req = StarletteRequest(scope)
        url = _derive_webhook_url(req)
        assert url.endswith("/api/v1/webhooks/gerrit")
        assert "api.internal.example.com" in url


@pytest.fixture
def reset_rate_limiter():
    """Reset the in-process rate-limit bucket before the test.

    The IP-bucket persists across tests in the shared in-memory
    ``InMemoryLimiter`` (singleton), so a long ``test_integration_settings``
    session accumulates requests and starts returning 429 partway
    through. Tests that fire several POSTs back-to-back use this to
    flush the bucket and isolate themselves.
    """
    from backend.rate_limit import get_limiter
    get_limiter().clear()
    yield
    get_limiter().clear()


class TestGerritFinalize:
    """B14 Part C row 227 — Gerrit Setup Wizard finalize endpoint.

    ``POST /api/v1/system/git-forge/gerrit/finalize`` is the wizard's
    closing act: it takes the SSH endpoint / REST URL / project values
    the operator already validated through Steps 1–5 and writes them
    atomically into ``settings.gerrit_*`` while flipping
    ``gerrit_enabled = true``. Without this endpoint the wizard would
    leave a half-configured Gerrit (webhook secret persisted by Step 5
    but the master switch never on).

    Tests cover (a) the happy path enables the integration and echoes
    back the persisted config, (b) input validation rejects empty
    ssh_host and out-of-range ports, (c) optional fields default to
    empty strings, (d) the success message matches the wizard's
    expected「Gerrit 整合已啟用」copy, (e) the response never echoes
    the plain webhook secret (only configured/not).
    """

    @pytest.mark.asyncio
    async def test_finalize_enables_and_persists(self, client, monkeypatch, reset_rate_limiter):
        """Happy path: posting the wizard inputs flips
        ``gerrit_enabled`` on, persists every gerrit_* field, and
        returns ``status=ok`` + the localised success banner copy."""
        from backend.config import settings as _s
        # Reset to a known-disabled baseline so we can prove the flip.
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        monkeypatch.setattr(_s, "gerrit_url", "")
        monkeypatch.setattr(_s, "gerrit_ssh_host", "")
        monkeypatch.setattr(_s, "gerrit_ssh_port", 29418)
        monkeypatch.setattr(_s, "gerrit_project", "")
        monkeypatch.setattr(_s, "gerrit_replication_targets", "")
        monkeypatch.setattr(_s, "gerrit_webhook_secret", "")

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={
                "url": "https://gerrit.example.com",
                "ssh_host": "merger-agent-bot@gerrit.example.com",
                "ssh_port": 29418,
                "project": "project/omnisight-core",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["enabled"] is True
        # Localised confirmation copy matches the UI banner verbatim —
        # the frontend test asserts the same string is rendered.
        assert data["message"] == "Gerrit 整合已啟用"
        assert _s.gerrit_enabled is True
        assert _s.gerrit_url == "https://gerrit.example.com"
        assert _s.gerrit_ssh_host == "merger-agent-bot@gerrit.example.com"
        assert _s.gerrit_ssh_port == 29418
        assert _s.gerrit_project == "project/omnisight-core"
        # Echo carries the post-write snapshot for the UI summary panel.
        cfg = data["config"]
        assert cfg["url"] == "https://gerrit.example.com"
        assert cfg["ssh_host"] == "merger-agent-bot@gerrit.example.com"
        assert cfg["ssh_port"] == 29418
        assert cfg["project"] == "project/omnisight-core"
        assert cfg["webhook_secret_configured"] is False

    @pytest.mark.asyncio
    async def test_finalize_trims_whitespace(self, client, monkeypatch, reset_rate_limiter):
        """Trailing whitespace from copy-paste should not leak into
        ``settings.*`` — the wizard inputs sometimes pick up trailing
        newlines from the operator's clipboard."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        monkeypatch.setattr(_s, "gerrit_ssh_host", "")
        monkeypatch.setattr(_s, "gerrit_url", "")
        monkeypatch.setattr(_s, "gerrit_project", "")

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={
                "url": "  https://gerrit.example.com\n",
                "ssh_host": "  bot@gerrit.example.com  ",
                "ssh_port": 29418,
                "project": "  project/omnisight-core  ",
            },
        )
        assert resp.status_code == 200
        assert _s.gerrit_url == "https://gerrit.example.com"
        assert _s.gerrit_ssh_host == "bot@gerrit.example.com"
        assert _s.gerrit_project == "project/omnisight-core"

    @pytest.mark.asyncio
    async def test_finalize_rejects_empty_ssh_host(self, client, monkeypatch, reset_rate_limiter):
        """ssh_host is the load-bearing field (Step 1 pivots on it).
        An empty value would leave Gerrit unreachable even though
        ``gerrit_enabled`` is true — refuse with HTTP 400 rather than
        write a half-broken config."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={"ssh_host": "   ", "ssh_port": 29418},
        )
        assert resp.status_code == 400
        # The integration must not have been enabled by a rejected call.
        assert _s.gerrit_enabled is False

    @pytest.mark.asyncio
    async def test_finalize_rejects_invalid_port(self, client, monkeypatch, reset_rate_limiter):
        """Out-of-range SSH port is a config-day footgun (e.g. 0 or
        99999 from a bad copy-paste). Refuse with HTTP 400 rather than
        write garbage that would make every subsequent Gerrit call
        fail with a confusing 'connection refused'."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        for bad_port in (0, -1, 65536, 99999):
            resp = await client.post(
                "/api/v1/system/git-forge/gerrit/finalize",
                json={"ssh_host": "bot@gerrit.example.com", "ssh_port": bad_port},
            )
            assert resp.status_code == 400, f"port={bad_port} should be rejected"
        assert _s.gerrit_enabled is False

    @pytest.mark.asyncio
    async def test_finalize_optional_fields_default_to_empty(
        self, client, monkeypatch, reset_rate_limiter
    ):
        """SSH-only Gerrit installs have no REST URL; single-instance
        installs have no replication targets. Both are valid — only
        ssh_host + ssh_port are mandatory."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        monkeypatch.setattr(_s, "gerrit_url", "preexisting")
        monkeypatch.setattr(_s, "gerrit_project", "preexisting")
        monkeypatch.setattr(_s, "gerrit_replication_targets", "preexisting")

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={"ssh_host": "bot@gerrit.example.com", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        # Missing fields collapse to empty strings (not None, not the
        # prior value) — finalize is a replace, not a patch.
        assert _s.gerrit_url == ""
        assert _s.gerrit_project == ""
        assert _s.gerrit_replication_targets == ""

    @pytest.mark.asyncio
    async def test_finalize_response_reports_webhook_secret_status(
        self, client, monkeypatch, reset_rate_limiter
    ):
        """The config echo must report whether Step 5 already set a
        webhook secret — the wizard uses this to surface a follow-up
        nudge if the operator skipped the Generate button. Critically,
        the plain secret is NEVER echoed (Step 5 generate is the
        one-and-only reveal)."""
        from backend.config import settings as _s
        plain = "VERY_SECRET_TOKEN_DO_NOT_LEAK_1234567890"
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        monkeypatch.setattr(_s, "gerrit_webhook_secret", plain)

        resp = await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={"ssh_host": "bot@gerrit.example.com", "ssh_port": 29418},
        )
        assert resp.status_code == 200
        body = resp.text
        # Belt + braces: the plain secret must NOT appear anywhere in
        # the response — even masked. Re-revealing on every finalize
        # would defeat Step 5's rotation surface.
        assert plain not in body
        assert resp.json()["config"]["webhook_secret_configured"] is True

    @pytest.mark.asyncio
    async def test_finalize_idempotent_overwrites(self, client, monkeypatch, reset_rate_limiter):
        """Re-running finalize with new values overwrites — operators
        will edit the wizard inputs and re-finalize when they realise
        they typed the wrong project. No 409 / no merge — just replace."""
        from backend.config import settings as _s
        monkeypatch.setattr(_s, "gerrit_enabled", False)
        monkeypatch.setattr(_s, "gerrit_ssh_port", 29418)

        # First finalize with one set of values.
        await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={
                "ssh_host": "old@gerrit.example.com",
                "ssh_port": 29418,
                "project": "project/old",
            },
        )
        assert _s.gerrit_ssh_host == "old@gerrit.example.com"
        assert _s.gerrit_project == "project/old"

        # Second finalize replaces.
        resp2 = await client.post(
            "/api/v1/system/git-forge/gerrit/finalize",
            json={
                "ssh_host": "new@gerrit.example.com",
                "ssh_port": 29419,
                "project": "project/new",
            },
        )
        assert resp2.status_code == 200
        assert _s.gerrit_ssh_host == "new@gerrit.example.com"
        assert _s.gerrit_ssh_port == 29419
        assert _s.gerrit_project == "project/new"
        assert _s.gerrit_enabled is True


class TestConnectionResponseShape:
    """B14 Part E row 240 — pin the per-forge probe response shapes that
    the front-end Test Connection buttons rely on. Each probe goes through
    the same dispatcher (``POST /system/test/<integration>``) but the
    metadata it surfaces differs by forge:
      - GitHub returns ``user`` (login) + ``scopes`` (X-OAuth-Scopes)
      - GitLab returns ``version`` + optional ``revision``
      - Jira   returns ``version`` + optional ``server_title``
      - Gerrit returns ``version``
    The Test Connection button renders these values inline, so a regression
    that drops one of them silently degrades the operator UX.

    Placed last in the module — TestGerritWebhookInfo's secret-rotation
    tests are pre-existing-flaky against test ordering, so we keep our
    new tests downstream of them to avoid disturbing the run order they
    were written against. The flakiness is unrelated to this work; see
    the test_generate_* docstrings for the underlying state assumption.
    """

    @pytest.mark.asyncio
    async def test_github_probe_surfaces_login_and_scopes(self, client, monkeypatch):
        """GitHub probe must produce both ``user`` (login) and ``scopes``
        keys — even when scopes is empty — so the front-end can always
        unconditionally render `(login) [scopes: ...]`."""
        from backend.routers import integration as _i

        async def _fake_create_subprocess_exec(*args, **kwargs):
            class _Proc:
                returncode = 0
                async def communicate(self):
                    body = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"X-OAuth-Scopes: repo, workflow\r\n"
                        b"Content-Type: application/json\r\n"
                        b"\r\n"
                        b"{\"login\": \"octocat\"}"
                    )
                    return body, b""
            return _Proc()

        monkeypatch.setattr(_i.settings, "github_token", "ghp_dummy")
        monkeypatch.setattr(
            _i.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
        )
        resp = await client.post("/api/v1/system/test/github")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["user"] == "octocat"
        assert data["scopes"] == "repo, workflow"

    @pytest.mark.asyncio
    async def test_gitlab_probe_surfaces_version(self, client, monkeypatch):
        """GitLab probe must hit /api/v4/version (not /user) so the
        operator sees the GitLab instance version after pressing TEST."""
        from backend.routers import integration as _i

        called_url = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            called_url["url"] = args[-1]

            class _Proc:
                returncode = 0
                async def communicate(self):
                    return b'{"version": "16.7.0", "revision": "abc1234"}', b""
            return _Proc()

        monkeypatch.setattr(_i.settings, "gitlab_token", "glpat_dummy")
        monkeypatch.setattr(_i.settings, "gitlab_url", "https://gitlab.example.com/")
        monkeypatch.setattr(
            _i.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
        )
        resp = await client.post("/api/v1/system/test/gitlab")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "16.7.0"
        assert called_url["url"].endswith("/api/v4/version")
        assert "/api/v4/user" not in called_url["url"]

    @pytest.mark.asyncio
    async def test_jira_probe_surfaces_version_via_server_info(
        self, client, monkeypatch,
    ):
        """Jira probe must hit /rest/api/2/serverInfo (not /myself) so
        the operator sees the Jira instance version after pressing TEST."""
        from backend.routers import integration as _i

        called_url = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            called_url["url"] = args[-1]

            class _Proc:
                returncode = 0
                async def communicate(self):
                    return (
                        b'{"version": "9.12.5", "serverTitle": "Acme Jira"}',
                        b"",
                    )
            return _Proc()

        monkeypatch.setattr(
            _i.settings, "notification_jira_url", "https://jira.example.com",
        )
        monkeypatch.setattr(_i.settings, "notification_jira_token", "jira_dummy")
        monkeypatch.setattr(
            _i.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
        )
        resp = await client.post("/api/v1/system/test/jira")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "9.12.5"
        assert called_url["url"].endswith("/rest/api/2/serverInfo")
        assert "/myself" not in called_url["url"]
