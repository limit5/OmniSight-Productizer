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

    def test_ssh_args(self):
        from backend.config import settings
        orig_host = settings.gerrit_ssh_host
        orig_port = settings.gerrit_ssh_port
        try:
            settings.gerrit_ssh_host = "review.example.com"
            settings.gerrit_ssh_port = 29418
            client = GerritClient()
            args = client._ssh_args
            assert "review.example.com" in args
            assert "29418" in args
            assert "BatchMode=yes" in args
        finally:
            settings.gerrit_ssh_host = orig_host
            settings.gerrit_ssh_port = orig_port


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
