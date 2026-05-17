"""OP-1442 — feature-duplication detector for runner pickup."""

from __future__ import annotations

import ast
import logging
import math
import re
import textwrap
from dataclasses import dataclass
from typing import Iterable

from backend.semantic_entropy import lexical_embed

logger = logging.getLogger(__name__)

FILE_JACCARD_THRESHOLD = 0.5
JIRA_SIMILARITY_THRESHOLD = 0.8
HOT_AREAS = frozenset({"core", "db", "security"})
FEATURE_DUP_WARNING = "[feature-dup-warning]"

_OP_KEY_RE = re.compile(r"\bOP-\d+\b")
_PY_SYMBOL_RE = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)


@dataclass(frozen=True)
class PatchSignal:
    """Feature-level signals extracted from one open patch set."""

    ticket_key: str
    change_number: str = ""
    subject: str = ""
    url: str = ""
    files_changed: frozenset[str] = frozenset()
    added_symbols: frozenset[str] = frozenset()
    description: str = ""
    areas: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FeatureDupHit:
    """One possible duplicate-feature overlap."""

    other: PatchSignal
    reasons: tuple[str, ...]
    shared_symbols: tuple[str, ...] = ()
    file_jaccard: float = 0.0
    jira_similarity: float | None = None


def ticket_key_from_subject(subject: str) -> str | None:
    """Extract the first OP key from a Gerrit subject."""

    match = _OP_KEY_RE.search(subject or "")
    return match.group(0) if match else None


def areas_from_labels(labels: Iterable[str]) -> frozenset[str]:
    """Extract ``area:*`` labels for hot-area JIRA similarity gating."""

    return frozenset(
        label.split(":", 1)[1].strip().lower()
        for label in labels
        if isinstance(label, str)
        and label.startswith("area:")
        and label.split(":", 1)[1].strip()
    )


def extract_added_symbols_from_diff(diff_text: str) -> frozenset[str]:
    """Return Python function/class names added by a unified diff.

    The AST pass is the primary extractor when added lines form a parseable
    snippet; the regex fallback handles tiny hunks without surrounding context.
    """

    snippets: list[str] = []
    current: list[str] = []
    regex_symbols: set[str] = set()
    for raw_line in (diff_text or "").splitlines():
        if raw_line.startswith("+++ ") or raw_line.startswith("--- "):
            continue
        if raw_line.startswith("diff --git ") or raw_line.startswith("@@ "):
            if current:
                snippets.append("\n".join(current))
                current = []
            continue
        if not raw_line.startswith("+"):
            continue

        line = raw_line[1:]
        current.append(line)
        match = _PY_SYMBOL_RE.match(line)
        if match:
            regex_symbols.add(match.group(1))
    if current:
        snippets.append("\n".join(current))

    symbols: set[str] = set()
    for snippet in snippets:
        source = textwrap.dedent(snippet).strip()
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(
                node,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
            ):
                symbols.add(node.name)

    return frozenset(symbols | regex_symbols)


def patch_signal_from_gerrit_change(
    change: dict,
    *,
    diff_text: str = "",
    description: str = "",
    areas: Iterable[str] = (),
) -> PatchSignal | None:
    """Build a :class:`PatchSignal` from a Gerrit query JSON object."""

    subject = str(change.get("subject") or "")
    ticket_key = ticket_key_from_subject(subject)
    if ticket_key is None:
        return None

    patchset = change.get("currentPatchSet") or {}
    files: set[str] = set()
    for file_info in patchset.get("files") or []:
        path = str(file_info.get("file") or "")
        if path and path != "/COMMIT_MSG":
            files.add(path)

    change_number = str(change.get("number") or change.get("_number") or "")
    return PatchSignal(
        ticket_key=ticket_key,
        change_number=change_number,
        subject=subject,
        url=str(change.get("url") or ""),
        files_changed=frozenset(files),
        added_symbols=extract_added_symbols_from_diff(diff_text),
        description=description,
        areas=frozenset(area.lower() for area in areas),
    )


def detect_feature_duplicates(
    candidate: PatchSignal,
    open_patches: Iterable[PatchSignal],
    *,
    file_jaccard_threshold: float = FILE_JACCARD_THRESHOLD,
    jira_similarity_threshold: float = JIRA_SIMILARITY_THRESHOLD,
) -> list[FeatureDupHit]:
    """Compare one candidate patch signal against other open patch signals."""

    hits: list[FeatureDupHit] = []
    for other in open_patches:
        if other.ticket_key == candidate.ticket_key:
            continue

        reasons: list[str] = []
        shared_symbols = tuple(sorted(candidate.added_symbols & other.added_symbols))
        if shared_symbols:
            reasons.append("symbol-overlap")

        file_jaccard = _jaccard(candidate.files_changed, other.files_changed)
        if file_jaccard > file_jaccard_threshold:
            reasons.append("changed-file-similarity")

        jira_similarity: float | None = None
        if _is_hot_area_pair(candidate, other):
            jira_similarity = _description_similarity(
                candidate.description,
                other.description,
            )
            if jira_similarity > jira_similarity_threshold:
                reasons.append("jira-description-similarity")

        if reasons:
            hits.append(
                FeatureDupHit(
                    other=other,
                    reasons=tuple(reasons),
                    shared_symbols=shared_symbols,
                    file_jaccard=file_jaccard,
                    jira_similarity=jira_similarity,
                )
            )

    return hits


def format_feature_dup_warning(candidate: PatchSignal, hit: FeatureDupHit) -> str:
    """Render the cross-link warning posted to both JIRA tickets."""

    parts = [
        f"{FEATURE_DUP_WARNING} possible overlap with {hit.other.ticket_key}",
        "",
        f"Current ticket: {candidate.ticket_key}",
        f"Other open PS: {hit.other.ticket_key}",
    ]
    if hit.other.change_number:
        parts.append(f"Gerrit change: #{hit.other.change_number}")
    if hit.other.url:
        parts.append(f"Gerrit URL: {hit.other.url}")
    parts.extend(
        [
            f"Signals: {', '.join(hit.reasons)}",
            f"Changed-file Jaccard: {hit.file_jaccard:.2f}",
        ]
    )
    if hit.shared_symbols:
        parts.append(f"Shared symbols: {', '.join(hit.shared_symbols)}")
    if hit.jira_similarity is not None:
        parts.append(f"JIRA description similarity: {hit.jira_similarity:.2f}")
    parts.append("")
    parts.append("Warning only; operator judges whether this is a duplicate.")
    return "\n".join(parts)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _is_hot_area_pair(candidate: PatchSignal, other: PatchSignal) -> bool:
    return bool((candidate.areas | other.areas) & HOT_AREAS)


def _description_similarity(left: str, right: str) -> float:
    vectors = lexical_embed([left or "", right or ""])
    if len(vectors) != 2:
        return 0.0
    return _cosine(vectors[0], vectors[1])


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    n = min(len(left), len(right))
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for i in range(n):
        dot += left[i] * right[i]
        norm_left += left[i] * left[i]
        norm_right += right[i] * right[i]
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    denom = math.sqrt(norm_left) * math.sqrt(norm_right)
    return max(-1.0, min(1.0, dot / denom))


__all__ = [
    "FEATURE_DUP_WARNING",
    "FeatureDupHit",
    "PatchSignal",
    "areas_from_labels",
    "detect_feature_duplicates",
    "extract_added_symbols_from_diff",
    "format_feature_dup_warning",
    "patch_signal_from_gerrit_change",
    "ticket_key_from_subject",
]
