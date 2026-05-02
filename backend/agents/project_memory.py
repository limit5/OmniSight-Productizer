"""Phase 4 — Project context multi-rule walker (WP.5 implementation).

Picks up project-level and user-level memory files following the four
conventions agentic CLIs share:

  * ``CLAUDE.md``      — Anthropic / Claude Code convention
  * ``AGENTS.md``      — OpenAI codex / agents-conventions.md
  * ``OMNISIGHT.md``   — OmniSight-specific rules
  * ``WARP.md``        — Warp.dev terminal AI

Both project root (``<repo>/<filename>``) and user home
(``~/.claude/<filename>``) are scanned. Missing files are silently
skipped. Concatenated content goes into the LLM's system prompt as
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
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Conventional rule-file names recognised at project root.
# Order matters: when both files exist they appear in this order in the
# rendered system prompt, so CLAUDE.md is read first.
PROJECT_RULE_FILENAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "OMNISIGHT.md",
    "WARP.md",
)

# User-home convention: ``~/.claude/<filename>``. CLAUDE.md is the
# common one; AGENTS.md included because some users keep cross-tool
# preferences there. Other names (OMNISIGHT.md, WARP.md) are project-
# scoped by convention so we don't look for them in user-home.
USER_RULE_FILENAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
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


def _load_one(path: Path, convention: str, scope: str) -> MemoryFile | None:
    """Read one rule file. Returns None on missing / empty / unreadable."""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("project_memory: skip unreadable file %s — %s", path, e)
        return None
    if not text.strip():
        return None
    return MemoryFile(path=path, convention=convention, scope=scope, content=text)


def load_project_memory(
    project_root: Path,
    *,
    filenames: tuple[str, ...] = PROJECT_RULE_FILENAMES,
) -> list[MemoryFile]:
    """Load every recognised rule file under ``project_root``."""
    out: list[MemoryFile] = []
    for fn in filenames:
        mf = _load_one(project_root / fn, fn, scope="project")
        if mf is not None:
            out.append(mf)
    return out


def load_user_memory(
    home: Path | None = None,
    *,
    filenames: tuple[str, ...] = USER_RULE_FILENAMES,
) -> list[MemoryFile]:
    """Load every recognised rule file under ``~/.claude/``."""
    h = home or Path.home()
    base = h / ".claude"
    out: list[MemoryFile] = []
    for fn in filenames:
        mf = _load_one(base / fn, fn, scope="user")
        if mf is not None:
            out.append(mf)
    return out


def load_all_memory(
    project_root: Path,
    *,
    home: Path | None = None,
) -> list[MemoryFile]:
    """User-level rules first, then project. Project comes last so its
    content is freshest in the LLM's working memory and easiest to
    reinforce. This isn't a precedence override — both layers go in.
    """
    return load_user_memory(home) + load_project_memory(project_root)


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
    parts: list[str] = [header, ""]
    for mf in memory_files:
        parts.append(f"## {mf.convention}（scope={mf.scope}）")
        parts.append(mf.content.strip())
        parts.append("")
    return "\n".join(parts)
