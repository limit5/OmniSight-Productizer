"""FS.6.1 -- Hosted search provider adapters package."""

from __future__ import annotations

from backend.search.base import (
    InvalidSearchTokenError,
    MissingSearchScopeError,
    SearchAdapter,
    SearchAdapterConflictError,
    SearchAdapterError,
    SearchAdapterRateLimitError,
    SearchDeleteResult,
    SearchDocument,
    SearchHit,
    SearchIndexNotFoundError,
    SearchIndexRequest,
    SearchIndexResult,
    SearchQuery,
    SearchResult,
)
from backend.search.pipeline import (
    DEFAULT_INDEX_BATCH_SIZE,
    SearchIndexAction,
    SearchIndexingJob,
    SearchIndexingOperation,
    SearchIndexingResult,
    run_indexing_pipeline,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped hosted search adapter."""
    return ["algolia", "typesense", "meilisearch"]


def get_adapter(provider: str) -> type[SearchAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "algolia":
        from backend.search.algolia import AlgoliaSearchAdapter
        return AlgoliaSearchAdapter
    if key == "typesense":
        from backend.search.typesense import TypesenseSearchAdapter
        return TypesenseSearchAdapter
    if key in ("meilisearch", "meili"):
        from backend.search.meilisearch import MeilisearchAdapter
        return MeilisearchAdapter
    raise ValueError(
        f"Unknown search provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "InvalidSearchTokenError",
    "MissingSearchScopeError",
    "SearchAdapter",
    "SearchAdapterConflictError",
    "SearchAdapterError",
    "SearchAdapterRateLimitError",
    "SearchDeleteResult",
    "SearchDocument",
    "SearchHit",
    "SearchIndexNotFoundError",
    "SearchIndexRequest",
    "SearchIndexResult",
    "SearchIndexAction",
    "SearchIndexingJob",
    "SearchIndexingOperation",
    "SearchIndexingResult",
    "SearchQuery",
    "SearchResult",
    "DEFAULT_INDEX_BATCH_SIZE",
    "get_adapter",
    "list_providers",
    "run_indexing_pipeline",
]
