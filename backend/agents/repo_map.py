"""OP-840 repo-map preamble builder with tree-sitter PageRank."""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from backend.agents.treesitter_parser import (
    PARSE_ERROR_PER_FILE,
    TREE_SITTER_BINARY_MISSING,
    TREE_SITTER_GRAMMAR_MISSING_LANG,
    FileParseError,
    GrammarMissing,
    ParsedSource,
    TreeSitterImportParser,
    TreeSitterUnavailable,
    language_for_path,
)

log = logging.getLogger(__name__)

REPO_MAP_OOM = "repo_map_oom"
CACHE_CORRUPTION = "cache_corruption"

DEFAULT_TOKEN_BUDGET = 1000
NO_FILES_TOUCHED_TOKEN_BUDGET = 2000
DEFAULT_TOP_N = 50
FALLBACK_RECENT_FILE_COUNT = 100
_CACHE_DIR = Path(".cache/omnisight/repo-map")
_PATH_RE = re.compile(r"\b[A-Za-z0-9_./-]+\.(?:py|ts|tsx)\b")


@dataclass(frozen=True)
class RepoMapLog:
    """One non-fatal repo-map build gap."""

    code: str
    path: str = ""
    language: str = ""
    message: str = ""


@dataclass(frozen=True)
class RepoMapGraph:
    """File-level import/reference graph."""

    head_sha: str
    nodes: tuple[str, ...]
    edges: dict[str, tuple[str, ...]]
    logs: tuple[RepoMapLog, ...] = ()
    fallback: str = ""

    def to_json(self) -> dict:
        return {
            "head_sha": self.head_sha,
            "nodes": list(self.nodes),
            "edges": {k: list(v) for k, v in self.edges.items()},
            "logs": [log.__dict__ for log in self.logs],
            "fallback": self.fallback,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "RepoMapGraph":
        return cls(
            head_sha=str(raw["head_sha"]),
            nodes=tuple(str(p) for p in raw["nodes"]),
            edges={
                str(k): tuple(str(v) for v in values)
                for k, values in raw["edges"].items()
            },
            logs=tuple(RepoMapLog(**row) for row in raw.get("logs", [])),
            fallback=str(raw.get("fallback", "")),
        )


@dataclass(frozen=True)
class RepoMapEntry:
    """One rendered repo-map file row."""

    path: str
    score: float
    outgoing_refs: int


def build_repo_map_system_prefix(
    repo_root: Path,
    *,
    ticket_text: str = "",
    token_budget: int | None = None,
    top_n: int = DEFAULT_TOP_N,
    cache_dir: Path | None = None,
) -> str:
    """Build the markdown prefix injected into the system prompt."""

    try:
        graph = load_or_build_graph(repo_root, cache_dir=cache_dir)
    except TreeSitterUnavailable as exc:
        _log_gap(RepoMapLog(code=TREE_SITTER_BINARY_MISSING, message=str(exc)))
        return ""
    budget = token_budget if token_budget is not None else token_budget_for_ticket(ticket_text)
    entries = rank_repo_files(graph, ticket_text=ticket_text, top_n=top_n)
    return render_repo_map_prefix(entries, token_budget=budget)


def load_or_build_graph(
    repo_root: Path,
    *,
    cache_dir: Path | None = None,
    parser: TreeSitterImportParser | None = None,
) -> RepoMapGraph:
    """Load a HEAD-keyed graph cache or rebuild it from source."""

    repo_root = repo_root.resolve()
    head_sha = repo_head_sha(repo_root)
    target_cache_dir = repo_root / (cache_dir or _CACHE_DIR)
    cache_path = target_cache_dir / f"{head_sha}.json"
    if cache_path.exists():
        try:
            graph = RepoMapGraph.from_json(json.loads(cache_path.read_text()))
            if graph.head_sha == head_sha:
                return graph
        except Exception as exc:
            _log_gap(RepoMapLog(code=CACHE_CORRUPTION, message=str(exc)))
    graph = build_graph(repo_root, head_sha=head_sha, parser=parser)
    target_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(graph.to_json(), indent=2, sort_keys=True) + "\n")
    return graph


def build_graph(
    repo_root: Path,
    *,
    head_sha: str | None = None,
    parser: TreeSitterImportParser | None = None,
) -> RepoMapGraph:
    """Build a file-level graph from supported source files."""

    repo_root = repo_root.resolve()
    parser = parser or TreeSitterImportParser()
    logs: list[RepoMapLog] = []
    try:
        files = supported_source_files(repo_root)
        parsed = _parse_files(repo_root, files, parser, logs)
        nodes = tuple(sorted(source.path for source in parsed))
        node_set = set(nodes)
        edges = {
            source.path: tuple(
                sorted(
                    resolved
                    for ref in source.references
                    for resolved in [_resolve_reference(source.path, ref, node_set)]
                    if resolved
                )
            )
            for source in parsed
        }
        return RepoMapGraph(
            head_sha=head_sha or repo_head_sha(repo_root),
            nodes=nodes,
            edges=edges,
            logs=tuple(logs),
        )
    except MemoryError:
        logs.append(RepoMapLog(code=REPO_MAP_OOM, message="repo-map build exceeded memory"))
        nodes = tuple(most_recent_source_files(repo_root, limit=FALLBACK_RECENT_FILE_COUNT))
        return RepoMapGraph(
            head_sha=head_sha or repo_head_sha(repo_root),
            nodes=nodes,
            edges={path: () for path in nodes},
            logs=tuple(logs),
            fallback=REPO_MAP_OOM,
        )


def supported_source_files(repo_root: Path) -> list[Path]:
    """Return tracked or filesystem-discovered supported files."""

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "*.py", "*.ts", "*.tsx"],
            text=True,
            capture_output=True,
            check=True,
        )
        paths = [repo_root / line for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.CalledProcessError):
        paths = [
            path
            for suffix in ("*.py", "*.ts", "*.tsx")
            for path in repo_root.rglob(suffix)
            if ".git" not in path.parts
        ]
    return sorted(path for path in paths if language_for_path(path) is not None)


def most_recent_source_files(repo_root: Path, *, limit: int) -> list[str]:
    files = supported_source_files(repo_root)
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [path.relative_to(repo_root).as_posix() for path in files[:limit]]


def rank_repo_files(
    graph: RepoMapGraph,
    *,
    ticket_text: str,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[RepoMapEntry, ...]:
    """Run personalized PageRank and return the highest-scoring files."""

    if not graph.nodes:
        return ()
    scores = _pagerank(graph, seeds=_mentioned_files(ticket_text, graph.nodes))
    ordered = sorted(graph.nodes, key=lambda path: (-scores[path], path))[:top_n]
    return tuple(
        RepoMapEntry(
            path=path,
            score=scores[path],
            outgoing_refs=len(graph.edges.get(path, ())),
        )
        for path in ordered
    )


def render_repo_map_prefix(
    entries: Iterable[RepoMapEntry],
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> str:
    """Render a token-budgeted system-prompt prefix."""

    if token_budget <= 0:
        return ""
    lines = [
        "# Repo Map Context",
        "",
        "High-signal files for this task, ranked by ticket-seeded PageRank:",
    ]
    used = _estimate_tokens("\n".join(lines))
    for entry in entries:
        line = f"- `{entry.path}` (score={entry.score:.5f}, refs={entry.outgoing_refs})"
        cost = _estimate_tokens(line)
        if used + cost > token_budget:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines) if len(lines) > 3 else ""


def token_budget_for_ticket(ticket_text: str) -> int:
    """Use a larger default when the ticket lacks Files Touched hints."""

    lower = ticket_text.lower()
    if "files touched" not in lower:
        return NO_FILES_TOUCHED_TOKEN_BUDGET
    touched = lower.split("files touched", 1)[1]
    return DEFAULT_TOKEN_BUDGET if _PATH_RE.search(touched) else NO_FILES_TOUCHED_TOKEN_BUDGET


def repo_head_sha(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "no-git-head"


def _parse_files(
    repo_root: Path,
    files: list[Path],
    parser: TreeSitterImportParser,
    logs: list[RepoMapLog],
) -> list[ParsedSource]:
    parsed: list[ParsedSource] = []
    missing_languages: set[str] = set()
    for path in files:
        rel_path = path.relative_to(repo_root).as_posix()
        try:
            parsed.append(parser.parse_file(repo_root, path))
        except GrammarMissing as exc:
            if exc.language not in missing_languages:
                logs.append(
                    RepoMapLog(
                        code=TREE_SITTER_GRAMMAR_MISSING_LANG,
                        path=rel_path,
                        language=exc.language,
                        message=str(exc),
                    )
                )
                missing_languages.add(exc.language)
        except FileParseError as exc:
            logs.append(
                RepoMapLog(
                    code=PARSE_ERROR_PER_FILE,
                    path=rel_path,
                    language=language_for_path(path) or "",
                    message=str(exc),
                )
            )
    for gap in logs:
        _log_gap(gap)
    return parsed


def _resolve_reference(source_path: str, ref: str, node_set: set[str]) -> str | None:
    if source_path.endswith(".py"):
        return _resolve_python_reference(source_path, ref, node_set)
    return _resolve_typescript_reference(source_path, ref, node_set)


def _resolve_python_reference(source_path: str, ref: str, node_set: set[str]) -> str | None:
    source = Path(source_path)
    if ref.startswith("."):
        level = len(ref) - len(ref.lstrip("."))
        suffix = ref.lstrip(".")
        base_parts = list(source.parent.parts)
        if level > 1:
            base_parts = base_parts[: -(level - 1)]
        module_parts = base_parts + ([p for p in suffix.split(".") if p] if suffix else [])
    else:
        module_parts = [p for p in ref.split(".") if p]
    candidates: list[str] = []
    for idx in range(len(module_parts), 0, -1):
        prefix = "/".join(module_parts[:idx])
        candidates.append(f"{prefix}.py")
        candidates.append(f"{prefix}/__init__.py")
    for candidate in candidates:
        if candidate in node_set:
            return candidate
    return None


def _resolve_typescript_reference(source_path: str, ref: str, node_set: set[str]) -> str | None:
    if not ref.startswith("."):
        return None
    base = (Path(source_path).parent / ref).as_posix()
    candidates = [
        base,
        f"{base}.ts",
        f"{base}.tsx",
        f"{base}/index.ts",
        f"{base}/index.tsx",
    ]
    return next((candidate for candidate in candidates if candidate in node_set), None)


def _pagerank(
    graph: RepoMapGraph,
    *,
    seeds: set[str],
    damping: float = 0.85,
    iterations: int = 40,
    tolerance: float = 1.0e-9,
) -> dict[str, float]:
    nodes = list(graph.nodes)
    count = len(nodes)
    personalization = {node: 0.0 for node in nodes}
    active_seeds = seeds & set(nodes)
    if active_seeds:
        weight = 1.0 / len(active_seeds)
        for seed in active_seeds:
            personalization[seed] = weight
    else:
        weight = 1.0 / count
        for node in nodes:
            personalization[node] = weight
    scores = {node: 1.0 / count for node in nodes}
    for _ in range(iterations):
        new_scores = {node: (1.0 - damping) * personalization[node] for node in nodes}
        dangling = sum(scores[node] for node in nodes if not graph.edges.get(node))
        for node in nodes:
            new_scores[node] += damping * dangling * personalization[node]
            outgoing = graph.edges.get(node, ())
            if not outgoing:
                continue
            share = damping * scores[node] / len(outgoing)
            for dest in outgoing:
                if dest in new_scores:
                    new_scores[dest] += share
        delta = sum(math.fabs(new_scores[node] - scores[node]) for node in nodes)
        scores = new_scores
        if delta < tolerance:
            break
    return scores


def _mentioned_files(ticket_text: str, nodes: tuple[str, ...]) -> set[str]:
    node_set = set(nodes)
    mentioned: set[str] = set()
    for raw in _PATH_RE.findall(ticket_text):
        cleaned = raw.lstrip("./")
        if cleaned in node_set:
            mentioned.add(cleaned)
    return mentioned


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _log_gap(gap: RepoMapLog) -> None:
    log.warning(
        "repo_map_gap",
        extra={
            "code": gap.code,
            "path": gap.path,
            "language": gap.language,
            "detail": gap.message,
        },
    )
