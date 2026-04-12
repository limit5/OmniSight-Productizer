"""Tests for External → Internal webhook sync + CI/CD triggers (Phase 26)."""

import hashlib
import hmac
import json

import pytest


class TestGitHubWebhook:

    @pytest.mark.asyncio
    async def test_unconfigured_returns_503(self, client):
        resp = await client.post("/api/v1/webhooks/github", content=b"{}")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client):
        from backend.config import settings
        settings.github_webhook_secret = "test-secret"
        try:
            resp = await client.post(
                "/api/v1/webhooks/github",
                content=b'{"action":"opened"}',
                headers={"X-Hub-Signature-256": "sha256=invalid"},
            )
            assert resp.status_code == 401
        finally:
            settings.github_webhook_secret = ""

    @pytest.mark.asyncio
    async def test_valid_signature_accepted(self, client):
        from backend.config import settings
        secret = "test-secret-123"
        settings.github_webhook_secret = secret
        try:
            body = json.dumps({"action": "closed", "issue": {"html_url": "https://github.com/test/1", "state": "closed"}}).encode()
            sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            resp = await client.post(
                "/api/v1/webhooks/github",
                content=body,
                headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"},
            )
            assert resp.status_code == 200
        finally:
            settings.github_webhook_secret = ""


class TestGitLabWebhook:

    @pytest.mark.asyncio
    async def test_unconfigured_returns_503(self, client):
        resp = await client.post("/api/v1/webhooks/gitlab", json={})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, client):
        from backend.config import settings
        settings.gitlab_webhook_secret = "gl-secret"
        try:
            resp = await client.post(
                "/api/v1/webhooks/gitlab", json={},
                headers={"X-Gitlab-Token": "wrong"},
            )
            assert resp.status_code == 401
        finally:
            settings.gitlab_webhook_secret = ""


class TestJiraWebhook:

    @pytest.mark.asyncio
    async def test_unconfigured_returns_503(self, client):
        resp = await client.post("/api/v1/webhooks/jira", json={})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_missing_bearer_returns_401(self, client):
        from backend.config import settings
        settings.jira_webhook_secret = "jira-secret"
        try:
            resp = await client.post("/api/v1/webhooks/jira", json={})
            assert resp.status_code == 401
        finally:
            settings.jira_webhook_secret = ""


class TestSyncDebounce:

    @pytest.mark.asyncio
    async def test_find_task_by_issue_url(self):
        from backend.routers.webhooks import _find_task_by_issue_url
        # With no tasks, should return None
        result = _find_task_by_issue_url("https://github.com/test/1")
        assert result is None


class TestCIConfig:

    def test_ci_config_defaults(self):
        from backend.config import settings
        assert settings.ci_github_actions_enabled is False
        assert settings.ci_jenkins_enabled is False
        assert settings.ci_gitlab_enabled is False

    def test_webhook_secret_defaults(self):
        from backend.config import settings
        assert settings.github_webhook_secret == ""
        assert settings.gitlab_webhook_secret == ""
        assert settings.jira_webhook_secret == ""


class TestTaskSyncFields:

    def test_task_has_platform_field(self):
        from backend.models import Task
        t = Task(id="t1", title="test", external_issue_platform="github")
        assert t.external_issue_platform == "github"

    def test_task_has_sync_timestamp(self):
        from backend.models import Task
        t = Task(id="t1", title="test", last_external_sync_at="2026-01-01T00:00:00")
        assert t.last_external_sync_at is not None
