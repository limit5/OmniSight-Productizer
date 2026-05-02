"""FS.6.2 -- Provider-neutral search indexing pipeline scaffold.

This module sits one layer above the FS.6.1 hosted search adapters: callers
prepare a ``SearchIndexingJob`` from application-owned records, then
``run_indexing_pipeline()`` batches upserts/deletes and delegates every batch
to the selected ``SearchAdapter``.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module only defines immutable constants, dataclasses, and pure helpers;
all per-run pipeline state is allocated inside ``run_indexing_pipeline()``,
so uvicorn workers do not share mutable indexing state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.search.base import (
    SearchAdapter,
    SearchDeleteResult,
    SearchDocument,
    SearchIndexRequest,
    SearchIndexResult,
)

DEFAULT_INDEX_BATCH_SIZE = 100
SearchIndexAction = Literal["index", "delete"]


@dataclass
class SearchIndexingJob:
    """Provider-neutral indexing work unit for one target index."""

    index_name: str
    documents: list[SearchDocument] = field(default_factory=list)
    delete_document_ids: list[str] = field(default_factory=list)
    batch_size: int = DEFAULT_INDEX_BATCH_SIZE

    def __post_init__(self) -> None:
        self.index_name = self.index_name.strip()
        if not self.index_name:
            raise ValueError("index_name is required")
        if self.batch_size < 1:
            raise ValueError("batch_size must be positive")

        self.delete_document_ids = [
            doc_id.strip()
            for doc_id in self.delete_document_ids
            if doc_id.strip()
        ]
        if not self.documents and not self.delete_document_ids:
            raise ValueError("at least one document or delete id is required")

    def to_dict(self) -> dict:
        return {
            "index_name": self.index_name,
            "documents": [doc.to_dict() for doc in self.documents],
            "delete_document_ids": list(self.delete_document_ids),
            "batch_size": self.batch_size,
        }


@dataclass
class SearchIndexingOperation:
    """One provider call performed by the indexing pipeline."""

    action: SearchIndexAction
    provider: str
    index_name: str
    document_ids: list[str]
    operation_id: str = ""
    status: str = "queued"

    @classmethod
    def from_index_result(cls, result: SearchIndexResult) -> "SearchIndexingOperation":
        return cls(
            action="index",
            provider=result.provider,
            index_name=result.index_name,
            document_ids=list(result.document_ids),
            operation_id=result.operation_id,
            status=result.status,
        )

    @classmethod
    def from_delete_result(cls, result: SearchDeleteResult) -> "SearchIndexingOperation":
        return cls(
            action="delete",
            provider=result.provider,
            index_name=result.index_name,
            document_ids=list(result.document_ids),
            operation_id=result.operation_id,
            status=result.status,
        )

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "provider": self.provider,
            "index_name": self.index_name,
            "document_ids": list(self.document_ids),
            "operation_id": self.operation_id,
            "status": self.status,
        }


@dataclass
class SearchIndexingResult:
    """Provider-neutral result for one indexing pipeline run."""

    provider: str
    index_name: str
    indexed_document_ids: list[str] = field(default_factory=list)
    deleted_document_ids: list[str] = field(default_factory=list)
    operations: list[SearchIndexingOperation] = field(default_factory=list)
    status: str = "completed"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "index_name": self.index_name,
            "indexed_document_ids": list(self.indexed_document_ids),
            "deleted_document_ids": list(self.deleted_document_ids),
            "operations": [op.to_dict() for op in self.operations],
            "status": self.status,
        }


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


async def run_indexing_pipeline(
    adapter: SearchAdapter,
    job: SearchIndexingJob,
) -> SearchIndexingResult:
    """Run one indexing job by batching through the supplied adapter."""
    operations: list[SearchIndexingOperation] = []
    indexed_document_ids: list[str] = []
    deleted_document_ids: list[str] = []

    for documents in _chunk(job.documents, job.batch_size):
        result = await adapter.index_documents(
            SearchIndexRequest(index_name=job.index_name, documents=documents),
        )
        indexed_document_ids.extend(result.document_ids)
        operations.append(SearchIndexingOperation.from_index_result(result))

    for document_ids in _chunk(job.delete_document_ids, job.batch_size):
        result = await adapter.delete_documents(job.index_name, document_ids)
        deleted_document_ids.extend(result.document_ids)
        operations.append(SearchIndexingOperation.from_delete_result(result))

    return SearchIndexingResult(
        provider=adapter.provider,
        index_name=job.index_name,
        indexed_document_ids=indexed_document_ids,
        deleted_document_ids=deleted_document_ids,
        operations=operations,
    )


__all__ = [
    "DEFAULT_INDEX_BATCH_SIZE",
    "SearchIndexAction",
    "SearchIndexingJob",
    "SearchIndexingOperation",
    "SearchIndexingResult",
    "run_indexing_pipeline",
]
