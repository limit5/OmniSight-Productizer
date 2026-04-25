"""R20 Phase 0 — Keyword retrieval over the classified doc corpus.

Strategy choice: BM25-lite (per-term length-normalised counts) over an
in-memory tokenised corpus. No vector DB. Reasoning:

  - Corpus is ~130 docs; in-memory + keyword is < 50 ms total.
  - Adding a vector DB pulls a runtime dependency (Chroma /
    sqlite-vec) and an embedding model — material complexity for
    marginal precision gain at this corpus size.
  - If precision becomes a problem (operators get docs that are
    keyword-matched but semantically wrong), swap in ``rank_bm25``
    or a small embedding model later. The retrieve() signature is
    stable so the upgrade is internal.

Classification gate is THE critical piece. ``retrieve()`` always
filters by the user's role, and ``corpus.visible_audiences_for``
NEVER includes ``internal``. So an admin asking
"what's the security architecture" will get no hits unless an
explicit operator/admin-tagged doc covers it — which is the desired
behaviour: docs that should stay internal stay internal.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .corpus import Doc, load_corpus, visible_audiences_for


@dataclass(frozen=True)
class Hit:
    """One retrieval result. ``snippet`` is a window around the first
    matched query term, suitable for LLM context."""
    doc_path: str
    title: str
    snippet: str
    score: float


# Tokenisation: word characters plus ``'`` and ``-`` (so contractions
# and hyphenated terms stay together). Unicode-aware so CJK queries
# tokenise per character, which BM25-lite handles fine for the kind
# of "how do I 設定 git" mixed queries operators actually type.
_TOKENISE_RE = re.compile(r"[\w'-]+", re.UNICODE)


def _tokenise(text: str) -> list[str]:
    return [t.lower() for t in _TOKENISE_RE.findall(text)]


def _bm25_lite_score(query_terms: list[str], doc_terms: list[str]) -> float:
    """Cheap keyword scoring used in lieu of full BM25.

    Sum of (count / (1 + len/1000)) for each query term. The length
    normalisation prevents long docs from monopolising results purely
    by accident. Skips IDF: the corpus is small and homogeneous, so
    IDF doesn't move the rankings much.
    """
    if not query_terms or not doc_terms:
        return 0.0
    counts = Counter(doc_terms)
    score = 0.0
    length_norm = 1.0 + len(doc_terms) / 1000.0
    for q in query_terms:
        c = counts.get(q, 0)
        if c == 0:
            continue
        score += c / length_norm
    return score


def _make_snippet(body: str, query_terms: list[str], width: int = 240) -> str:
    """Pull a window around the first query-term hit so the LLM sees
    relevant context, not just the doc's opening paragraph.

    Falls back to the head of the body if no terms match (which only
    happens on retrieval-with-zero-score, but defensive anyway).
    """
    if not query_terms:
        return body[:width]
    body_lower = body.lower()
    best_idx = -1
    for q in query_terms:
        idx = body_lower.find(q)
        if idx >= 0 and (best_idx == -1 or idx < best_idx):
            best_idx = idx
    if best_idx < 0:
        return body[:width]
    half = width // 2
    start = max(0, best_idx - half)
    end = min(len(body), start + width)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet = snippet + "…"
    return snippet


# Process-local corpus cache. The corpus is small + ~static between
# deploys, and re-loading from disk per query is wasted I/O. Reset
# via ``reset_corpus_cache()`` from tests.
_corpus_cache: list[Doc] | None = None


def _get_corpus() -> list[Doc]:
    global _corpus_cache
    if _corpus_cache is None:
        _corpus_cache = load_corpus()
    return _corpus_cache


def reset_corpus_cache() -> None:
    """Force re-load on next ``retrieve()``. Test hook only."""
    global _corpus_cache
    _corpus_cache = None


def retrieve(
    query: str, *, role: str = "operator", top_k: int = 4,
) -> list[Hit]:
    """Top-k retrieval, classification-gated by the user's role.

    Returns at most ``top_k`` ``Hit`` objects ordered by descending
    score. Empty list when the query has no tokens or no doc that the
    role can see contains any of the query terms — this is the
    desired "I don't know" signal; the caller should fall back to
    "no relevant docs found" rather than retrieving across the gate.
    """
    if not query:
        return []
    visible = visible_audiences_for(role)
    corpus = _get_corpus()
    query_terms = _tokenise(query)
    if not query_terms:
        return []
    scored: list[tuple[float, Doc]] = []
    for doc in corpus:
        # CLASSIFICATION GATE — this is the primary security control.
        # ``visible`` never contains "internal", so no chat path can
        # read internal docs even if the LLM somehow retrieves them.
        if doc.audience not in visible:
            continue
        doc_terms = _tokenise(doc.body)
        score = _bm25_lite_score(query_terms, doc_terms)
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda x: -x[0])
    out: list[Hit] = []
    for score, doc in scored[:top_k]:
        out.append(Hit(
            doc_path=doc.path,
            title=doc.title,
            snippet=_make_snippet(doc.body, query_terms),
            score=score,
        ))
    return out


def format_hits_for_prompt(hits: list[Hit]) -> str:
    """Render a list of ``Hit`` records into LLM-context text.

    Includes explicit ``[source: <path>]`` citations so the LLM is
    nudged to attribute claims back to the doc — and the operator
    can open the original doc to verify.
    """
    if not hits:
        return ""
    lines = [
        "Retrieved documentation excerpts. Cite as [source: <path>] when "
        "you draw on a specific snippet:",
    ]
    for h in hits:
        lines.append(f"\n--- {h.title} [source: {h.doc_path}] ---")
        lines.append(h.snippet)
    return "\n".join(lines)
