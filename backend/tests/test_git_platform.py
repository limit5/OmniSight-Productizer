"""Tests for backend/git_platform.py — PR/MR creation logic."""

from backend.git_auth import detect_platform, parse_repo_path, get_gitlab_api_url
from urllib.parse import quote_plus


class TestGitLabUrlConstruction:
    """Verify GitLab API URL and project path construction."""

    def test_gitlab_com_project_encoding(self):
        path = parse_repo_path("https://gitlab.com/my-org/my-project.git")
        assert path == "my-org/my-project"
        encoded = quote_plus(path)
        assert encoded == "my-org%2Fmy-project"

    def test_self_hosted_project_encoding(self):
        path = parse_repo_path("git@gitlab.internal.io:team/sub/repo.git")
        assert path == "team/sub/repo"
        encoded = quote_plus(path)
        assert encoded == "team%2Fsub%2Frepo"

    def test_api_url_construction(self):
        remote = "https://gitlab.company.com/team/project.git"
        api_base = get_gitlab_api_url(remote)
        project_path = parse_repo_path(remote)
        encoded = quote_plus(project_path)
        full_url = f"{api_base}/api/v4/projects/{encoded}/merge_requests"
        assert full_url == "https://gitlab.company.com/api/v4/projects/team%2Fproject/merge_requests"


class TestGitHubUrlConstruction:
    """Verify GitHub repo slug extraction."""

    def test_https_repo_slug(self):
        slug = parse_repo_path("https://github.com/anthropics/claude-code.git")
        assert slug == "anthropics/claude-code"

    def test_ssh_repo_slug(self):
        slug = parse_repo_path("git@github.com:org/repo.git")
        assert slug == "org/repo"


class TestPlatformRouting:
    """Verify correct platform is selected for different URLs."""

    def test_github_routes_to_github(self):
        assert detect_platform("https://github.com/org/repo.git") == "github"
        assert detect_platform("git@github.com:org/repo.git") == "github"

    def test_gitlab_routes_to_gitlab(self):
        assert detect_platform("https://gitlab.com/org/repo.git") == "gitlab"
        assert detect_platform("git@gitlab.com:org/repo.git") == "gitlab"

    def test_self_hosted_gitlab(self):
        assert detect_platform("https://gitlab.mycompany.com/team/repo.git") == "gitlab"
        assert detect_platform("git@gitlab.internal.io:team/repo.git") == "gitlab"
