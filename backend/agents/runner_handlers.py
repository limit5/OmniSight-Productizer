"""Tool handlers for `auto-runner-sdk.py` agentic loop.

Implements Read / Write / Edit / Bash / Grep / Glob / KnowledgeRetrieval
with safety guards:

  * Path operations (Read / Write / Edit / Glob) reject paths outside
    `BASE_DIR` (resolved with realpath, so symlink escapes are blocked).
  * Bash forces ``cwd=BASE_DIR`` and honours the schema's ``timeout`` (ms).
  * Edit routes unique replacements through the WP.3 diff-validation
    cascade and refuses non-unique ``old_string`` unless
    ``replace_all=True``.

Register on a dispatcher via :func:`bind_to_dispatcher`. Tests build a
fresh ``ToolDispatcher`` so registration does not leak across tests.

Used by ``auto-runner-sdk.py`` to back ``AnthropicClient.run_with_tools``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from backend.agents.rag import VectorHit, VectorQuery
from backend.agents.rag_indexer import (
    DEFAULT_TENANT_ID,
    _build_embedder_from_env,
    _build_store_from_env,
)
from backend.agents.tool_dispatcher import ToolDispatcher


# Project root: env override > ascend from this file (backend/agents/ -> root).
_THIS = Path(__file__).resolve()
_DEFAULT_BASE = _THIS.parents[2]
BASE_DIR: Path = Path(
    os.environ.get("OMNISIGHT_RUNNER_BASE_DIR") or _DEFAULT_BASE
).resolve()


def _ensure_inside_base(path: str | Path) -> Path:
    """Resolve to absolute and verify it's under :data:`BASE_DIR`.

    Uses ``realpath`` semantics — symlinks pointing outside BASE_DIR are
    rejected. Non-existent paths are still resolved (Write needs this).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    p = p.resolve()
    try:
        p.relative_to(BASE_DIR)
    except ValueError as e:
        raise PermissionError(
            f"path {p} is outside BASE_DIR {BASE_DIR}"
        ) from e
    return p


# ─── Read ────────────────────────────────────────────────────────


def read_handler(payload: dict[str, Any]) -> str:
    p = _ensure_inside_base(payload["file_path"])
    if not p.exists():
        raise FileNotFoundError(f"{p} does not exist")
    if not p.is_file():
        raise IsADirectoryError(f"{p} is not a regular file")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(int(payload.get("offset", 1)) - 1, 0)
    limit = int(payload.get("limit", 2000))
    sliced = lines[offset : offset + limit]
    # cat -n style numbering — same shape AnthropicClient already documents
    return "\n".join(f"{i + 1 + offset}\t{ln}" for i, ln in enumerate(sliced))


# ─── Write ───────────────────────────────────────────────────────


def write_handler(payload: dict[str, Any]) -> str:
    p = _ensure_inside_base(payload["file_path"])
    content = payload["content"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


# ─── Edit ────────────────────────────────────────────────────────


def edit_handler(payload: dict[str, Any]) -> str:
    p = _ensure_inside_base(payload["file_path"])
    if not p.exists():
        raise FileNotFoundError(f"{p} does not exist")
    old = payload["old_string"]
    new = payload["new_string"]
    if old == new:
        raise ValueError("old_string and new_string are identical")
    from backend.agents.tools_patch import (
        PatchAmbiguous,
        PatchNotFound,
        apply_edit_to_file,
    )

    try:
        result = apply_edit_to_file(
            p,
            old,
            new,
            replace_all=bool(payload.get("replace_all")),
        )
    except PatchNotFound as exc:
        raise ValueError(str(exc)) from exc
    except PatchAmbiguous as exc:
        raise ValueError(str(exc)) from exc

    suffix = ""
    if result.match is not None:
        suffix = (
            f" via cascade layer {result.match.layer} "
            f"(confidence {result.match.score:.3f})"
        )
    return f"Replaced {result.replaced_count} occurrence(s) in {p}{suffix}"


# ─── Bash ────────────────────────────────────────────────────────


_BASH_DEFAULT_TIMEOUT_MS = 30_000
_BASH_MAX_STDOUT = 30_000
_BASH_MAX_STDERR = 10_000

# Shell metacharacters meaningful to /bin/sh but not to execvp. With
# shell=False they would be passed as literal argv entries, silently
# breaking the caller's intent — and with shell=True they were the
# RCE vector that this hardening removes (audit B4).
_SHELL_METACHARS = ("|", "&", ";", "(", ")", "<", ">", "$", "`", "\n", "\r")


def _validate_bash_command(cmd: Any) -> str:
    """Reject non-string, empty, or shell-metacharacter-bearing commands.

    The runner's Bash tool runs without a shell, so metacharacters like
    `|`, `>`, `;`, `$()` would either be misleading literals or, before
    this hardening, an injection vector.
    """
    if not isinstance(cmd, str):
        raise ValueError("command must be a string")
    stripped = cmd.strip()
    if not stripped:
        raise ValueError("command must be a non-empty string")
    for ch in _SHELL_METACHARS:
        if ch in cmd:
            raise ValueError(
                "shell metacharacter "
                f"{ch!r} is not allowed (the runner Bash tool runs "
                "without a shell; split the work into separate calls)"
            )
    return stripped


def bash_handler(payload: dict[str, Any]) -> str:
    """Run a foreground command inside ``BASE_DIR``.

    Contract:
      ``run_in_background`` is intentionally unsupported. The runner has
      no monitor channel for detached process lifecycle, so the handler
      rejects background requests before command parsing or subprocess
      dispatch.
    """
    if payload.get("run_in_background"):
        # The runner has no Monitor channel — backgrounding would orphan
        # processes when the runner exits. Refuse explicitly.
        raise NotImplementedError(
            "run_in_background is not supported in the runner; "
            "use a foreground command with `timeout` instead"
        )
    cmd = _validate_bash_command(payload.get("command"))
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise ValueError(f"invalid command syntax: {e}") from e
    if not argv:
        raise ValueError("command parsed to empty argv")
    timeout_ms = int(payload.get("timeout") or _BASH_DEFAULT_TIMEOUT_MS)
    timeout_s = max(1, timeout_ms // 1000)
    try:
        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return f"❌ command timed out after {timeout_s}s"
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if len(stdout) > _BASH_MAX_STDOUT:
        stdout = stdout[-_BASH_MAX_STDOUT:]
        stdout = "(... stdout truncated to last 30KB ...)\n" + stdout
    if len(stderr) > _BASH_MAX_STDERR:
        stderr = stderr[-_BASH_MAX_STDERR:]
        stderr = "(... stderr truncated to last 10KB ...)\n" + stderr
    return (
        f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}\nEXIT_CODE: {result.returncode}"
    )


# ─── Grep ────────────────────────────────────────────────────────


def _rg_available() -> bool:
    """True if a real ripgrep binary is on PATH (not a shell alias)."""
    return shutil.which("rg") is not None


def _build_grep_cmd(
    payload: dict[str, Any], path: Path, *, prefer_rg: bool
) -> list[str]:
    pattern = payload["pattern"]
    output_mode = payload.get("output_mode", "files_with_matches")
    if prefer_rg:
        cmd: list[str] = ["rg"]
        if payload.get("-i"):
            cmd.append("-i")
        if payload.get("-n"):
            cmd.append("-n")
        if payload.get("glob"):
            cmd.extend(["--glob", payload["glob"]])
        if payload.get("type"):
            cmd.extend(["-t", payload["type"]])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        cmd.extend(["--", pattern, str(path)])
        return cmd
    # POSIX grep fallback. Same surface, tighter feature set.
    cmd = ["grep", "-rE"]
    if payload.get("-i"):
        cmd.append("-i")
    if payload.get("-n"):
        cmd.append("-n")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    if payload.get("glob"):
        cmd.extend(["--include", payload["glob"]])
    if payload.get("type"):
        cmd.extend(["--include", f"*.{payload['type']}"])
    cmd.extend(["--", pattern, str(path)])
    return cmd


def grep_handler(payload: dict[str, Any]) -> str:
    path = (
        _ensure_inside_base(payload["path"]) if payload.get("path") else BASE_DIR
    )
    cmd = _build_grep_cmd(payload, path, prefer_rg=_rg_available())
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return "❌ grep timed out after 60s"
    # rg/grep exit codes: 0 = found, 1 = no match (not an error), 2+ = real error
    if result.returncode >= 2:
        return f"❌ {cmd[0]} exit {result.returncode}\n{result.stderr[:1000]}"
    out = result.stdout or ""
    if len(out) > 50_000:
        out = "(... output truncated to last 50KB ...)\n" + out[-50_000:]
    return out


# ─── Glob ────────────────────────────────────────────────────────


def glob_handler(payload: dict[str, Any]) -> str:
    pattern = payload["pattern"]
    base = (
        _ensure_inside_base(payload["path"]) if payload.get("path") else BASE_DIR
    )
    matches: list[str] = []
    for p in sorted(base.glob(pattern)):
        try:
            _ensure_inside_base(p)
            matches.append(str(p))
        except PermissionError:
            continue
    if len(matches) > 1000:
        matches = matches[:1000]
        matches.append("(... truncated to first 1000 matches ...)")
    return "\n".join(matches)


# ─── KnowledgeRetrieval ──────────────────────────────────────────


def _normalise_knowledge_source_path(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    path = _ensure_inside_base(raw_path)
    return path.relative_to(BASE_DIR).as_posix()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _hit_to_knowledge_result(hit: VectorHit) -> dict[str, Any]:
    line_start = _optional_int(hit.metadata.get("line_start"))
    line_end = _optional_int(hit.metadata.get("line_end"))
    citation = {
        "path": hit.source_path,
        "line_start": line_start,
        "line_end": line_end,
        "line_range": (
            f"L{line_start}-L{line_end}"
            if line_start is not None and line_end is not None
            else "unknown"
        ),
        "similarity_score": hit.score,
    }
    return {
        "chunk_id": hit.chunk_id,
        "chunk_text": hit.chunk_text,
        "citation": citation,
        "metadata": dict(hit.metadata),
    }


async def knowledge_retrieval_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Query the configured BP.Q vector index without process-local caches.

    Module-global state audit: this handler reads immutable module constants
    only. Store/embedder instances are constructed per call from env/config,
    and vector state lives in PG/Qdrant/Chroma, so uvicorn workers coordinate
    through the configured backing service rather than shared memory.
    """

    query_text = str(payload.get("query", "")).strip()
    if not query_text:
        raise ValueError("query is required")
    tenant_id = str(
        payload.get("tenant_id") or os.environ.get("OMNISIGHT_RAG_TENANT_ID")
        or DEFAULT_TENANT_ID
    ).strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    top_k = int(payload.get("top_k") or 5)
    if top_k < 1 or top_k > 20:
        raise ValueError("top_k must be between 1 and 20")
    metadata_filter = payload.get("metadata_filter") or {}
    if not isinstance(metadata_filter, dict):
        raise ValueError("metadata_filter must be an object")

    source_path = _normalise_knowledge_source_path(payload.get("source_path"))
    embedder = _build_embedder_from_env()
    store, closeable = await _build_store_from_env()
    try:
        embedding = await embedder.embed_query(query_text)
        hits = await store.query(
            VectorQuery(
                tenant_id=tenant_id,
                embedding=embedding,
                limit=top_k,
                source_path=source_path,
                metadata_filter=metadata_filter,
            )
        )
    finally:
        if closeable is not None:
            await closeable.close()

    return {
        "query": query_text,
        "tenant_id": tenant_id,
        "top_k": top_k,
        "results": [_hit_to_knowledge_result(hit) for hit in hits],
    }


# ─── Registration ────────────────────────────────────────────────


_HANDLERS: dict[str, Any] = {
    "Read": read_handler,
    "Write": write_handler,
    "Edit": edit_handler,
    "Bash": bash_handler,
    "Grep": grep_handler,
    "Glob": glob_handler,
    "KnowledgeRetrieval": knowledge_retrieval_handler,
}


def bind_to_dispatcher(dispatcher: ToolDispatcher) -> ToolDispatcher:
    """Register all runner handlers on ``dispatcher``. Returns the dispatcher."""
    for name, fn in _HANDLERS.items():
        dispatcher.register(name, fn)
    return dispatcher


def make_runner_dispatcher() -> ToolDispatcher:
    """Build a fresh dispatcher with all runner handlers wired up."""
    return bind_to_dispatcher(ToolDispatcher())
