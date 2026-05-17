"""OP-841 - AST blast-radius gate for runner locator candidates.

The gate is intentionally pure and dependency-injected so runner pickup can
call it before code generation without coupling tests to live JIRA/Gerrit.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS_PATH = REPO_ROOT / "config" / "blast_radius_thresholds.yaml"
DEFAULT_THRESHOLD = {"files": 3, "loc": 300, "depth": 4}
PARSE_FAIL_LOC = 200


@dataclass(frozen=True)
class LocatorCandidate:
    """One path returned by the locator plus dependency graph depth."""

    path: str
    dependency_depth: int = 0


@dataclass(frozen=True)
class BlastRadiusMetrics:
    total_files: int
    total_loc: int
    dependency_depth: int


@dataclass(frozen=True)
class PeerPatchSet:
    file: str
    change_number: str
    ticket_key: str | None = None
    owner: str | None = None


@dataclass(frozen=True)
class BlastRadiusResult:
    status: str
    metrics: BlastRadiusMetrics
    threshold: dict[str, int]
    comments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    peer_conflict: PeerPatchSet | None = None
    runner_policy: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "within_threshold"


TreeSitterParserFactory = Callable[[str], Any | None]
PeerLookup = Callable[[str], Iterable[PeerPatchSet]]


def evaluate_blast_radius(
    *,
    ticket_key: str,
    area_labels: Iterable[str],
    candidates: Iterable[LocatorCandidate],
    worktree_root: str | Path,
    cwd: str | Path | None = None,
    thresholds_path: str | Path = THRESHOLDS_PATH,
    parser_factory: TreeSitterParserFactory | None = None,
    peer_lookup: PeerLookup | None = None,
) -> BlastRadiusResult:
    """Compute the component-aware blast-radius decision for locator output."""

    assert_worktree_cwd(worktree_root, cwd=cwd)
    candidate_list = list(candidates)
    comments: list[str] = []
    errors: list[str] = []

    thresholds, threshold_error = load_thresholds(Path(thresholds_path))
    if threshold_error:
        errors.append(threshold_error)
        comments.append(
            "[blast-radius] threshold_yaml_malformed; using default (3, 300, 4)."
        )

    threshold, threshold_comments, threshold_errors = select_threshold(
        area_labels, thresholds
    )
    comments.extend(threshold_comments)
    errors.extend(threshold_errors)

    root = Path(worktree_root)
    total_loc = 0
    for candidate in candidate_list:
        loc, parse_error = candidate_loc(
            root / candidate.path,
            parser_factory=parser_factory,
        )
        total_loc += loc
        if parse_error:
            errors.append("tree_sitter_parse_fail")
            comments.append(
                f"[blast-radius] tree_sitter_parse_fail for {candidate.path}; "
                f"counted as {PARSE_FAIL_LOC} LOC."
            )

    metrics = BlastRadiusMetrics(
        total_files=len(candidate_list),
        total_loc=total_loc,
        dependency_depth=max((c.dependency_depth for c in candidate_list), default=0),
    )

    peer = detect_peer_conflict(ticket_key, candidate_list, peer_lookup)
    if peer is not None:
        return BlastRadiusResult(
            status="peer_conflict",
            metrics=metrics,
            threshold=threshold,
            comments=comments + [
                "[blast-radius] cross_ticket_peer_collision: "
                f"{peer.file} is already in open PS #{peer.change_number}."
            ],
            errors=errors + ["cross_ticket_peer_collision"],
            peer_conflict=peer,
        )

    if exceeds_threshold(metrics, threshold):
        return BlastRadiusResult(
            status="oversize_refuse",
            metrics=metrics,
            threshold=threshold,
            comments=comments,
            errors=errors,
            runner_policy={
                "decision": "main_runner_decides",
                "options": ["auto_split", "needs_split_revert", "escalate"],
                "fallback_label": "needs:split",
            },
        )

    return BlastRadiusResult(
        status="within_threshold",
        metrics=metrics,
        threshold=threshold,
        comments=comments,
        errors=errors,
    )


def assert_worktree_cwd(
    worktree_root: str | Path,
    cwd: str | Path | None = None,
) -> None:
    """Fail if the gate is not executing from the runner worktree root."""

    expected = Path(worktree_root).resolve()
    actual = Path.cwd().resolve() if cwd is None else Path(cwd).resolve()
    if actual != expected:
        raise RuntimeError(
            "worktree_cwd_mismatch in blast_radius_gate.assert_worktree_cwd: "
            f"cwd={actual} worktree={expected} "
            f"(cwd_arg={cwd!r}, worktree_root_arg={worktree_root!r})"
        )

    result = subprocess.run(
        ["git", "-C", str(actual), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    git_root = Path(result.stdout.strip()).resolve()
    if git_root != expected:
        raise RuntimeError(
            "worktree_cwd_mismatch in blast_radius_gate.assert_worktree_cwd: "
            f"git_root={git_root} worktree={expected} cwd={actual} "
            f"(worktree_root_arg={worktree_root!r})"
        )


def load_thresholds(path: Path) -> tuple[dict[str, dict[str, int]], str | None]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(
                "blast_radius_gate.load_thresholds: threshold YAML must be a "
                f"top-level mapping (path={path}, got_type={type(raw).__name__})"
            )
        parsed: dict[str, dict[str, int]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError(
                    "blast_radius_gate.load_thresholds: threshold YAML entries "
                    "must map str -> dict "
                    f"(path={path}, key={key!r}, key_type={type(key).__name__}, "
                    f"value_type={type(value).__name__})"
                )
            parsed[key] = _coerce_threshold(value)
        if "default" not in parsed:
            parsed["default"] = dict(DEFAULT_THRESHOLD)
        return parsed, None
    except Exception as exc:
        log.warning(
            "threshold_yaml_malformed in blast_radius_gate.load_thresholds: "
            "path=%s: %s",
            path,
            exc,
        )
        return {"default": dict(DEFAULT_THRESHOLD)}, "threshold_yaml_malformed"


def select_threshold(
    area_labels: Iterable[str],
    thresholds: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, int], list[str], list[str]]:
    labels = [label for label in area_labels if label.startswith("area:")]
    comments: list[str] = []
    errors: list[str] = []
    if not labels:
        comments.append(
            "[blast-radius] missing area label; using default (3, 300, 4)."
        )
        return dict(thresholds.get("default", DEFAULT_THRESHOLD)), comments, errors

    selected = dict(
        thresholds.get(labels[0], thresholds.get("default", DEFAULT_THRESHOLD))
    )
    matched = [labels[0]]
    for label in labels[1:]:
        current = thresholds.get(label, thresholds.get("default", DEFAULT_THRESHOLD))
        selected = {
            "files": max(selected["files"], int(current["files"])),
            "loc": max(selected["loc"], int(current["loc"])),
            "depth": max(selected["depth"], int(current["depth"])),
        }
        matched.append(label)

    if len(labels) > 1:
        errors.append("multi_area_threshold_collision")
        comments.append(
            "[blast-radius] multi_area_threshold_collision: "
            f"{', '.join(matched)} -> max threshold "
            f"({selected['files']}, {selected['loc']}, {selected['depth']})."
        )
    return selected, comments, errors


def candidate_loc(
    path: Path,
    *,
    parser_factory: TreeSitterParserFactory | None = None,
) -> tuple[int, bool]:
    text = path.read_text(encoding="utf-8")
    language = _language_for_path(path)
    parser = (parser_factory or default_tree_sitter_parser)(language)
    if parser is None:
        return len(text.splitlines()), False
    try:
        tree = parser.parse(text.encode("utf-8"))
        root = tree.root_node
        if bool(getattr(root, "has_error", False)):
            raise ValueError(
                "blast_radius_gate.candidate_loc: tree-sitter root has parse "
                f"errors (path={path}, language={language!r})"
            )
    except Exception as exc:
        log.warning(
            "tree_sitter_parse_fail in blast_radius_gate.candidate_loc: "
            "path=%s language=%s: %s",
            path,
            language or "<unknown>",
            exc,
        )
        return PARSE_FAIL_LOC, True
    return len(text.splitlines()), False


def detect_peer_conflict(
    ticket_key: str,
    candidates: Iterable[LocatorCandidate],
    peer_lookup: PeerLookup | None,
) -> PeerPatchSet | None:
    lookup = peer_lookup or default_peer_lookup
    for candidate in candidates:
        for peer in lookup(candidate.path):
            if peer.ticket_key != ticket_key:
                return peer
    return None


def default_peer_lookup(path: str) -> Iterable[PeerPatchSet]:
    from backend.agents import jira_dispatch

    try:
        owners = jira_dispatch._open_bot_owned_file_owners()
    except Exception as exc:
        log.warning(
            "gerrit peer lookup failed in blast_radius_gate.default_peer_lookup "
            "for path=%s: %s",
            path,
            exc,
        )
        return []
    return [
        PeerPatchSet(file=path, change_number=owner.change_number, owner=owner.owner)
        for owner in owners.get(path, [])
    ]


def exceeds_threshold(metrics: BlastRadiusMetrics, threshold: Mapping[str, int]) -> bool:
    return (
        metrics.total_files > int(threshold["files"])
        or metrics.total_loc > int(threshold["loc"])
        or metrics.dependency_depth > int(threshold["depth"])
    )


def default_tree_sitter_parser(language: str) -> Any | None:
    if not language:
        return None
    try:
        from tree_sitter_languages import get_parser
    except ImportError:
        return None
    try:
        return get_parser(language)
    except Exception:
        return None


def _language_for_path(path: Path) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
    }.get(path.suffix, "")


def _coerce_threshold(value: Mapping[str, object]) -> dict[str, int]:
    return {
        "files": int(value["files"]),
        "loc": int(value["loc"]),
        "depth": int(value["depth"]),
    }
