"""R20 Phase 0 — classification-gated retrieval-augmented generation.

Module surface:
  - corpus.load_corpus() — walk docs/, parse frontmatter, return Doc list
  - corpus.visible_audiences_for(role) — role → audience set
  - retrieval.retrieve(query, role, top_k) — BM25-lite over filtered corpus
  - retrieval.format_hits_for_prompt(hits) — render for LLM context
  - retrieval.reset_corpus_cache() — test hook
"""

from .corpus import (
    Doc,
    VALID_AUDIENCES,
    load_corpus,
    visible_audiences_for,
)
from .retrieval import (
    Hit,
    format_hits_for_prompt,
    reset_corpus_cache,
    retrieve,
)

__all__ = [
    "Doc",
    "Hit",
    "VALID_AUDIENCES",
    "format_hits_for_prompt",
    "load_corpus",
    "reset_corpus_cache",
    "retrieve",
    "visible_audiences_for",
]
