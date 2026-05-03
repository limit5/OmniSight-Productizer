"""FS.6.1 -- Meilisearch hosted search adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.search.base import (
    SearchAdapterConflictError,
    SearchAdapterError,
    SearchDocument,
    SearchIndexRequest,
    SearchQuery,
)
from backend.search.meilisearch import MEILISEARCH_API_BASE, MeilisearchAdapter

S = MEILISEARCH_API_BASE


def _mk_adapter(**kw):
    return MeilisearchAdapter(
        token="meili_ABCDEF0123456789",
        **kw,
    )


def _request() -> SearchIndexRequest:
    return SearchIndexRequest(
        index_name="products",
        documents=[SearchDocument("sku-1", {"title": "Camera"})],
    )


class TestMeilisearch:

    @respx.mock
    async def test_index_documents_happy(self):
        route = respx.post(f"{S}/indexes/products/documents").mock(
            return_value=httpx.Response(202, json={"taskUid": 123}),
        )

        result = await _mk_adapter().index_documents(_request())

        assert result.provider == "meilisearch"
        assert result.operation_id == "123"
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer meili_ABCDEF0123456789"
        body = httpx.Response(200, content=req.read()).json()
        assert body == [{"id": "sku-1", "title": "Camera"}]

    @respx.mock
    async def test_search_happy(self):
        route = respx.post(f"{S}/indexes/products/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": [{"id": "sku-1", "title": "Camera"}],
                    "estimatedTotalHits": 1,
                },
            ),
        )

        result = await _mk_adapter().search(
            SearchQuery(
                index_name="products",
                query="camera",
                filters="category = photo",
                limit=5,
                offset=10,
            ),
        )

        assert result.total == 1
        assert result.hits[0].document_id == "sku-1"
        assert result.hits[0].fields == {"title": "Camera"}
        body = httpx.Response(200, content=route.calls.last.request.read()).json()
        assert body == {
            "q": "camera",
            "limit": 5,
            "offset": 10,
            "filter": "category = photo",
        }

    @respx.mock
    async def test_delete_documents_happy(self):
        route = respx.post(f"{S}/indexes/products/documents/delete-batch").mock(
            return_value=httpx.Response(202, json={"taskUid": 456}),
        )

        result = await _mk_adapter().delete_documents("products", ["sku-1"])

        assert result.operation_id == "456"
        body = httpx.Response(200, content=route.calls.last.request.read()).json()
        assert body == ["sku-1"]

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.post(f"{S}/indexes/products/documents").mock(
            return_value=httpx.Response(422, json={"message": "bad document"}),
        )
        with pytest.raises(SearchAdapterConflictError):
            await _mk_adapter().index_documents(_request())

    @respx.mock
    async def test_missing_task_uid_rejected(self):
        respx.post(f"{S}/indexes/products/documents").mock(
            return_value=httpx.Response(202, json={}),
        )
        with pytest.raises(SearchAdapterError, match="taskUid"):
            await _mk_adapter().index_documents(_request())
