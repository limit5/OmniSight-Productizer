"""Phase 67-B S1 — SEARCH/REPLACE + unified-diff patch application.

The output-generation acceleration from `lossless-agent-acceleration.md`
Engine 2 lives here: instead of regenerating a 2000-line file to fix 3
lines, the agent emits a diff that a host-side patcher applies. This
module IS that patcher.

Design rules (enforced by tests):

  1. SEARCH block must match the file exactly ONCE. Multi-match or
     zero-match → raise. This is the whole point — silent apply on
     the wrong occurrence corrupts code invisibly.

  2. SEARCH block must carry ≥ 3 lines of context (the design
     document locks this threshold). 1-line SEARCH is too ambiguous;
     most real files have duplicate single lines.

  3. Line endings are preserved exactly as the file has them.
     We do NOT normalise on write — corrupting `\\r\\n` into `\\n`
     breaks Windows-originated files.

  4. apply_unified_diff wraps the stdlib-ish unified diff format
     (`---`, `+++`, `@@` hunks). Multiple hunks per file supported;
     bad hunk context raises with the file_path + hunk index.

  5. No file IO in this module — callers pass the file body in and
     we return the new body. That keeps every test path pure and
     lets the caller (Phase 67-B S2) own the atomic write. Exception:
     `apply_to_file(path, …)` convenience wrapper that reads + writes
     for the common case.

Exceptions:

  * PatchNotFound     — SEARCH matched zero times
  * PatchAmbiguous    — SEARCH matched more than once
  * PatchMalformed    — input format (markers, context count, hunk
                        header) is wrong
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Locked by design — SEARCH block needs ≥ this many non-blank lines so
# the match is actually unique in typical code.
MIN_SEARCH_CONTEXT_LINES = 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PatchError(ValueError):
    """Base class — every failure the LLM caused."""


class PatchNotFound(PatchError):
    """SEARCH block not found in the source file."""


class PatchAmbiguous(PatchError):
    """SEARCH block matched more than once; need more context."""


class PatchMalformed(PatchError):
    """Caller-side contract violation — format, minimum context,
    mis-paired markers, bad hunk header."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SEARCH / REPLACE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SR_BLOCK_RE = re.compile(
    r"<{7}\s*SEARCH\s*\n"
    r"(.*?)"
    r"={7}\s*\n"
    r"(.*?)"
    r">{7}\s*REPLACE",
    re.DOTALL,
)


@dataclass(frozen=True)
class SearchReplaceBlock:
    search: str
    replace: str


def parse_search_replace(raw: str) -> list[SearchReplaceBlock]:
    """Parse one or more SEARCH/REPLACE blocks out of `raw`. Raises
    `PatchMalformed` when the markers are unbalanced or absent."""
    if not raw or not raw.strip():
        raise PatchMalformed("empty patch payload")
    blocks: list[SearchReplaceBlock] = []
    for m in _SR_BLOCK_RE.finditer(raw):
        blocks.append(SearchReplaceBlock(search=m.group(1), replace=m.group(2)))
    if not blocks:
        raise PatchMalformed(
            "no SEARCH/REPLACE block found — expected "
            "'<<<<<<< SEARCH … ======= … >>>>>>> REPLACE'"
        )
    # Leftover markers = half-paired block → mostly harmless to ignore
    # the tail but almost always a bug, so we count markers.
    start_count = raw.count("<<<<<<< SEARCH")
    end_count = raw.count(">>>>>>> REPLACE")
    if start_count != end_count or start_count != len(blocks):
        raise PatchMalformed(
            f"unbalanced SEARCH/REPLACE markers: "
            f"{start_count} SEARCH vs {end_count} REPLACE vs "
            f"{len(blocks)} parsed blocks"
        )
    return blocks


def _count_content_lines(text: str) -> int:
    """Non-blank, non-whitespace-only lines."""
    return sum(1 for line in text.splitlines() if line.strip())


def apply_search_replace(
    source: str, block: SearchReplaceBlock,
    *, min_context: int = MIN_SEARCH_CONTEXT_LINES,
) -> str:
    """Apply one SEARCH/REPLACE to `source`. Pure function."""
    if _count_content_lines(block.search) < min_context:
        raise PatchMalformed(
            f"SEARCH block has fewer than {min_context} non-blank lines "
            f"of context; patches with too little context are ambiguous"
        )
    count = source.count(block.search)
    if count == 0:
        raise PatchNotFound(
            "SEARCH block did not match any run in the source file"
        )
    if count > 1:
        raise PatchAmbiguous(
            f"SEARCH block matched {count} times — add more context"
        )
    return source.replace(block.search, block.replace, 1)


def apply_search_replace_payload(
    source: str, raw: str,
    *, min_context: int = MIN_SEARCH_CONTEXT_LINES,
) -> str:
    """Apply EVERY SEARCH/REPLACE block in `raw` against `source`, in
    order. Each block must be uniquely matchable AGAINST THE RESULT
    of the previous block — so the agent can chain edits safely."""
    blocks = parse_search_replace(raw)
    out = source
    for i, blk in enumerate(blocks):
        try:
            out = apply_search_replace(out, blk, min_context=min_context)
        except PatchError as exc:
            raise type(exc)(f"block {i + 1}/{len(blocks)}: {exc}") from None
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unified diff
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]  # each line begins with ' ', '-', or '+' (or '')


def _parse_unified_diff(raw: str) -> list[Hunk]:
    """Parse ONE file worth of unified diff into hunks. Ignores the
    leading `---` / `+++` headers; the caller knows which file this
    is for (we never trust paths from the LLM here)."""
    lines = raw.splitlines()
    hunks: list[Hunk] = []
    i = 0
    while i < len(lines):
        m = _HUNK_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2) or 1)
        new_start = int(m.group(3))
        new_count = int(m.group(4) or 1)
        j = i + 1
        body: list[str] = []
        # A hunk ends at the next '@@' or end of input.
        while j < len(lines) and not lines[j].startswith("@@"):
            # Stop at diff boundary markers too.
            if lines[j].startswith(("--- ", "+++ ")):
                break
            body.append(lines[j])
            j += 1
        hunks.append(Hunk(old_start, old_count, new_start, new_count, body))
        i = j
    if not hunks:
        raise PatchMalformed("unified diff contains no valid hunk headers")
    return hunks


def _apply_hunk(source_lines: list[str], h: Hunk,
                *, hunk_idx: int) -> list[str]:
    """Apply one hunk by walking source_lines starting at old_start.
    Strict context match — if a context line doesn't equal source,
    raise `PatchNotFound` pointing at the hunk index."""
    if h.old_start < 1 or h.old_start > len(source_lines) + 1:
        raise PatchNotFound(
            f"hunk {hunk_idx}: old_start={h.old_start} out of range "
            f"for file with {len(source_lines)} lines"
        )

    out = list(source_lines[: h.old_start - 1])
    src_idx = h.old_start - 1  # 0-based
    for raw_line in h.lines:
        if not raw_line:
            # Empty element in body — treat as a context blank.
            prefix, body = " ", ""
        else:
            prefix, body = raw_line[0], raw_line[1:]
        if prefix == " ":
            if src_idx >= len(source_lines) or source_lines[src_idx] != body:
                raise PatchNotFound(
                    f"hunk {hunk_idx}: context mismatch at source line "
                    f"{src_idx + 1}: expected {body!r}, found "
                    f"{source_lines[src_idx] if src_idx < len(source_lines) else '<EOF>'!r}"
                )
            out.append(body)
            src_idx += 1
        elif prefix == "-":
            if src_idx >= len(source_lines) or source_lines[src_idx] != body:
                raise PatchNotFound(
                    f"hunk {hunk_idx}: removal line mismatch at source "
                    f"line {src_idx + 1}: expected {body!r}"
                )
            src_idx += 1  # consumed from source, NOT appended to out
        elif prefix == "+":
            out.append(body)
        else:
            # Could be a diff "\ No newline at end of file" marker;
            # skip silently.
            continue

    # Append the untouched tail.
    out.extend(source_lines[src_idx:])
    return out


def apply_unified_diff(source: str, diff: str) -> str:
    """Apply a unified diff (one file's worth) to `source`. Returns
    the new body. Preserves the original newline convention."""
    # Detect the line terminator in the source; we'll reuse it when
    # joining so we don't corrupt CRLF files.
    newline = "\n"
    if "\r\n" in source[:4096]:
        newline = "\r\n"

    # splitlines() loses the trailing newline info; track it.
    had_trailing_newline = source.endswith(newline) or source.endswith("\n")
    source_lines = source.splitlines()

    hunks = _parse_unified_diff(diff)
    # Apply hunks LAST → FIRST so earlier line numbers stay valid as
    # we mutate.
    out_lines = source_lines
    for idx, h in enumerate(sorted(hunks, key=lambda x: -x.old_start), start=1):
        # Use the original hunks list index for error messages.
        real_idx = hunks.index(h) + 1
        out_lines = _apply_hunk(out_lines, h, hunk_idx=real_idx)

    joined = newline.join(out_lines)
    if had_trailing_newline and not joined.endswith(newline):
        joined += newline
    return joined


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  File convenience wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def apply_to_file(path: Path, patch_kind: str, payload: str) -> None:
    """Read file → apply → write atomically (temp file + rename).
    `patch_kind` is ``"search_replace"`` or ``"unified_diff"``.
    Never creates a new file — refuses with `PatchNotFound` if the
    target doesn't exist (new-file creation goes through a separate
    `create_file` tool per Phase 67-B spec)."""
    p = Path(path)
    if not p.is_file():
        raise PatchNotFound(
            f"apply_to_file: {path} does not exist (use create_file for new files)"
        )
    body = p.read_text(encoding="utf-8")
    if patch_kind == "search_replace":
        new = apply_search_replace_payload(body, payload)
    elif patch_kind == "unified_diff":
        new = apply_unified_diff(body, payload)
    else:
        raise PatchMalformed(f"unknown patch_kind {patch_kind!r}")
    tmp = p.with_suffix(p.suffix + ".omnisight-patch-tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX
