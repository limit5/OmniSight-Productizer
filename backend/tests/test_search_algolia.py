"""FS.6.1 -- Algolia hosted search adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.search.algolia import (
    ALGOLIA_API_HOST_TEMPLATE,
    ALGOLIA_SEARCH_HOST_TEMPLATE,
    AlgoliaSearchAdapter,
)
from backend.search.base import (
    InvalidSearchTokenError,
    SearchAdapterError,
    SearchAdapterRateLimitError,
    SearchDocument,
    SearchIndexRequest,
    SearchQuery,
)

APP_ID = "demoapp"
A = ALGOLIA_API_HOST_TEMPLATE.format(app_id=APP_ID)
S = ALGOLIA_SEARCH_HOST_TEMPLATE.format(app_id=APP_ID)


def _mk_adapter(**kw):
    return AlgoliaSearchAdapter(
        token="algolia_ABCDEF0123456789",
        app_id=APP_ID,
        **kw,
    )


def _request() -> SearchIndexRequest:
    return SearchIndexRequest(
        index_name="products",
        documents=[
            SearchDocument(
                document_id="sku-1",
                fields={"title": "Camera", "category": "photo"},
            ),
        ],
    )


class TestAlgoliaSearch:

    @respx.mock
    async def test_index_documents_happy(self):
        route = respx.post(f"{A}/1/indexes/products/batch").mock(
            return_value=httpx.Response(200, json={"taskID": 123}),
        )

        result = await _mk_adapter().index_documents(_request())

        assert result.provider == "algolia"
        assert result.operation_id == "123"
        assert result.document_ids == ["sku-1"]
        req = route.calls.last.request
        assert req.headers["x-algolia-api-key"] == "algolia_ABCDEF0123456789"
        assert req.headers["x-algolia-application-id"] == APP_ID
        body = httpx.Response(200, content=req.read()).json()
        assert body == {
            "requests": [
                {
                    "action": "addObject",
                    "body": {
                        "objectID": "sku-1",
                        "title": "Camera",
                        "category": "photo",
                    },
                },
            ],
        }

    @respx.mock
    async def test_search_happy(self):
        route = respx.post(f"{S}/1/indexes/products/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": [{"objectID": "sku-1", "title": "Camera"}],
                    "nbHits": 1,
                },
            ),
        )

        result = await _mk_adapter().search(
            SearchQuery(
                index_name="products",
                query="camera",
                filters="category:photo",
                limit=10,
                offset=20,
            ),
        )

        assert result.provider == "algolia"
        assert result.total == 1
        assert result.hits[0].document_id == "sku-1"
        assert result.hits[0].fields == {"title": "Camera"}
        body = httpx.Response(200, content=route.calls.last.request.read()).json()
        assert body == {
            "query": "camera",
            "hitsPerPage": 10,
            "page": 2,
            "filters": "category:photo",
        }

    @respx.mock
    async def test_delete_documents_happy(self):
        route = respx.post(f"{A}/1/indexes/products/batch").mock(
            return_value=httpx.Response(200, json={"taskID": 456}),
        )

        result = await _mk_adapter().delete_documents("products", ["sku-1"])

        assert result.operation_id == "456"
        body = httpx.Response(200, content=route.calls.last.request.read()).json()
        assert body == {
            "requests": [
                {"action": "deleteObject", "body": {"objectID": "sku-1"}},
            ],
        }

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.post(f"{A}/1/indexes/products/batch").mock(
            return_value=httpx.Response(401, json={"message": "bad token"}),
        )
        with pytest.raises(InvalidSearchTokenError):
            await _mk_adapter().index_documents(_request())

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.post(f"{A}/1/indexes/products/batch").mock(
            return_value=httpx.Response(
                429,
                json={"message": "slow"},
                headers={"Retry-After": "7"},
            ),
        )
        with pytest.raises(SearchAdapterRateLimitError) as excinfo:
            await _mk_adapter().index_documents(_request())
        assert excinfo.value.retry_after == 7

    @respx.mock
    async def test_missing_task_id_rejected(self):
        respx.post(f"{A}/1/indexes/products/batch").mock(
            return_value=httpx.Response(200, json={}),
        )
        with pytest.raises(SearchAdapterError, match="taskID"):
            await _mk_adapter().index_documents(_request())
