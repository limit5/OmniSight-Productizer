"""BP.Q.2 -- workspace RAG indexing pipeline.

This module mirrors the BP.Q.1 adapter shape in :mod:`backend.agents.rag` and
the BP.J.2 hook split: the indexer is provider-neutral, testable business
logic, while ``backend.hooks.post_merge_rag_index`` is only git glue.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
Only immutable constants and pure helper functions live at module scope. The
index state is persisted through the injected ``VectorStore`` and embeddings
are produced by the injected ``EmbeddingProvider`` instance; uvicorn workers do
not share process-local mutable truth.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from backend.agents.rag import (
    ChromaStore,
    EmbeddingProvider,
    LocalSentenceTransformerEmbedding,
    OpenAIEmbedding,
    PgvectorStore,
    QdrantStore,
    VectorDocument,
    VectorStore,
)


DEFAULT_TENANT_ID = "t-default"
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_CHUNK_LINES = 120

CODE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".m",
        ".mm",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
    }
)
DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt"})
SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "test_assets",
    }
)

TreeSitterParserFactory = Callable[[str], Any | None]


class AsyncCloseable(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True)
class SourceFile:
    """One indexable repo-relative source file."""

    path: str
    absolute_path: Path
    kind: str
    language: str | None = None


@dataclass(frozen=True)
class SourceChunk:
    """One chunk ready for embedding."""

    source_path: str
    chunk_text: str
    line_start: int
    line_end: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        body_hash = hashlib.sha256(self.chunk_text.encode("utf-8")).hexdigest()[:16]
        raw = f"{self.source_path}:{self.line_start}:{self.line_end}:{body_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IndexResult:
    """Summary returned by initial and incremental indexing runs."""

    indexed_files: int = 0
    deleted_files: int = 0
    skipped_files: int = 0
    chunks: int = 0


class WorkspaceRagIndexer:
    """Index a git workspace into a BP.Q.1 ``VectorStore``."""

    def __init__(
        self,
        *,
        repo_root: Path,
        tenant_id: str,
        embedder: EmbeddingProvider,
        store: VectorStore,
        batch_size: int = DEFAULT_BATCH_SIZE,
        parser_factory: TreeSitterParserFactory | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.tenant_id = tenant_id.strip()
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        self.embedder = embedder
        self.store = store
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.parser_factory = parser_factory

    async def index_workspace(self, *, prune_missing: bool = True) -> IndexResult:
        """Index every relevant git-tracked file in the workspace."""

        sources = discover_source_files(self.repo_root)
        result = await self._index_sources(sources)
        if prune_missing:
            deleted = await self._prune_missing_sources({src.path for src in sources})
            result = IndexResult(
                indexed_files=result.indexed_files,
                deleted_files=result.deleted_files + deleted,
                skipped_files=result.skipped_files,
                chunks=result.chunks,
            )
        return result

    async def index_changed_paths(self, paths: Iterable[str]) -> IndexResult:
        """Index changed paths and delete stale chunks for removed/untracked paths."""

        sources: list[SourceFile] = []
        deleted = 0
        skipped = 0
        seen: set[str] = set()
        for raw_path in paths:
            rel = _normalise_rel_path(raw_path)
            if not rel or rel in seen:
                continue
            seen.add(rel)
            absolute = self.repo_root / rel
            source = source_file_for_path(self.repo_root, rel)
            if source is None:
                await self.store.delete(tenant_id=self.tenant_id, source_path=rel)
                deleted += 1
            elif not absolute.exists():
                await self.store.delete(tenant_id=self.tenant_id, source_path=rel)
                deleted += 1
            else:
                sources.append(source)
        result = await self._index_sources(sources)
        return IndexResult(
            indexed_files=result.indexed_files,
            deleted_files=result.deleted_files + deleted,
            skipped_files=result.skipped_files + skipped,
            chunks=result.chunks,
        )

    async def index_git_merge_delta(self) -> IndexResult:
        """Index files changed by ``ORIG_HEAD..HEAD`` after a git merge."""

        changed = changed_files_in_merge(self.repo_root)
        if changed is None:
            return await self.index_workspace(prune_missing=True)
        return await self.index_changed_paths(changed)

    async def _index_sources(self, sources: list[SourceFile]) -> IndexResult:
        indexed = 0
        skipped = 0
        chunks_total = 0
        for source in sources:
            chunks = chunk_source_file(source, parser_factory=self.parser_factory)
            await self.store.delete(tenant_id=self.tenant_id, source_path=source.path)
            if not chunks:
                skipped += 1
                continue
            await self._embed_and_upsert(chunks)
            indexed += 1
            chunks_total += len(chunks)
        return IndexResult(indexed_files=indexed, skipped_files=skipped, chunks=chunks_total)

    async def _embed_and_upsert(self, chunks: list[SourceChunk]) -> None:
        for batch in _batched(chunks, self.batch_size):
            embeddings = await self.embedder.embed_texts([chunk.chunk_text for chunk in batch])
            documents = [
                VectorDocument(
                    chunk_id=self._tenant_chunk_id(chunk),
                    tenant_id=self.tenant_id,
                    source_path=chunk.source_path,
                    chunk_text=chunk.chunk_text,
                    embedding=embedding,
                    metadata={
                        **chunk.metadata,
                        "line_start": chunk.line_start,
                        "line_end": chunk.line_end,
                    },
                )
                for chunk, embedding in zip(batch, embeddings, strict=True)
            ]
            await self.store.upsert(documents)

    def _tenant_chunk_id(self, chunk: SourceChunk) -> str:
        raw = f"{self.tenant_id}:{chunk.chunk_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _prune_missing_sources(self, live_paths: set[str]) -> int:
        deleted = 0
        offset = 0
        limit = 500
        while True:
            docs = await self.store.list_by_tenant(
                self.tenant_id, limit=limit, offset=offset
            )
            if not docs:
                break
            for source_path in sorted({doc.source_path for doc in docs} - live_paths):
                deleted += await self.store.delete(
                    tenant_id=self.tenant_id, source_path=source_path
                )
            if len(docs) < limit:
                break
            offset += limit
        return deleted


def discover_source_files(repo_root: Path) -> list[SourceFile]:
    """Return relevant git-tracked files sorted by path."""

    sources: list[SourceFile] = []
    for rel in git_tracked_files(repo_root):
        source = source_file_for_path(repo_root, rel)
        if source is not None:
            sources.append(source)
    return sorted(sources, key=lambda src: src.path)


def source_file_for_path(repo_root: Path, rel_path: str) -> SourceFile | None:
    """Build a ``SourceFile`` if ``rel_path`` is indexable."""

    rel = _normalise_rel_path(rel_path)
    if not rel or _path_has_skip_dir(rel):
        return None
    path = (repo_root / rel).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError:
        return None
    if not path.is_file() or _looks_binary(path):
        return None
    kind, language = classify_source_path(rel)
    if kind is None:
        return None
    return SourceFile(path=rel, absolute_path=path, kind=kind, language=language)


def classify_source_path(rel_path: str) -> tuple[str | None, str | None]:
    """Classify a repo-relative path into code/doc/special source kinds."""

    path = Path(rel_path)
    name = path.name
    suffix = path.suffix.lower()
    if name == "TODO.md":
        return "todo", "markdown"
    if name == "SKILL.md":
        return "skill", "markdown"
    if rel_path.startswith("docs/") and suffix in DOC_EXTENSIONS:
        return "doc", "markdown" if suffix in {".md", ".mdx"} else "text"
    if suffix in CODE_EXTENSIONS:
        return "code", language_for_extension(suffix)
    return None, None


def chunk_source_file(
    source: SourceFile,
    *,
    parser_factory: TreeSitterParserFactory | None = None,
) -> list[SourceChunk]:
    """Chunk a source file using markdown headers for docs and tree-sitter for code."""

    text = source.absolute_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return []
    if source.kind in {"doc", "skill", "todo"} and source.language == "markdown":
        return chunk_markdown(source.path, text, source_kind=source.kind)
    if source.kind == "code":
        chunks = chunk_code_with_tree_sitter(
            source.path,
            text,
            language=source.language or "",
            parser_factory=parser_factory,
        )
        if chunks:
            return chunks
    return chunk_by_line_window(source.path, text, source_kind=source.kind)


def chunk_markdown(
    source_path: str,
    text: str,
    *,
    source_kind: str,
) -> list[SourceChunk]:
    """Split markdown at ATX headers while preserving citation line ranges."""

    lines = text.splitlines()
    starts = [idx for idx, line in enumerate(lines) if line.startswith("#")]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    chunks: list[SourceChunk] = []
    for pos, start in enumerate(starts):
        end = (starts[pos + 1] - 1) if pos + 1 < len(starts) else len(lines) - 1
        body = "\n".join(lines[start : end + 1]).strip()
        if not body:
            continue
        header = _first_markdown_header(lines[start : end + 1])
        chunks.append(
            SourceChunk(
                source_path=source_path,
                chunk_text=body,
                line_start=start + 1,
                line_end=end + 1,
                metadata={
                    "kind": source_kind,
                    "language": "markdown",
                    "chunk_strategy": "markdown-header",
                    "header": header,
                },
            )
        )
    return chunks


def chunk_code_with_tree_sitter(
    source_path: str,
    text: str,
    *,
    language: str,
    parser_factory: TreeSitterParserFactory | None = None,
) -> list[SourceChunk]:
    """Chunk code using tree-sitter top-level named nodes when available."""

    parser = (parser_factory or default_tree_sitter_parser)(language)
    if parser is None:
        return []
    try:
        tree = parser.parse(text.encode("utf-8"))
        root = tree.root_node
    except Exception:
        return []
    lines = text.splitlines()
    chunks: list[SourceChunk] = []
    for node in getattr(root, "children", []):
        if not getattr(node, "is_named", True):
            continue
        start_line = int(node.start_point[0]) + 1
        end_line = int(node.end_point[0]) + 1
        if end_line < start_line:
            continue
        body = "\n".join(lines[start_line - 1 : end_line]).strip()
        if not body:
            continue
        chunks.append(
            SourceChunk(
                source_path=source_path,
                chunk_text=body,
                line_start=start_line,
                line_end=end_line,
                metadata={
                    "kind": "code",
                    "language": language,
                    "chunk_strategy": "tree-sitter",
                    "node_type": getattr(node, "type", "node"),
                },
            )
        )
    return chunks or chunk_by_line_window(source_path, text, source_kind="code")


def default_tree_sitter_parser(language: str) -> Any | None:
    """Return a tree-sitter parser when optional grammar packages are installed."""

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


def chunk_by_line_window(
    source_path: str,
    text: str,
    *,
    source_kind: str,
    max_lines: int = DEFAULT_MAX_CHUNK_LINES,
) -> list[SourceChunk]:
    """Fallback line-window chunker for text and code without tree-sitter."""

    lines = text.splitlines()
    chunks: list[SourceChunk] = []
    for start in range(0, len(lines), max_lines):
        end = min(len(lines), start + max_lines)
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        chunks.append(
            SourceChunk(
                source_path=source_path,
                chunk_text=body,
                line_start=start + 1,
                line_end=end,
                metadata={
                    "kind": source_kind,
                    "chunk_strategy": "line-window",
                },
            )
        )
    return chunks


def git_tracked_files(repo_root: Path) -> list[str]:
    """Return ``git ls-files`` output, falling back to a filesystem walk."""

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return _walk_files(repo_root)
    if out.returncode != 0:
        return _walk_files(repo_root)
    return [
        path.decode("utf-8", errors="replace")
        for path in out.stdout.split(b"\0")
        if path
    ]


def changed_files_in_merge(repo_root: Path) -> list[str] | None:
    """Return paths changed by ``ORIG_HEAD..HEAD`` or ``None`` when unknown."""

    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "ORIG_HEAD", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line]


def language_for_extension(suffix: str) -> str | None:
    return {
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cs": "c_sharp",
        ".css": "css",
        ".go": "go",
        ".h": "c",
        ".hpp": "cpp",
        ".java": "java",
        ".js": "javascript",
        ".jsx": "javascript",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".m": "objc",
        ".mm": "objc",
        ".py": "python",
        ".rs": "rust",
        ".sh": "bash",
        ".sql": "sql",
        ".swift": "swift",
        ".ts": "typescript",
        ".tsx": "tsx",
    }.get(suffix)


def _first_markdown_header(lines: list[str]) -> str:
    for line in lines:
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _normalise_rel_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _path_has_skip_dir(rel_path: str) -> bool:
    return any(part in SKIP_DIRS for part in rel_path.split("/"))


def _looks_binary(path: Path) -> bool:
    try:
        return b"\0" in path.read_bytes()[:4096]
    except OSError:
        return True


def _walk_files(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        rel_str = str(rel).replace(os.sep, "/")
        if not _path_has_skip_dir(rel_str):
            paths.append(rel_str)
    return sorted(paths)


def _batched(items: list[SourceChunk], size: int) -> Iterable[list[SourceChunk]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


async def build_indexer_from_env(repo_root: Path) -> tuple[WorkspaceRagIndexer, AsyncCloseable | None]:
    """Build an indexer from environment variables for CLI / hook use."""

    tenant_id = os.environ.get("OMNISIGHT_RAG_TENANT_ID", DEFAULT_TENANT_ID)
    embedder = _build_embedder_from_env()
    store, closeable = await _build_store_from_env()
    return (
        WorkspaceRagIndexer(
            repo_root=repo_root,
            tenant_id=tenant_id,
            embedder=embedder,
            store=store,
            batch_size=int(os.environ.get("OMNISIGHT_RAG_INDEX_BATCH_SIZE", "32")),
        ),
        closeable,
    )


def _build_embedder_from_env() -> EmbeddingProvider:
    provider = os.environ.get("OMNISIGHT_RAG_EMBEDDING_PROVIDER", "local").lower()
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI RAG embeddings")
        return OpenAIEmbedding(
            api_key=api_key,
            model=os.environ.get("OMNISIGHT_RAG_EMBEDDING_MODEL", "text-embedding-3-small"),
        )
    if provider == "local":
        return LocalSentenceTransformerEmbedding(
            model=os.environ.get(
                "OMNISIGHT_RAG_EMBEDDING_MODEL",
                "sentence-transformers/all-MiniLM-L6-v2",
            )
        )
    raise RuntimeError(f"unsupported OMNISIGHT_RAG_EMBEDDING_PROVIDER={provider!r}")


async def _build_store_from_env() -> tuple[VectorStore, AsyncCloseable | None]:
    provider = os.environ.get("OMNISIGHT_RAG_VECTOR_STORE", "pgvector").lower()
    if provider == "pgvector":
        database_url = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("OMNISIGHT_DATABASE_URL is required for pgvector RAG indexing")
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("asyncpg is required for pgvector RAG indexing") from exc
        pool = await asyncpg.create_pool(database_url)
        return PgvectorStore(pool), pool
    if provider == "qdrant":
        collection = os.environ.get("OMNISIGHT_RAG_QDRANT_COLLECTION", "omnisight")
        return (
            QdrantStore(
                collection=collection,
                api_key=os.environ.get("QDRANT_API_KEY"),
                api_base=os.environ.get("QDRANT_API_BASE", "http://localhost:6333"),
            ),
            None,
        )
    if provider == "chroma":
        collection = os.environ.get("OMNISIGHT_RAG_CHROMA_COLLECTION", "omnisight")
        return (
            ChromaStore(
                collection=collection,
                api_base=os.environ.get("CHROMA_API_BASE", "http://localhost:8000"),
            ),
            None,
        )
    raise RuntimeError(f"unsupported OMNISIGHT_RAG_VECTOR_STORE={provider!r}")


async def _run_cli(args: argparse.Namespace) -> int:
    indexer, closeable = await build_indexer_from_env(args.repo_root)
    try:
        if args.incremental:
            result = await indexer.index_git_merge_delta()
        else:
            result = await indexer.index_workspace(prune_missing=True)
    finally:
        if closeable is not None:
            await closeable.close()
    print(
        "indexed={indexed} deleted={deleted} skipped={skipped} chunks={chunks}".format(
            indexed=result.indexed_files,
            deleted=result.deleted_files,
            skipped=result.skipped_files,
            chunks=result.chunks,
        )
    )
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.agents.rag_indexer",
        description="Index a workspace into the configured BP.Q vector store.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--incremental", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
