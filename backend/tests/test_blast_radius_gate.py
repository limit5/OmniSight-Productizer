"""OP-841 - AST blast-radius gate regression tests."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.agents.blast_radius_gate import (
    BlastRadiusMetrics,
    LocatorCandidate,
    PeerPatchSet,
    evaluate_blast_radius,
)


class _CleanParser:
    def parse(self, raw: bytes) -> Any:
        return type(
            "Tree",
            (),
            {"root_node": type("Root", (), {"has_error": False})()},
        )()


class _FailingParser:
    def parse(self, raw: bytes) -> Any:
        raise ValueError("bad syntax")


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _write(root: Path, path: str, body: str = "x = 1\n") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _eval(
    root: Path,
    *,
    labels: tuple[str, ...] = ("area:tests",),
    candidates: tuple[LocatorCandidate, ...] = (LocatorCandidate("backend/a.py"),),
    parser: Any | None = _CleanParser(),
    peer_lookup=None,
    thresholds_path: Path | None = None,
) -> Any:
    return evaluate_blast_radius(
        ticket_key="OP-841",
        area_labels=labels,
        candidates=candidates,
        worktree_root=root,
        cwd=root,
        parser_factory=lambda language: parser,
        peer_lookup=peer_lookup or (lambda path: []),
        thresholds_path=thresholds_path
        or Path("config/blast_radius_thresholds.yaml"),
    )


def test_within_threshold_passes_to_coder(worktree: Path) -> None:
    _write(worktree, "backend/a.py", "x = 1\n")

    result = _eval(worktree)

    assert result.status == "within_threshold"
    assert result.ok is True
    assert result.metrics == BlastRadiusMetrics(
        total_files=1,
        total_loc=1,
        dependency_depth=0,
    )


def test_oversize_files_returns_structured_refusal(worktree: Path) -> None:
    for idx in range(9):
        _write(worktree, f"backend/{idx}.py")

    result = _eval(
        worktree,
        candidates=tuple(LocatorCandidate(f"backend/{idx}.py") for idx in range(9)),
    )

    assert result.status == "oversize_refuse"
    assert result.runner_policy["fallback_label"] == "needs:split"


def test_oversize_loc_returns_structured_refusal(worktree: Path) -> None:
    body = "\n".join("x = 1" for _ in range(801)) + "\n"
    _write(worktree, "backend/a.py", body)

    result = _eval(worktree)

    assert result.status == "oversize_refuse"
    assert result.metrics.total_loc == 801


def test_oversize_depth_returns_structured_refusal(worktree: Path) -> None:
    _write(worktree, "backend/a.py")

    result = _eval(
        worktree,
        candidates=(LocatorCandidate("backend/a.py", dependency_depth=7),),
    )

    assert result.status == "oversize_refuse"
    assert result.metrics.dependency_depth == 7


def test_multi_area_collision_takes_max_threshold_and_comments(worktree: Path) -> None:
    for idx in range(8):
        _write(worktree, f"backend/{idx}.py")

    result = _eval(
        worktree,
        labels=("area:migrations", "area:tests"),
        candidates=tuple(LocatorCandidate(f"backend/{idx}.py") for idx in range(8)),
    )

    assert result.status == "within_threshold"
    assert result.threshold == {"files": 8, "loc": 800, "depth": 6}
    assert "multi_area_threshold_collision" in result.errors
    assert any(
        "multi_area_threshold_collision" in comment
        for comment in result.comments
    )


def test_missing_area_uses_strict_default_and_comments(worktree: Path) -> None:
    for idx in range(4):
        _write(worktree, f"backend/{idx}.py")

    result = _eval(
        worktree,
        labels=(),
        candidates=tuple(LocatorCandidate(f"backend/{idx}.py") for idx in range(4)),
    )

    assert result.status == "oversize_refuse"
    assert result.threshold == {"files": 3, "loc": 300, "depth": 4}
    assert any("missing area label" in comment for comment in result.comments)


def test_parse_fail_counts_candidate_as_200_loc_and_comments(worktree: Path) -> None:
    _write(worktree, "backend/a.py")

    result = _eval(worktree, parser=_FailingParser())

    assert result.metrics.total_loc == 200
    assert "tree_sitter_parse_fail" in result.errors
    assert any("counted as 200 LOC" in comment for comment in result.comments)


def test_cross_ticket_peer_detect_returns_peer_conflict(worktree: Path) -> None:
    _write(worktree, "backend/a.py")

    result = _eval(
        worktree,
        peer_lookup=lambda path: [
            PeerPatchSet(
                file=path,
                change_number="335",
                ticket_key="OP-824",
                owner="codex-bot",
            )
        ],
    )

    assert result.status == "peer_conflict"
    assert result.peer_conflict == PeerPatchSet(
        file="backend/a.py",
        change_number="335",
        ticket_key="OP-824",
        owner="codex-bot",
    )
    assert "cross_ticket_peer_collision" in result.errors


def test_threshold_yaml_malformed_falls_back_to_default(
    worktree: Path,
    tmp_path: Path,
) -> None:
    _write(worktree, "backend/a.py")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    result = _eval(worktree, labels=("area:tests",), thresholds_path=bad)

    assert result.threshold == {"files": 3, "loc": 300, "depth": 4}
    assert "threshold_yaml_malformed" in result.errors


def test_worktree_cwd_assertion_rejects_main_repo_cwd(
    worktree: Path,
    tmp_path: Path,
) -> None:
    _write(worktree, "backend/a.py")
    other = tmp_path / "main"
    other.mkdir()

    with pytest.raises(RuntimeError, match="worktree_cwd_mismatch"):
        evaluate_blast_radius(
            ticket_key="OP-841",
            area_labels=("area:tests",),
            candidates=(LocatorCandidate("backend/a.py"),),
            worktree_root=worktree,
            cwd=other,
            parser_factory=lambda language: _CleanParser(),
            peer_lookup=lambda path: [],
        )
