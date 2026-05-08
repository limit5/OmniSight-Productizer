"""Contract tests for OP-782 generated-file auto-resolve."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import auto_rebase
from backend.agents.auto_resolve_config import (
    AutoResolveRule,
    load_auto_resolve_config,
)


class _FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.code = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _fake_http_error(req: Any, status: int, body: bytes) -> Any:
    import urllib.error

    class _Body:
        def read(self) -> bytes:
            return body

        def close(self) -> None:
            return None

    raise urllib.error.HTTPError(req.full_url, status, "fake", {}, _Body())


def _change(number: int = 782, *, subject: str = "[OP-782] test") -> dict[str, Any]:
    return {
        "id": f"I{number}",
        "number": number,
        "subject": subject,
        "owner": {"username": "claude-bot"},
        "currentPatchSet": {
            "number": 1,
            "revision": f"rev_{number}",
            "ref": f"refs/changes/{number % 100:02d}/{number}/1",
            "parents": [{"revision": "old"}],
        },
    }


def _ssh_cmd_builder(*remote_args: str) -> list[str]:
    return ["ssh", "fake-host", *remote_args]


def _ssh_env_builder() -> dict[str, str]:
    return {"GIT_SSH_COMMAND": "ssh"}


def test_registry_schema_has_lessons_entry() -> None:
    path = REPO_ROOT / ".gerrit" / "auto-resolve.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert list(raw) == ["auto_resolved_files"]
    rules = load_auto_resolve_config(path)
    assert rules["docs/sop/lessons-learned.md"] == AutoResolveRule(
        path="docs/sop/lessons-learned.md",
        resolver="scripts/build_lessons_index.py",
        description="Index regenerated from docs/sop/lessons/ frontmatter",
    )


def test_duplicate_registry_path_keeps_first_and_warns(tmp_path: Path) -> None:
    cfg = tmp_path / "auto-resolve.yaml"
    cfg.write_text(
        "auto_resolved_files:\n"
        "  - path: docs/sop/lessons-learned.md\n"
        "    resolver: scripts/build_lessons_index.py\n"
        "  - path: docs/sop/lessons-learned.md\n"
        "    resolver: scripts/other.py\n",
        encoding="utf-8",
    )
    logs: list[tuple[str, str, dict[str, Any]]] = []

    rules = load_auto_resolve_config(
        cfg,
        log=lambda level, label, **kw: logs.append((level, label, kw)),
    )

    assert rules["docs/sop/lessons-learned.md"].resolver == "scripts/build_lessons_index.py"
    assert logs[0][1] == "auto_resolve_duplicate_path"


def test_r3_auto_resolves_registered_conflict_and_records_audit(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / ".gerrit" / "auto-resolve.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "auto_resolved_files:\n"
        "  - path: docs/sop/lessons-learned.md\n"
        "    resolver: scripts/build_lessons_index.py\n",
        encoding="utf-8",
    )
    rest_calls: list[str] = []
    notify_calls: list[tuple[str, str]] = []
    audit_rows: list[dict[str, Any]] = []

    def urlopen(req: Any, timeout: int = 30) -> Any:
        rest_calls.append(req.full_url)
        return _fake_http_error(
            req,
            409,
            b"merge conflict in: docs/sop/lessons-learned.md",
        )

    def local_runner(**kwargs: Any) -> auto_rebase.RebaseResult:
        return auto_rebase.RebaseResult(
            change_number="782",
            success=True,
            auto_resolved=("docs/sop/lessons-learned.md",),
            new_revision="local-push",
        )

    sweeper = auto_rebase.AutoRebaseSweeper(
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        run_command=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        urlopen=urlopen,
        load_password=lambda u: "pw",
        notify_jira=lambda key, msg: notify_calls.append((key, msg)),
        audit_recorder=audit_rows.append,
        repo_root=tmp_path,
        local_rebase_runner=local_runner,
    )

    result = sweeper.attempt_rebase(_change(), target_sha="develop-tip")

    assert result.success is True
    assert result.auto_resolved == ("docs/sop/lessons-learned.md",)
    assert len(rest_calls) == 1
    assert notify_calls == [
        (
            "OP-782",
            "[auto-resolve] R3 regenerated docs/sop/lessons-learned.md "
            "via build_lessons_index.py to handle merge conflict against "
            "develop tip develop-tip",
        ),
    ]
    assert audit_rows[0]["change_id"] == "I782"
    assert audit_rows[0]["ps"] == "1"
    assert audit_rows[0]["file"] == "docs/sop/lessons-learned.md"
    assert audit_rows[0]["resolver"] == "scripts/build_lessons_index.py"
    assert audit_rows[0]["resolved_at"]


def test_r3_resolver_failure_falls_back_to_conflict_and_alerts(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / ".gerrit" / "auto-resolve.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "auto_resolved_files:\n"
        "  - path: docs/sop/lessons-learned.md\n"
        "    resolver: scripts/build_lessons_index.py\n",
        encoding="utf-8",
    )
    notify_calls: list[tuple[str, str]] = []

    def urlopen(req: Any, timeout: int = 30) -> Any:
        return _fake_http_error(
            req,
            409,
            b"merge conflict in: docs/sop/lessons-learned.md",
        )

    def local_runner(**kwargs: Any) -> auto_rebase.RebaseResult:
        return auto_rebase.RebaseResult(
            change_number="782",
            conflict=True,
            files=("docs/sop/lessons-learned.md",),
            error="resolver exited 2",
        )

    sweeper = auto_rebase.AutoRebaseSweeper(
        ssh_cmd_builder=_ssh_cmd_builder,
        ssh_env_builder=_ssh_env_builder,
        run_command=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        urlopen=urlopen,
        load_password=lambda u: "pw",
        notify_jira=lambda key, msg: notify_calls.append((key, msg)),
        repo_root=tmp_path,
        local_rebase_runner=local_runner,
    )

    result = sweeper.attempt_rebase(_change(), target_sha="develop-tip")

    assert result.conflict is True
    assert result.files == ("docs/sop/lessons-learned.md",)
    assert notify_calls == [
        (
            "OP-782",
            "[auto-resolve] R3 could not regenerate "
            "docs/sop/lessons-learned.md; falling back to manual conflict "
            "handling. Reason: resolver exited 2",
        ),
    ]


def test_local_lessons_rebase_regenerates_without_conflict_markers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "docs/sop").mkdir(parents=True)
    (repo / "scripts").mkdir()
    lesson_index = repo / "docs/sop/lessons-learned.md"
    lesson_index.write_text("base\n", encoding="utf-8")
    resolver = repo / "scripts/build_lessons_index.py"
    resolver.write_text(
        "from pathlib import Path\n"
        "Path('docs/sop/lessons-learned.md').write_text('generated\\n')\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "ps", base)
    lesson_index.write_text("incoming\n", encoding="utf-8")
    _git(repo, "commit", "-am", "ps edit", "-q")
    ps_rev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/changes/82/782/1", ps_rev)

    _git(repo, "checkout", "-q", "develop")
    lesson_index.write_text("develop\n", encoding="utf-8")
    _git(repo, "commit", "-am", "develop edit", "-q")
    develop_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = auto_rebase.local_rebase_with_resolvers(
        change=_change(),
        target_sha=develop_tip,
        files=("docs/sop/lessons-learned.md",),
        resolvers={
            "docs/sop/lessons-learned.md": AutoResolveRule(
                path="docs/sop/lessons-learned.md",
                resolver="scripts/build_lessons_index.py",
            ),
        },
        repo_root=repo,
    )

    assert result.success is True
    assert result.auto_resolved == ("docs/sop/lessons-learned.md",)
    pushed = _git(
        repo,
        "show",
        "refs/for/develop:docs/sop/lessons-learned.md",
    ).stdout
    assert pushed == "generated\n"
    assert "<<<<<<<" not in pushed
    assert "=======" not in pushed
    assert ">>>>>>>" not in pushed


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
