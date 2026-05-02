"""FS.6.2 -- Indexing pipeline scaffold tests."""

from __future__ import annotations

from typing import Any

import pytest

from backend.search import (
    DEFAULT_INDEX_BATCH_SIZE,
    SearchAdapter,
    SearchDocument,
    SearchIndexingJob,
    SearchIndexingResult,
    run_indexing_pipeline,
)
from backend.search.base import (
    SearchDeleteResult,
    SearchIndexRequest,
    SearchIndexResult,
    SearchQuery,
    SearchResult,
)


class RecordingSearchAdapter(SearchAdapter):
    provider = "recording"

    def _configure(self, **kwargs: Any) -> None:
        del kwargs
        self.index_requests: list[SearchIndexRequest] = []
        self.delete_requests: list[tuple[str, list[str]]] = []

    async def index_documents(
        self,
        request: SearchIndexRequest,
        **kwargs: Any,
    ) -> SearchIndexResult:
        del kwargs
        self.index_requests.append(request)
        return SearchIndexResult(
            provider=self.provider,
            index_name=request.index_name,
            document_ids=[doc.document_id for doc in request.documents],
            operation_id=f"idx-{len(self.index_requests)}",
            status="indexed",
        )

    async def delete_documents(
        self,
        index_name: str,
        document_ids: list[str],
        **kwargs: Any,
    ) -> SearchDeleteResult:
        del kwargs
        self.delete_requests.append((index_name, document_ids))
        return SearchDeleteResult(
            provider=self.provider,
            index_name=index_name,
            document_ids=list(document_ids),
            operation_id=f"del-{len(self.delete_requests)}",
            status="deleted",
        )

    async def search(self, query: SearchQuery, **kwargs: Any) -> SearchResult:
        del query, kwargs
        return SearchResult(provider=self.provider, index_name="unused", query="")


def _documents(count: int) -> list[SearchDocument]:
    return [
        SearchDocument(document_id=f"doc-{i}", fields={"title": f"Camera {i}"})
        for i in range(count)
    ]


class TestSearchIndexingJob:

    def test_to_dict(self):
        job = SearchIndexingJob(
            index_name=" products ",
            documents=[SearchDocument("sku-1", {"title": "Camera"})],
            delete_document_ids=[" old-1 ", " "],
            batch_size=25,
        )

        assert job.to_dict() == {
            "index_name": "products",
            "documents": [
                {
                    "document_id": "sku-1",
                    "fields": {"title": "Camera"},
                },
            ],
            "delete_document_ids": ["old-1"],
            "batch_size": 25,
        }

    def test_default_batch_size(self):
        job = SearchIndexingJob(
            index_name="products",
            documents=[SearchDocument("sku-1", {})],
        )
        assert job.batch_size == DEFAULT_INDEX_BATCH_SIZE

    def test_requires_work(self):
        with pytest.raises(ValueError, match="at least one document"):
            SearchIndexingJob(index_name="products")

    def test_rejects_invalid_batch_size(self):
        with pytest.raises(ValueError, match="batch_size"):
            SearchIndexingJob(
                index_name="products",
                documents=[SearchDocument("sku-1", {})],
                batch_size=0,
            )


class TestRunIndexingPipeline:

    @pytest.mark.asyncio
    async def test_batches_index_and_delete_operations(self):
        adapter = RecordingSearchAdapter(token="recording-token")
        job = SearchIndexingJob(
            index_name="products",
            documents=_documents(3),
            delete_document_ids=["old-1", "old-2", "old-3"],
            batch_size=2,
        )

        result = await run_indexing_pipeline(adapter, job)

        assert result.provider == "recording"
        assert result.indexed_document_ids == ["doc-0", "doc-1", "doc-2"]
        assert result.deleted_document_ids == ["old-1", "old-2", "old-3"]
        assert [len(req.documents) for req in adapter.index_requests] == [2, 1]
        assert [ids for _, ids in adapter.delete_requests] == [
            ["old-1", "old-2"],
            ["old-3"],
        ]
        assert [op.action for op in result.operations] == [
            "index",
            "index",
            "delete",
            "delete",
        ]
        assert [op.operation_id for op in result.operations] == [
            "idx-1",
            "idx-2",
            "del-1",
            "del-2",
        ]

    @pytest.mark.asyncio
    async def test_result_to_dict_omits_adapter_token(self):
        adapter = RecordingSearchAdapter(token="recording-secret-token")
        job = SearchIndexingJob(
            index_name="products",
            documents=[SearchDocument("sku-1", {"title": "Camera"})],
        )

        result = await run_indexing_pipeline(adapter, job)
        data = result.to_dict()

        assert isinstance(result, SearchIndexingResult)
        assert data == {
            "provider": "recording",
            "index_name": "products",
            "indexed_document_ids": ["sku-1"],
            "deleted_document_ids": [],
            "operations": [
                {
                    "action": "index",
                    "provider": "recording",
                    "index_name": "products",
                    "document_ids": ["sku-1"],
                    "operation_id": "idx-1",
                    "status": "indexed",
                },
            ],
            "status": "completed",
        }
        assert "recording-secret-token" not in repr(data)
