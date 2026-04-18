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
        assert "TEST" in content
