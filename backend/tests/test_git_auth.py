"""Tests for backend/git_auth.py — platform detection and auth env."""

from backend.git_auth import detect_platform, parse_repo_path, get_gitlab_api_url, get_auth_env


class TestDetectPlatform:

    def test_github_https(self):
        assert detect_platform("https://github.com/org/repo.git") == "github"

    def test_github_ssh(self):
        assert detect_platform("git@github.com:org/repo.git") == "github"

    def test_gitlab_https(self):
        assert detect_platform("https://gitlab.com/org/repo.git") == "gitlab"

    def test_gitlab_ssh(self):
        assert detect_platform("git@gitlab.com:org/repo.git") == "gitlab"

    def test_self_hosted_gitlab_https(self):
        assert detect_platform("https://gitlab.company.com/org/repo.git") == "gitlab"

    def test_self_hosted_gitlab_ssh(self):
        assert detect_platform("git@gitlab.myhost.io:team/project.git") == "gitlab"

    def test_unknown_host(self):
        assert detect_platform("https://bitbucket.org/org/repo.git") == "unknown"

    def test_local_path(self):
        assert detect_platform("/home/user/repos/project") == "unknown"


class TestParseRepoPath:

    def test_https_github(self):
        assert parse_repo_path("https://github.com/org/repo.git") == "org/repo"

    def test_https_no_dotgit(self):
        assert parse_repo_path("https://github.com/org/repo") == "org/repo"

    def test_ssh_github(self):
        assert parse_repo_path("git@github.com:org/repo.git") == "org/repo"

    def test_ssh_gitlab(self):
        assert parse_repo_path("git@gitlab.com:team/project.git") == "team/project"

    def test_nested_path(self):
        assert parse_repo_path("https://gitlab.com/group/sub/repo.git") == "group/sub/repo"


class TestGetGitlabApiUrl:

    def test_gitlab_com_ssh(self):
        assert get_gitlab_api_url("git@gitlab.com:org/repo.git") == "https://gitlab.com"

    def test_self_hosted_https(self):
        url = get_gitlab_api_url("https://gitlab.company.com/org/repo.git")
        assert url == "https://gitlab.company.com"

    def test_self_hosted_ssh(self):
        url = get_gitlab_api_url("git@git.internal.io:team/proj.git")
        assert url == "https://git.internal.io"


class TestGetAuthEnv:

    def test_ssh_url_gets_ssh_command(self):
        env = get_auth_env("git@github.com:org/repo.git")
        # Should set GIT_SSH_COMMAND if key exists
        if "GIT_SSH_COMMAND" in env:
            assert "ssh -i" in env["GIT_SSH_COMMAND"]
            assert "StrictHostKeyChecking" in env["GIT_SSH_COMMAND"]

    def test_https_url_no_token_empty_env(self):
        # Without tokens configured, HTTPS should return empty or minimal env
        env = get_auth_env("https://github.com/org/repo.git")
        assert "GIT_OMNISIGHT_TOKEN" not in env or env["GIT_OMNISIGHT_TOKEN"] == ""

    def test_local_path_empty_env(self):
        env = get_auth_env("/home/user/repos/project")
        assert env == {}
