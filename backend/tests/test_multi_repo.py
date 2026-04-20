"""Tests for Multi-Repo Credential Registry (Phase 43).

Covers:
- Credential registry loading (YAML, JSON maps, scalar fallback)
- Per-host token/SSH key resolution
- Webhook secret lookup
- Settings API credential list
- RepoInfo with platform/authStatus
"""

from __future__ import annotations

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Credential Registry Loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCredentialRegistry:

    def test_empty_registry_no_config(self):
        from backend.git_credentials import get_credential_registry, clear_credential_cache
        clear_credential_cache()
        reg = get_credential_registry()
        assert isinstance(reg, list)

    def test_scalar_fallback_creates_entries(self):
        from unittest.mock import patch
        from backend.git_credentials import get_credential_registry, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = "ghp_test123"
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = "~/.ssh/id_test"
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = ""
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            reg = get_credential_registry()

        assert any(r["id"] == "default-github" for r in reg)
        gh = next(r for r in reg if r["id"] == "default-github")
        assert gh["platform"] == "github"
        assert gh["token"] == "ghp_test123"
        clear_credential_cache()

    def test_json_map_creates_entries(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import get_credential_registry, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = json.dumps({"github.com": "ghp_map1", "github.enterprise.com": "ghp_map2"})
            mock.gitlab_token_map = json.dumps({"gitlab.vendor.com": "glpat_vendor"})
            mock.gerrit_instances = ""
            reg = get_credential_registry()

        assert len(reg) >= 3
        hosts = {r["url"] for r in reg}
        assert "https://github.com" in hosts
        assert "https://github.enterprise.com" in hosts
        assert "https://gitlab.vendor.com" in hosts
        clear_credential_cache()

    def test_cache_works(self):
        from backend.git_credentials import get_credential_registry, clear_credential_cache
        clear_credential_cache()
        r1 = get_credential_registry()
        r2 = get_credential_registry()
        # Should return same data (cached)
        assert len(r1) == len(r2)

    def test_clear_cache(self):
        from backend.git_credentials import get_credential_registry, clear_credential_cache
        get_credential_registry()
        clear_credential_cache()
        from backend import git_credentials
        assert git_credentials._CREDENTIALS_CACHE is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-Host Lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPerHostLookup:

    def test_find_credential_github(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import find_credential_for_url, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = json.dumps({"github.com": "ghp_found"})
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            cred = find_credential_for_url("https://github.com/org/repo.git")

        assert cred is not None
        assert cred["token"] == "ghp_found"
        clear_credential_cache()

    def test_find_credential_ssh_url(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import find_credential_for_url, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = json.dumps({"github.com": "ghp_ssh"})
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            cred = find_credential_for_url("git@github.com:org/repo.git")

        assert cred is not None
        assert cred["token"] == "ghp_ssh"
        clear_credential_cache()

    def test_find_no_match(self):
        from backend.git_credentials import find_credential_for_url, clear_credential_cache
        clear_credential_cache()
        cred = find_credential_for_url("https://unknown-host.com/repo.git")
        assert cred is None
        clear_credential_cache()

    def test_get_token_for_url_with_registry(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import get_token_for_url, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = "ghp_scalar"
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = json.dumps({"github.com": "ghp_registry"})
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            token = get_token_for_url("https://github.com/org/repo")

        # Registry takes priority over scalar
        assert token == "ghp_registry"
        clear_credential_cache()

    def test_get_ssh_key_for_url(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import get_ssh_key_for_url, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = "~/.ssh/default"
            mock.gerrit_enabled = False
            mock.gerrit_ssh_host = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = json.dumps({"github.com": "~/.ssh/id_github"})
            mock.github_token_map = json.dumps({"github.com": "tok"})
            mock.gitlab_token_map = ""
            mock.gerrit_instances = ""
            key = get_ssh_key_for_url("git@github.com:org/repo.git")

        assert key == "~/.ssh/id_github"
        clear_credential_cache()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Webhook Secret Lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWebhookSecretLookup:

    def test_gerrit_secret_from_registry(self):
        import json
        from unittest.mock import patch
        from backend.git_credentials import get_webhook_secret_for_host, clear_credential_cache
        clear_credential_cache()
        with patch("backend.git_credentials.settings") as mock:
            mock.github_token = ""
            mock.gitlab_token = ""
            mock.gitlab_url = ""
            mock.git_ssh_key_path = ""
            mock.gerrit_enabled = True
            mock.gerrit_ssh_host = ""
            mock.gerrit_webhook_secret = "scalar_secret"
            mock.github_webhook_secret = ""
            mock.gitlab_webhook_secret = ""
            mock.git_credentials_file = ""
            mock.git_ssh_key_map = ""
            mock.github_token_map = ""
            mock.gitlab_token_map = ""
            mock.gerrit_instances = json.dumps([
                {"id": "gerrit-vendor", "ssh_host": "gerrit.vendor.com", "webhook_secret": "vendor_secret"}
            ])
            secret = get_webhook_secret_for_host("gerrit.vendor.com", "gerrit")

        assert secret == "vendor_secret"
        clear_credential_cache()

    def test_fallback_to_scalar_secret(self):
        from backend.git_credentials import get_webhook_secret_for_host, clear_credential_cache
        clear_credential_cache()
        # No registry match → fallback
        secret = get_webhook_secret_for_host("unknown.host.com", "github")
        # Should return scalar settings value (empty in test env)
        assert isinstance(secret, str)
        clear_credential_cache()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Settings API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSettingsCredentialList:

    @pytest.mark.asyncio
    async def test_settings_includes_credentials(self, client):
        resp = await client.get("/api/v1/runtime/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "credentials" in data["git"]
        assert isinstance(data["git"]["credentials"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Repos Endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReposEndpoint:

    @pytest.mark.asyncio
    async def test_repos_has_platform_field(self, client):
        resp = await client.get("/api/v1/runtime/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        main = data[0]
        assert "platform" in main
        assert "repoId" in main
        assert "authStatus" in main


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config Fields
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigFields:

    def test_new_config_fields_exist(self):
        from backend.config import settings
        assert hasattr(settings, "git_credentials_file")
        assert hasattr(settings, "git_ssh_key_map")
        assert hasattr(settings, "github_token_map")
        assert hasattr(settings, "gitlab_token_map")
        assert hasattr(settings, "gerrit_instances")

    def test_defaults_are_empty(self):
        from backend.config import settings
        assert settings.git_credentials_file == ""
        assert settings.git_ssh_key_map == ""
        assert settings.github_token_map == ""
        assert settings.gitlab_token_map == ""
        assert settings.gerrit_instances == ""
