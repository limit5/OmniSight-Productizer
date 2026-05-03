"""FS.6.1 -- Unified hosted search adapter interface.

Algolia / Typesense / Meilisearch expose provider APIs for indexing and
querying documents before later FS.6 rows add the indexing pipeline and
faceted UI. This module mirrors ``backend.email_delivery.base``: callers
construct a provider adapter from an encrypted or plaintext token, call
``index_documents()`` / ``delete_documents()`` / ``search()``, then keep
the returned provider operation ids in the caller-owned workflow.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable classes/functions only. No module-level
cache, singleton, or mutable registry is read or written; provider
factory functions in ``backend.search`` materialize fresh lists per
call, so uvicorn workers do not share runtime state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from backend import secret_store
from backend.deploy.base import token_fingerprint


class SearchAdapterError(Exception):
    """Base for all hosted search adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidSearchTokenError(SearchAdapterError):
    """401 -- API token invalid / revoked."""


class MissingSearchScopeError(SearchAdapterError):
    """403 -- API token lacks required permission."""


class SearchIndexNotFoundError(SearchAdapterError):
    """404 -- requested index does not exist."""


class SearchAdapterConflictError(SearchAdapterError):
    """409 / 422 -- provider rejected document or index shape."""


class SearchAdapterRateLimitError(SearchAdapterError):
    """429 -- provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class SearchDocument:
    """Provider-neutral document to index."""

    document_id: str
    fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.document_id = self.document_id.strip()
        if not self.document_id:
            raise ValueError("document_id is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "fields": dict(self.fields),
        }


@dataclass
class SearchIndexRequest:
    """Provider-neutral request to index one batch of documents."""

    index_name: str
    documents: list[SearchDocument]

    def __post_init__(self) -> None:
        self.index_name = self.index_name.strip()
        if not self.index_name:
            raise ValueError("index_name is required")
        if not self.documents:
            raise ValueError("at least one document is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_name": self.index_name,
            "documents": [doc.to_dict() for doc in self.documents],
        }


@dataclass
class SearchIndexResult:
    """Outcome of ``adapter.index_documents(...)``."""

    provider: str
    index_name: str
    document_ids: list[str]
    operation_id: str = ""
    status: str = "queued"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "index_name": self.index_name,
            "document_ids": list(self.document_ids),
            "operation_id": self.operation_id,
            "status": self.status,
        }


@dataclass
class SearchDeleteResult:
    """Outcome of ``adapter.delete_documents(...)``."""

    provider: str
    index_name: str
    document_ids: list[str]
    operation_id: str = ""
    status: str = "queued"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "index_name": self.index_name,
            "document_ids": list(self.document_ids),
            "operation_id": self.operation_id,
            "status": self.status,
        }


@dataclass
class SearchQuery:
    """Provider-neutral search query."""

    index_name: str
    query: str
    filters: str | None = None
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        self.index_name = self.index_name.strip()
        if not self.index_name:
            raise ValueError("index_name is required")
        self.query = self.query.strip()
        if self.limit < 1:
            raise ValueError("limit must be positive")
        if self.offset < 0:
            raise ValueError("offset must be non-negative")
        if self.filters is not None:
            self.filters = self.filters.strip() or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_name": self.index_name,
            "query": self.query,
            "filters": self.filters,
            "limit": self.limit,
            "offset": self.offset,
        }


@dataclass
class SearchHit:
    """Provider-neutral search hit."""

    document_id: str
    fields: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "fields": dict(self.fields),
            "score": self.score,
        }


@dataclass
class SearchResult:
    """Outcome of ``adapter.search(...)``."""

    provider: str
    index_name: str
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    total: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "index_name": self.index_name,
            "query": self.query,
            "hits": [hit.to_dict() for hit in self.hits],
            "total": self.total,
        }


class SearchAdapter(ABC):
    """Abstract base for every hosted search provider adapter."""

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        self._token = token
        self._timeout = timeout
        self._configure(**kwargs)

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        **kwargs: Any,
    ) -> "SearchAdapter":
        """Decrypt via ``backend.secret_store`` and build an adapter."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        **kwargs: Any,
    ) -> "SearchAdapter":
        """Build an adapter from a plaintext token for tests / CLI paths."""
        return cls(token=token, **kwargs)

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    @abstractmethod
    async def index_documents(
        self,
        request: SearchIndexRequest,
        **kwargs: Any,
    ) -> SearchIndexResult:
        """Index one document batch via the provider API."""

    @abstractmethod
    async def delete_documents(
        self,
        index_name: str,
        document_ids: list[str],
        **kwargs: Any,
    ) -> SearchDeleteResult:
        """Delete one document batch via the provider API."""

    @abstractmethod
    async def search(self, query: SearchQuery, **kwargs: Any) -> SearchResult:
        """Run one query via the provider API."""


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
    "SearchQuery",
    "SearchResult",
]
