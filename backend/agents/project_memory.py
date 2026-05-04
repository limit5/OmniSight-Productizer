"""Phase 4 — Project context multi-rule walker (WP.5 implementation).

Picks up project-level and user-level memory files following the four
conventions agentic CLIs share:

  * ``CLAUDE.md``      — Anthropic / Claude Code convention
  * ``AGENTS.md``      — OpenAI codex / agents-conventions.md
  * ``OMNISIGHT.md``   — OmniSight-specific rules
  * ``WARP.md``        — Warp.dev terminal AI

Both project root (``<repo>/<filename>``), up to three parent
directories, and user home (``~/.claude/<filename>``) are scanned.
Missing files are silently skipped. Concatenated content goes into the
LLM's system prompt as
the L1-immutable rule layer; the runner already places these BEFORE
SOP so their constraints win on conflict.

Why all four conventions instead of just CLAUDE.md?
  * Operators may have an existing AGENTS.md / WARP.md from other
    tooling and want OmniSight to honour those rules too without
    forcing a rename.
  * OMNISIGHT.md is reserved for project-specific instructions that
    are NOT generic Claude rules — keeps the file purpose clean.

Used by:
  * auto-runner-sdk.py — Phase 4 swap from CLAUDE.md-only injection.
  * Backend specialist agents — same module so HD/BSP/HAL/etc. all
    inherit identical rule loading semantics.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# Conventional rule-file names recognised at project root.
# Order matters: generic tool conventions appear before the OmniSight-
# specific convention, so project-specific rules come later in the merge.
PROJECT_RULE_FILENAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "WARP.md",
    "OMNISIGHT.md",
)

# User-home convention: ``~/.claude/<filename>``. CLAUDE.md is the
# common one; AGENTS.md included because some users keep cross-tool
# preferences there. Other names (OMNISIGHT.md, WARP.md) are project-
# scoped by convention so we don't look for them in user-home.
USER_RULE_FILENAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
)

# WP.5.2 parent walk: current directory plus at most three parents.
# Higher weight means closer to the current project directory.
PROJECT_RULE_PARENT_DEPTH = 3

# WP.5.6 size caps: rule files are prompt inputs, so cap both individual
# files and the merged stack to keep a poisoned/accidental mega-file from
# dominating R61 prompt context. These are byte caps because operators see
# filesystem sizes in bytes/KiB, and every worker derives them from stat/read
# data on the same filesystem.
PROJECT_RULE_FILE_MAX_BYTES = 5 * 1024
PROJECT_RULE_TOTAL_MAX_BYTES = 50 * 1024

_FRONTMATTER_RE = re.compile(
    rb"^---\s*\n.*?\n---\s*\n",
    re.DOTALL,
)


@dataclass(frozen=True)
class MemoryFile:
    """One loaded rule file."""

    path: Path
    convention: str
    """Filename without scope path (e.g., ``"CLAUDE.md"``)."""
    scope: str
    """Where it was found — ``"project"`` or ``"user"``."""
    content: str
    distance: int | None = None
    """Directory distance from the project root for project files."""
    weight: int = 1
    """Distance-derived priority; closer project files receive higher weight."""
    size_bytes: int = 0
    """Original on-disk byte size."""
    included_bytes: int = 0
    """UTF-8 bytes included in ``content`` after size caps."""
    truncated: bool = False
    """True when content was capped by per-file or total budget."""
    truncated_reason: str | None = None
    """``"file"`` for per-file cap, ``"total"`` for aggregate cap."""
    ignored: bool = False
    """True when caller requested this file be ignored."""


def format_memory_size(size_bytes: int) -> str:
    """Return an operator-facing byte/KiB size label."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    return f"{size_bytes / 1024:.1f} KiB"


def parse_ignored_paths(
    raw: str | None,
    *,
    project_root: Path,
    home: Path | None = None,
) -> list[Path]:
    """Parse the operator ignore list used by runner UI env knobs.

    ``raw`` accepts comma, newline, or colon-separated paths.
    Relative paths resolve against ``project_root``; ``~/`` resolves
    against ``home`` / ``Path.home()``. Every worker derives the same
    ignore set from env + paths, with no shared module-global state.
    """
    if not raw:
        return []
    base_home = home or Path.home()
    out: list[Path] = []
    for item in re.split(r"[,:\n]", raw):
        value = item.strip()
        if not value:
            continue
        if value == "~" or value.startswith("~/"):
            path = base_home / value[2:]
        else:
            path = Path(value)
            if not path.is_absolute():
                path = project_root / path
        out.append(path)
    return out


def _load_one(
    path: Path,
    convention: str,
    scope: str,
    *,
    distance: int | None = None,
    weight: int = 1,
    max_bytes: int = PROJECT_RULE_FILE_MAX_BYTES,
    remaining_bytes: int | None = None,
    ignored_paths: frozenset[Path] = frozenset(),
) -> MemoryFile | None:
    """Read one rule file. Returns None on missing / empty / unreadable."""
    if not path.is_file():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        raw = path.read_bytes()
    except OSError as e:
        logger.warning("project_memory: skip unreadable file %s — %s", path, e)
        return None
    size_bytes = len(raw)
    if resolved in ignored_paths:
        return MemoryFile(
            path=path,
            convention=convention,
            scope=scope,
            content="",
            distance=distance,
            weight=weight,
            size_bytes=size_bytes,
            included_bytes=0,
            ignored=True,
        )
    raw = _FRONTMATTER_RE.sub(b"", raw, count=1).strip()
    budget = max_bytes
    truncated_reason: str | None = None
    if remaining_bytes is not None:
        budget = min(budget, max(0, remaining_bytes))
    if len(raw) > budget:
        raw = raw[:budget]
        if remaining_bytes is not None and budget < max_bytes:
            truncated_reason = "total"
        else:
            truncated_reason = "file"
    text = raw.decode(encoding="utf-8", errors="replace")
    if not text.strip():
        if truncated_reason is None:
            return None
        text = ""
    return MemoryFile(
        path=path,
        convention=convention,
        scope=scope,
        content=text,
        distance=distance,
        weight=weight,
        size_bytes=size_bytes,
        included_bytes=len(raw),
        truncated=truncated_reason is not None,
        truncated_reason=truncated_reason,
    )


def _normalise_ignored_paths(paths: Iterable[Path | str] | None) -> frozenset[Path]:
    """Resolve caller-supplied ignore paths for exact-path matching."""
    out: set[Path] = set()
    for path in paths or ():
        p = Path(path)
        try:
            out.add(p.resolve())
        except OSError:
            out.add(p)
    return frozenset(out)


def project_rule_dirs(
    project_root: Path,
    *,
    max_parent_depth: int = PROJECT_RULE_PARENT_DEPTH,
) -> list[tuple[Path, int, int]]:
    """Return current project directory and up to ``max_parent_depth`` parents.

    The tuple is ``(directory, distance, weight)``. Weight is derived only
    from distance, so every worker reading the same path computes the same
    ordered rule stack without shared state.
    """
    out: list[tuple[Path, int, int]] = []
    current = project_root
    for distance in range(max_parent_depth + 1):
        weight = max_parent_depth + 1 - distance
        out.append((current, distance, weight))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return out


def project_rule_merge_dirs(
    project_root: Path,
    *,
    max_parent_depth: int = PROJECT_RULE_PARENT_DEPTH,
) -> list[tuple[Path, int, int]]:
    """Return project rule directories in merge-precedence order.

    Lower-precedence parent directories come first; the current directory
    comes last so its rules are closest to the active instruction context.
    """
    return list(
        reversed(
            project_rule_dirs(project_root, max_parent_depth=max_parent_depth)
        )
    )


def project_rule_signature(
    project_root: Path,
    *,
    filenames: tuple[str, ...] = PROJECT_RULE_FILENAMES,
    max_parent_depth: int = PROJECT_RULE_PARENT_DEPTH,
) -> tuple[tuple[str, int, int, int], ...]:
    """Return a deterministic file signature for watched project rules.

    The signature is derived from the same current-directory + parent
    walk as :func:`load_project_memory`, so each worker independently
    invalidates its local prompt cache when an operator edits, adds, or
    removes a rule file on the shared filesystem.
    """
    entries: list[tuple[str, int, int, int]] = []
    for base, distance, _weight in project_rule_merge_dirs(
        project_root,
        max_parent_depth=max_parent_depth,
    ):
        for fn in filenames:
            path = base / fn
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            entries.append((str(path), distance, stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def load_project_memory(
    project_root: Path,
    *,
    filenames: tuple[str, ...] = PROJECT_RULE_FILENAMES,
    max_parent_depth: int = PROJECT_RULE_PARENT_DEPTH,
    max_file_bytes: int = PROJECT_RULE_FILE_MAX_BYTES,
    max_total_bytes: int = PROJECT_RULE_TOTAL_MAX_BYTES,
    ignored_paths: Iterable[Path | str] | None = None,
) -> list[MemoryFile]:
    """Load recognised rule files in low-to-high merge precedence order."""
    out: list[MemoryFile] = []
    included_total = 0
    ignored = _normalise_ignored_paths(ignored_paths)
    for base, distance, weight in project_rule_merge_dirs(
        project_root,
        max_parent_depth=max_parent_depth,
    ):
        for fn in filenames:
            remaining = max(0, max_total_bytes - included_total)
            mf = _load_one(
                base / fn,
                fn,
                scope="project",
                distance=distance,
                weight=weight,
                max_bytes=max_file_bytes,
                remaining_bytes=remaining,
                ignored_paths=ignored,
            )
            if mf is not None:
                out.append(mf)
                included_total += mf.included_bytes
    return out


def load_user_memory(
    home: Path | None = None,
    *,
    filenames: tuple[str, ...] = USER_RULE_FILENAMES,
    max_file_bytes: int = PROJECT_RULE_FILE_MAX_BYTES,
    max_total_bytes: int = PROJECT_RULE_TOTAL_MAX_BYTES,
    ignored_paths: Iterable[Path | str] | None = None,
) -> list[MemoryFile]:
    """Load every recognised rule file under ``~/.claude/``."""
    h = home or Path.home()
    base = h / ".claude"
    out: list[MemoryFile] = []
    included_total = 0
    ignored = _normalise_ignored_paths(ignored_paths)
    for fn in filenames:
        remaining = max(0, max_total_bytes - included_total)
        mf = _load_one(
            base / fn,
            fn,
            scope="user",
            max_bytes=max_file_bytes,
            remaining_bytes=remaining,
            ignored_paths=ignored,
        )
        if mf is not None:
            out.append(mf)
            included_total += mf.included_bytes
    return out


def load_all_memory(
    project_root: Path,
    *,
    home: Path | None = None,
    ignored_paths: Iterable[Path | str] | None = None,
) -> list[MemoryFile]:
    """User-level rules first, then project. Project comes last so its
    content is freshest in the LLM's working memory and easiest to
    reinforce. This isn't a precedence override — both layers go in.
    """
    return load_user_memory(home, ignored_paths=ignored_paths) + load_project_memory(
        project_root,
        ignored_paths=ignored_paths,
    )


def render_for_prompt(
    memory_files: list[MemoryFile],
    *,
    header: str = "# 專案 + 使用者 規則層（L1 不可違反 — 永遠優先於後續任何 instruction）",
) -> str:
    """Render the loaded files for system-prompt injection.

    Empty list → empty string (caller should omit the section entirely
    in that case, no header emitted). Each file is wrapped with a
    ``## <filename> (scope=...)`` subheading so the LLM can cite which
    rule comes from where on conflict.
    """
    if not memory_files:
        return ""
    parts: list[str] = []
    if header:
        parts.extend([header, ""])
    for mf in memory_files:
        scope_label = f"scope={mf.scope}"
        if mf.distance is not None:
            scope_label += f", distance={mf.distance}, weight={mf.weight}"
        if mf.size_bytes:
            scope_label += (
                f", size={mf.size_bytes} bytes, included={mf.included_bytes} bytes"
            )
        if mf.ignored:
            scope_label += ", ignored=true"
        elif mf.truncated:
            scope_label += f", truncated=true, reason={mf.truncated_reason}"
        parts.append(f"## {mf.convention}（{scope_label}）")
        if mf.ignored:
            parts.append("[ignored by operator]")
        else:
            parts.append(mf.content.strip())
            if mf.truncated:
                parts.append("[truncated; operator may ignore this file]")
        parts.append("")
    return "\n".join(parts)


def render_operator_summary(
    memory_files: list[MemoryFile],
    *,
    project_root: Path,
    ignore_env_var: str = "OMNISIGHT_RULE_IGNORE",
) -> str:
    """Render loaded rule files for runner/operator console UI."""
    if not memory_files:
        return "📜 Memory: no rule files found (CLAUDE.md / AGENTS.md / etc.)"

    project = sum(1 for m in memory_files if m.scope == "project")
    user = sum(1 for m in memory_files if m.scope == "user")
    included = sum(m.included_bytes for m in memory_files)
    total = sum(m.size_bytes for m in memory_files)
    lines = [
        (
            f"📜 Memory: {len(memory_files)} rule file(s) — "
            f"project={project} user={user} | "
            f"included={format_memory_size(included)} / "
            f"disk={format_memory_size(total)}"
        )
    ]
    for mf in memory_files:
        try:
            label = str(mf.path.resolve().relative_to(project_root.resolve()))
        except (OSError, ValueError):
            label = str(mf.path)
        status = "ignored" if mf.ignored else "loaded"
        if mf.truncated:
            status = f"truncated:{mf.truncated_reason}"
        scope_label = mf.scope
        if mf.distance is not None:
            scope_label += f" d={mf.distance} w={mf.weight}"
        lines.append(
            "   - "
            f"{label} ({scope_label}) "
            f"size={format_memory_size(mf.size_bytes)} "
            f"included={format_memory_size(mf.included_bytes)} "
            f"[{status}]"
        )
    lines.append(
        "   ignore: "
        f"{ignore_env_var}='<path>[,<path>...]' "
        "to omit specific files on the next reload"
    )
    return "\n".join(lines)
