"""FS.6.1 -- Typesense hosted search adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.search.base import (
    MissingSearchScopeError,
    SearchDocument,
    SearchIndexRequest,
    SearchIndexNotFoundError,
    SearchQuery,
)
from backend.search.typesense import TYPESENSE_API_BASE, TypesenseSearchAdapter

S = TYPESENSE_API_BASE


def _mk_adapter(**kw):
    return TypesenseSearchAdapter(
        token="ts_ABCDEF0123456789",
        **kw,
    )


def _request() -> SearchIndexRequest:
    return SearchIndexRequest(
        index_name="products",
        documents=[SearchDocument("sku-1", {"title": "Camera"})],
    )


class TestTypesenseSearch:

    @respx.mock
    async def test_index_documents_happy(self):
        route = respx.post(f"{S}/collections/products/documents/import").mock(
            return_value=httpx.Response(200, text='{"success":true}'),
        )

        result = await _mk_adapter().index_documents(_request())

        assert result.provider == "typesense"
        assert result.status == "indexed"
        assert result.document_ids == ["sku-1"]
        req = route.calls.last.request
        assert req.headers["x-typesense-api-key"] == "ts_ABCDEF0123456789"
        assert req.url.params["action"] == "upsert"
        body = httpx.Response(200, content=req.read()).json()
        assert body == [{"id": "sku-1", "title": "Camera"}]

    @respx.mock
    async def test_search_happy(self):
        route = respx.get(f"{S}/collections/products/documents/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "document": {"id": "sku-1", "title": "Camera"},
                            "text_match": 100,
                        },
                    ],
                    "found": 1,
                },
            ),
        )

        result = await _mk_adapter(query_by="title").search(
            SearchQuery(
                index_name="products",
                query="camera",
                filters="category:=photo",
                limit=10,
                offset=10,
            ),
        )

        assert result.total == 1
        assert result.hits[0].document_id == "sku-1"
        assert result.hits[0].score == 100
        params = route.calls.last.request.url.params
        assert params["q"] == "camera"
        assert params["query_by"] == "title"
        assert params["filter_by"] == "category:=photo"
        assert params["page"] == "2"

    @respx.mock
    async def test_delete_documents_happy(self):
        route = respx.delete(f"{S}/collections/products/documents").mock(
            return_value=httpx.Response(200, json={"num_deleted": 1}),
        )

        result = await _mk_adapter().delete_documents("products", ["sku-1"])

        assert result.operation_id == "1"
        assert result.status == "deleted"
        assert route.calls.last.request.url.params["filter_by"] == "id:=[sku-1]"

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(f"{S}/collections/products/documents/import").mock(
            return_value=httpx.Response(403, json={"message": "denied"}),
        )
        with pytest.raises(MissingSearchScopeError):
            await _mk_adapter().index_documents(_request())

    @respx.mock
    async def test_404_maps_to_index_not_found(self):
        respx.get(f"{S}/collections/products/documents/search").mock(
            return_value=httpx.Response(404, json={"message": "missing"}),
        )
        with pytest.raises(SearchIndexNotFoundError):
            await _mk_adapter().search(SearchQuery(index_name="products", query="camera"))
