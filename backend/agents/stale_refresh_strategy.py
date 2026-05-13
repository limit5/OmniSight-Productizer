"""SP-B-X-006 (C5) — Semantic drift refresh strategy.

Long agent loops drift: by iter 30 the model still "remembers" the file
content it saw at iter 5, even though tool calls have rewritten it many
times since. Mitigation per design doc §C5 line 617: every N iterations,
inject an unsolicited ``view`` of one of the touched files so the model
is forced to re-read fresh content.

This module owns three concerns:
  - environment-driven policy config (every-N, strategy name, max tokens)
  - the stateless picker ``pick_refresh_target``
  - a tiny helper that tracks which files have been mutated by tool calls
    (text_editor / Edit / Write) so the picker has something to choose from
"""

from __future__ import annotations

import logging
import os
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_STRATEGIES = ("most-edited", "random-touched", "all-touched")
DEFAULT_STRATEGY = "most-edited"
DEFAULT_EVERY_N_ITER = 10
DEFAULT_MAX_TOKENS = 1000

ENV_EVERY_N = "OMNISIGHT_RUNNER_STALE_REFRESH_EVERY_N_ITER"
ENV_STRATEGY = "OMNISIGHT_RUNNER_STALE_REFRESH_STRATEGY"
ENV_MAX_TOKENS = "OMNISIGHT_RUNNER_STALE_REFRESH_MAX_TOKENS"

# Tool names whose successful invocation should count as a file edit.
_MUTATING_TOOL_NAMES = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_TEXT_EDITOR_TOOL = "str_replace_based_edit_tool"
_TEXT_EDITOR_MUTATING_CMDS = frozenset({"create", "str_replace", "insert", "undo_edit"})


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def get_refresh_every_n() -> int:
    return _read_int_env(ENV_EVERY_N, DEFAULT_EVERY_N_ITER)


def get_refresh_strategy() -> str:
    value = os.environ.get(ENV_STRATEGY, DEFAULT_STRATEGY)
    if value not in VALID_STRATEGIES:
        return DEFAULT_STRATEGY
    return value


def get_refresh_max_tokens() -> int:
    return _read_int_env(ENV_MAX_TOKENS, DEFAULT_MAX_TOKENS)


def pick_refresh_target(
    touched_files: Mapping[str, int] | Sequence[str] | None,
    strategy: str,
    *,
    iteration: int = 0,
    rng: random.Random | None = None,
) -> str | None:
    """Pick a single file to refresh, or return ``None`` when nothing applies.

    ``touched_files`` accepts two shapes:
      - ``Mapping[path, edit_count]`` — used by ``most-edited`` to break ties
      - ``Sequence[path]`` — order preserved for ``all-touched`` round-robin

    Unknown strategies fall back to ``most-edited`` so a malformed env var
    can't take the refresh policy offline.
    """
    if not touched_files:
        return None

    if isinstance(touched_files, Mapping):
        paths = list(touched_files.keys())
        counts: Mapping[str, int] = touched_files
    else:
        paths = list(touched_files)
        counts = {path: 1 for path in paths}

    if not paths:
        return None

    if strategy == "random-touched":
        chooser = rng if rng is not None else random
        return chooser.choice(paths)

    if strategy == "all-touched":
        return paths[iteration % len(paths)]

    # most-edited (and fallback for unknown strategy)
    return max(paths, key=lambda path: counts.get(path, 0))


def estimate_tokens(content: str) -> int:
    """Anthropic's published rule of thumb: ~4 characters per token."""
    if not content:
        return 0
    return len(content) // 4 + 1


def extract_mutated_path(tool_use: Mapping[str, Any]) -> str | None:
    """Return the file path mutated by this tool_use block, or ``None``.

    Recognises the OmniSight Edit / Write / MultiEdit / NotebookEdit
    schemas (which use ``file_path``) and Anthropic's built-in
    ``str_replace_based_edit_tool`` (``path`` + mutating ``command``).
    Pure-view calls and tools that don't write files return ``None``.
    """
    name = tool_use.get("name", "")
    raw_input = tool_use.get("input") or {}
    if not isinstance(raw_input, Mapping):
        return None

    if name in _MUTATING_TOOL_NAMES:
        path = raw_input.get("file_path") or raw_input.get("notebook_path")
        return path if isinstance(path, str) and path else None

    if name == _TEXT_EDITOR_TOOL:
        command = raw_input.get("command")
        if command not in _TEXT_EDITOR_MUTATING_CMDS:
            return None
        path = raw_input.get("path")
        return path if isinstance(path, str) and path else None

    return None


def read_file_for_refresh(
    path: str | Path,
    *,
    worktree_root: Path | None = None,
) -> str | None:
    """Read the file content for refresh, or return ``None`` if it can't be read.

    When ``worktree_root`` is supplied, paths resolving outside the root are
    rejected — a defence-in-depth check on top of the dispatcher's own
    path-safety enforcement at edit time.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and worktree_root is not None:
        candidate = worktree_root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if worktree_root is not None:
        try:
            resolved.relative_to(worktree_root.resolve())
        except ValueError:
            return None
    if not resolved.is_file():
        return None
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def build_refresh_marker(target: str, iteration: int, strategy: str) -> str:
    """Format the transcript marker line used by AC: ``[stale-refresh-injected] ...``."""
    return f"[stale-refresh-injected] view {target} at iter {iteration} (strategy={strategy})"


def build_skip_marker(target: str, iteration: int, tokens: int, max_tokens: int) -> str:
    """Format the cost-gate skip marker used by AC: ``[stale-refresh-skipped] cost-budget-exceeded``."""
    return (
        f"[stale-refresh-skipped] cost-budget-exceeded view {target} at iter {iteration} "
        f"(estimated_tokens={tokens}, max_tokens={max_tokens})"
    )
