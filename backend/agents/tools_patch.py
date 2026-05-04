"""Phase 67-B S1 — SEARCH/REPLACE + unified-diff patch application.

The output-generation acceleration from `lossless-agent-acceleration.md`
Engine 2 lives here: instead of regenerating a 2000-line file to fix 3
lines, the agent emits a diff that a host-side patcher applies. This
module IS that patcher.

Design rules (enforced by tests):

  1. SEARCH block resolves through the WP.3.1 cascade: exact match,
     indent-agnostic match, prefix-tail rescue, then Jaro-Winkler
     similarity >= 0.9. Each layer carries a confidence score and
     must produce exactly one match. Multi-match or zero-match → raise.
     This is the whole point — silent apply on the wrong occurrence
     corrupts code invisibly.

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
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Locked by design — SEARCH block needs ≥ this many non-blank lines so
# the match is actually unique in typical code.
MIN_SEARCH_CONTEXT_LINES = 3
REPO_ROOT = Path(__file__).resolve().parents[2]
N10_LEDGER_PATH = REPO_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"

_CASCADE_LAYER_CONFIDENCE = {
    1: 1.0,
    2: 0.98,
    3: 0.94,
}
DEFAULT_JARO_WINKLER_THRESHOLD = 0.9
DIFF_VALIDATION_ENABLED_ENV = "OMNISIGHT_WP_DIFF_VALIDATION_ENABLED"
HD_BRINGUP_STRICT_JARO_WINKLER_THRESHOLD = 0.95
HD_BRINGUP_STRICT_SUFFIXES = (
    ".dts",
    ".dtsi",
    ".dtso",
    ".bb",
    ".bbappend",
)


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


@dataclass(frozen=True)
class CascadeMatch:
    layer: int
    start: int
    end: int
    score: float


@dataclass(frozen=True)
class DiffValidationLedgerEvent:
    path: str
    patch_kind: str
    layer: int
    score: float
    disposition: str = "applied"
    notes: str = ""


@dataclass(frozen=True)
class EditApplyResult:
    replaced_count: int
    match: CascadeMatch | None = None


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


def _line_without_newline(line: str) -> str:
    return line.rstrip("\r\n")


def _indent_agnostic_lines(text: str) -> list[str]:
    return [_line_without_newline(line).lstrip(" \t") for line in text.splitlines()]


def _line_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    total = 0
    for line in lines:
        total += len(line)
        offsets.append(total)
    return offsets


def _window_matches(source: str, search: str) -> list[CascadeMatch]:
    source_lines = source.splitlines(keepends=True)
    search_lines = search.splitlines()
    if not search_lines or len(search_lines) > len(source_lines):
        return []

    offsets = _line_offsets(source_lines)
    window_size = len(search_lines)
    matches: list[CascadeMatch] = []
    for start_line in range(0, len(source_lines) - window_size + 1):
        end_line = start_line + window_size
        matches.append(
            CascadeMatch(
                layer=0,
                start=offsets[start_line],
                end=offsets[end_line],
                score=0.0,
            )
        )
    return matches


def _unique_match(matches: list[CascadeMatch], *, layer: int) -> CascadeMatch | None:
    if not matches:
        return None
    if len(matches) > 1:
        raise PatchAmbiguous(
            f"SEARCH block matched {len(matches)} times at cascade layer {layer}"
        )
    return matches[0]


def _find_exact_match(source: str, search: str) -> CascadeMatch | None:
    count = source.count(search)
    if count == 0:
        return None
    if count > 1:
        raise PatchAmbiguous(
            f"SEARCH block matched {count} times — add more context"
        )
    start = source.index(search)
    return CascadeMatch(
        layer=1,
        start=start,
        end=start + len(search),
        score=_CASCADE_LAYER_CONFIDENCE[1],
    )


def diff_validation_enabled() -> bool:
    """Return whether the WP.3 fuzzy cascade is enabled.

    The rollback knob is read lazily per call, so every worker derives
    the same value from its process environment without relying on
    shared module-global state.
    """

    raw = (os.environ.get(DIFF_VALIDATION_ENABLED_ENV) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _find_indent_agnostic_match(source: str, search: str) -> CascadeMatch | None:
    search_norm = _indent_agnostic_lines(search)
    matches: list[CascadeMatch] = []
    for match in _window_matches(source, search):
        window_norm = _indent_agnostic_lines(source[match.start:match.end])
        if window_norm == search_norm:
            matches.append(
                CascadeMatch(
                    layer=2,
                    start=match.start,
                    end=match.end,
                    score=_CASCADE_LAYER_CONFIDENCE[2],
                )
            )
    return _unique_match(matches, layer=2)


def _find_prefix_tail_match(source: str, search: str) -> CascadeMatch | None:
    search_norm = _indent_agnostic_lines(search)
    content_indexes = [i for i, line in enumerate(search_norm) if line.strip()]
    if len(content_indexes) < 2:
        return None
    first_idx = content_indexes[0]
    last_idx = content_indexes[-1]
    first_line = search_norm[first_idx]
    last_line = search_norm[last_idx]

    matches: list[CascadeMatch] = []
    for match in _window_matches(source, search):
        window_norm = _indent_agnostic_lines(source[match.start:match.end])
        if (
            len(window_norm) == len(search_norm)
            and window_norm[first_idx] == first_line
            and window_norm[last_idx] == last_line
        ):
            matches.append(
                CascadeMatch(
                    layer=3,
                    start=match.start,
                    end=match.end,
                    score=_CASCADE_LAYER_CONFIDENCE[3],
                )
            )
    return _unique_match(matches, layer=3)


def _jaro_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    match_distance = max(len(a), len(b)) // 2 - 1
    a_matches = [False] * len(a)
    b_matches = [False] * len(b)
    matches = 0

    for i, char in enumerate(a):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len(b))
        for j in range(start, end):
            if b_matches[j] or char != b[j]:
                continue
            a_matches[i] = True
            b_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    j = 0
    for i, char in enumerate(a):
        if not a_matches[i]:
            continue
        while not b_matches[j]:
            j += 1
        if char != b[j]:
            transpositions += 1
        j += 1

    return (
        matches / len(a)
        + matches / len(b)
        + (matches - transpositions / 2) / matches
    ) / 3


def _jaro_winkler_similarity(a: str, b: str) -> float:
    jaro = _jaro_similarity(a, b)
    prefix = 0
    for left, right in zip(a[:4], b[:4]):
        if left != right:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def _find_jaro_winkler_match(
    source: str,
    search: str,
    *,
    threshold: float = DEFAULT_JARO_WINKLER_THRESHOLD,
) -> CascadeMatch | None:
    search_norm = "\n".join(_indent_agnostic_lines(search))
    matches: list[CascadeMatch] = []
    for match in _window_matches(source, search):
        window_norm = "\n".join(
            _indent_agnostic_lines(source[match.start:match.end])
        )
        score = _jaro_winkler_similarity(window_norm, search_norm)
        if score >= threshold:
            matches.append(
                CascadeMatch(
                    layer=4,
                    start=match.start,
                    end=match.end,
                    score=score,
                )
            )
    return _unique_match(matches, layer=4)


def find_search_replace_match(
    source: str,
    search: str,
    *,
    jaro_winkler_threshold: float = DEFAULT_JARO_WINKLER_THRESHOLD,
) -> CascadeMatch:
    """Resolve a SEARCH block through the WP.3.1 cascade.

    Pure function: no module-global mutable state; every worker derives
    the same result from the supplied source/search strings and process
    environment.
    """
    match = _find_exact_match(source, search)
    if match is not None:
        return match
    if not diff_validation_enabled():
        raise PatchNotFound("SEARCH block did not match exactly in the source file")
    for finder in (_find_indent_agnostic_match, _find_prefix_tail_match):
        match = finder(source, search)
        if match is not None:
            return match
    match = _find_jaro_winkler_match(
        source,
        search,
        threshold=jaro_winkler_threshold,
    )
    if match is not None:
        return match
    raise PatchNotFound("SEARCH block did not match any run in the source file")


def apply_search_replace(
    source: str, block: SearchReplaceBlock,
    *, min_context: int = MIN_SEARCH_CONTEXT_LINES,
    jaro_winkler_threshold: float = DEFAULT_JARO_WINKLER_THRESHOLD,
) -> str:
    """Apply one SEARCH/REPLACE to `source`. Pure function."""
    if _count_content_lines(block.search) < min_context:
        raise PatchMalformed(
            f"SEARCH block has fewer than {min_context} non-blank lines "
            f"of context; patches with too little context are ambiguous"
        )
    match = find_search_replace_match(
        source,
        block.search,
        jaro_winkler_threshold=jaro_winkler_threshold,
    )
    return source[:match.start] + block.replace + source[match.end:]


def _apply_search_replace_with_match(
    source: str, block: SearchReplaceBlock,
    *, min_context: int = MIN_SEARCH_CONTEXT_LINES,
    jaro_winkler_threshold: float = DEFAULT_JARO_WINKLER_THRESHOLD,
) -> tuple[str, CascadeMatch]:
    if _count_content_lines(block.search) < min_context:
        raise PatchMalformed(
            f"SEARCH block has fewer than {min_context} non-blank lines "
            f"of context; patches with too little context are ambiguous"
        )
    match = find_search_replace_match(
        source,
        block.search,
        jaro_winkler_threshold=jaro_winkler_threshold,
    )
    return source[:match.start] + block.replace + source[match.end:], match


def apply_search_replace_payload(
    source: str, raw: str,
    *, min_context: int = MIN_SEARCH_CONTEXT_LINES,
    jaro_winkler_threshold: float = DEFAULT_JARO_WINKLER_THRESHOLD,
) -> str:
    """Apply EVERY SEARCH/REPLACE block in `raw` against `source`, in
    order. Each block must be uniquely matchable AGAINST THE RESULT
    of the previous block — so the agent can chain edits safely."""
    blocks = parse_search_replace(raw)
    out = source
    for i, blk in enumerate(blocks):
        try:
            out, _match = _apply_search_replace_with_match(
                out,
                blk,
                min_context=min_context,
                jaro_winkler_threshold=jaro_winkler_threshold,
            )
        except PatchError as exc:
            raise type(exc)(f"block {i + 1}/{len(blocks)}: {exc}") from None
    return out


def _apply_search_replace_payload_with_matches(
    source: str, raw: str,
    *, min_context: int = MIN_SEARCH_CONTEXT_LINES,
    jaro_winkler_threshold: float = DEFAULT_JARO_WINKLER_THRESHOLD,
) -> tuple[str, list[CascadeMatch]]:
    blocks = parse_search_replace(raw)
    out = source
    matches: list[CascadeMatch] = []
    for i, blk in enumerate(blocks):
        try:
            out, match = _apply_search_replace_with_match(
                out,
                blk,
                min_context=min_context,
                jaro_winkler_threshold=jaro_winkler_threshold,
            )
        except PatchError as exc:
            raise type(exc)(f"block {i + 1}/{len(blocks)}: {exc}") from None
        matches.append(match)
    return out, matches


def _clean_ledger_cell(value: object) -> str:
    return str(value).replace("|", r"\|").replace("\n", " ").strip()


def append_diff_validation_confidence_ledger(
    event: DiffValidationLedgerEvent,
    *, ledger_path: Path = N10_LEDGER_PATH,
    now: datetime | None = None,
) -> None:
    """Append one WP.3 confidence event to the N10 ledger.

    The ledger write is file-backed shared state; workers coordinate via
    the filesystem, not module-global memory.
    """
    path = Path(ledger_path)
    text = path.read_text(encoding="utf-8")
    section = "## Diff Validation Confidence\n"
    if section not in text:
        raise PatchMalformed("N10 ledger missing Diff Validation Confidence section")

    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = (
        f"| {_clean_ledger_cell(ts)} | {_clean_ledger_cell(event.path)} | "
        f"{_clean_ledger_cell(event.patch_kind)} | {event.layer} | "
        f"{event.score:.3f} | {_clean_ledger_cell(event.disposition)} | "
        f"{_clean_ledger_cell(event.notes)} |\n"
    )

    start = text.index(section) + len(section)
    next_section = text.find("\n## ", start)
    if next_section == -1:
        next_section = len(text)
    insert_at = text.rfind("\n\n", start, next_section)
    if insert_at == -1:
        insert_at = next_section
    path.write_text(text[:insert_at] + "\n" + row + text[insert_at:], encoding="utf-8")


def _repo_relative_path(path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return None


def diff_validation_jaro_winkler_threshold_for_path(path: Path) -> float:
    """Return the Layer-4 threshold for a patch target path.

    DTS / Yocto recipe edits are the HD bring-up strict profile from
    WP.3.3: no 0.9 fuzzy fallback; Layer 4 must clear 0.95. This is a
    pure path-derived value, so uvicorn workers independently derive the
    same policy without module-global coordination.
    """
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if suffix in HD_BRINGUP_STRICT_SUFFIXES or name.endswith(".inc"):
        return HD_BRINGUP_STRICT_JARO_WINKLER_THRESHOLD
    return DEFAULT_JARO_WINKLER_THRESHOLD


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

def apply_to_file(
    path: Path,
    patch_kind: str,
    payload: str,
    *,
    ledger_path: Path = N10_LEDGER_PATH,
) -> None:
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
    matches: list[CascadeMatch] = []
    if patch_kind == "search_replace":
        new, matches = _apply_search_replace_payload_with_matches(
            body,
            payload,
            jaro_winkler_threshold=diff_validation_jaro_winkler_threshold_for_path(p),
        )
    elif patch_kind == "unified_diff":
        new = apply_unified_diff(body, payload)
    else:
        raise PatchMalformed(f"unknown patch_kind {patch_kind!r}")
    tmp = p.with_suffix(p.suffix + ".omnisight-patch-tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX
    ledger_rel = _repo_relative_path(p)
    if matches and (ledger_rel is not None or ledger_path != N10_LEDGER_PATH):
        for match in matches:
            append_diff_validation_confidence_ledger(
                DiffValidationLedgerEvent(
                    path=ledger_rel or str(p),
                    patch_kind=patch_kind,
                    layer=match.layer,
                    score=match.score,
                    notes="WP.3 cascade match confidence",
                ),
                ledger_path=ledger_path,
            )


def apply_edit_to_file(
    path: Path,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    ledger_path: Path = N10_LEDGER_PATH,
) -> EditApplyResult:
    """Apply the legacy Edit-tool replacement through WP.3 cascade.

    Exact one-line edits remain supported for compatibility. Fuzzy
    fallback requires the same 3-line context floor as SEARCH/REPLACE,
    so workers derive a deterministic match policy from file content and
    path without sharing module-global state.
    """
    p = Path(path)
    if not p.is_file():
        raise PatchNotFound(f"apply_edit_to_file: {path} does not exist")
    body = p.read_text(encoding="utf-8")
    count = body.count(old_string)
    if replace_all:
        if count == 0:
            raise PatchNotFound("old_string not found in file")
        new_body = body.replace(old_string, new_string)
        match: CascadeMatch | None = None
        replaced_count = count
    else:
        if count > 1:
            raise PatchAmbiguous(
                f"old_string is not unique (found {count} matches); "
                "pass replace_all=true or extend old_string with more context"
            )
        if count == 1:
            match = _find_exact_match(body, old_string)
            if match is None:
                raise PatchNotFound("old_string not found in file")
        else:
            if _count_content_lines(old_string) < MIN_SEARCH_CONTEXT_LINES:
                raise PatchNotFound("old_string not found in file")
            try:
                match = find_search_replace_match(
                    body,
                    old_string,
                    jaro_winkler_threshold=(
                        diff_validation_jaro_winkler_threshold_for_path(p)
                    ),
                )
            except PatchNotFound:
                raise PatchNotFound("old_string not found in file") from None
        new_body = body[:match.start] + new_string + body[match.end:]
        replaced_count = 1

    tmp = p.with_suffix(p.suffix + ".omnisight-patch-tmp")
    tmp.write_text(new_body, encoding="utf-8")
    tmp.replace(p)

    ledger_rel = _repo_relative_path(p)
    if match is not None and (ledger_rel is not None or ledger_path != N10_LEDGER_PATH):
        append_diff_validation_confidence_ledger(
            DiffValidationLedgerEvent(
                path=ledger_rel or str(p),
                patch_kind="edit",
                layer=match.layer,
                score=match.score,
                notes="WP.3 cascade match confidence",
            ),
            ledger_path=ledger_path,
        )
    return EditApplyResult(replaced_count=replaced_count, match=match)
