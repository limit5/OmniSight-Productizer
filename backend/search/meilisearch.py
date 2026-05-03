"""FS.6.1 -- Meilisearch hosted search adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.search.base import (
    SearchAdapter,
    SearchAdapterError,
    SearchDeleteResult,
    SearchHit,
    SearchIndexRequest,
    SearchIndexResult,
    SearchQuery,
    SearchResult,
)
from backend.search.http import raise_for_search_response

logger = logging.getLogger(__name__)

MEILISEARCH_API_BASE = "http://localhost:7700"


class MeilisearchAdapter(SearchAdapter):
    """Meilisearch API adapter (``provider='meilisearch'``)."""

    provider = "meilisearch"

    def _configure(
        self,
        *,
        api_base: str = MEILISEARCH_API_BASE,
        **_: Any,
    ) -> None:
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def index_documents(
        self,
        request: SearchIndexRequest,
        **kwargs: Any,
    ) -> SearchIndexResult:
        del kwargs
        documents = [
            {"id": doc.document_id, **dict(doc.fields)}
            for doc in request.documents
        ]
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/indexes/{request.index_name}/documents",
                headers=self._headers(),
                json=documents,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        operation_id = str(data.get("taskUid") or data.get("uid") or "")
        if not operation_id:
            raise SearchAdapterError(
                "Meilisearch response missing taskUid",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info(
            "meilisearch.search_index index=%s count=%s operation_id=%s fp=%s",
            request.index_name, len(request.documents), operation_id, self.token_fp(),
        )
        return SearchIndexResult(
            provider=self.provider,
            index_name=request.index_name,
            document_ids=[doc.document_id for doc in request.documents],
            operation_id=operation_id,
            raw=data,
        )

    async def delete_documents(
        self,
        index_name: str,
        document_ids: list[str],
        **kwargs: Any,
    ) -> SearchDeleteResult:
        del kwargs
        ids = [doc_id.strip() for doc_id in document_ids if doc_id.strip()]
        if not ids:
            raise ValueError("at least one document id is required")
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/indexes/{index_name.strip()}/documents/delete-batch",
                headers=self._headers(),
                json=ids,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        operation_id = str(data.get("taskUid") or data.get("uid") or "")
        return SearchDeleteResult(
            provider=self.provider,
            index_name=index_name.strip(),
            document_ids=ids,
            operation_id=operation_id,
            raw=data,
        )

    async def search(self, query: SearchQuery, **kwargs: Any) -> SearchResult:
        del kwargs
        body: dict[str, Any] = {
            "q": query.query,
            "limit": query.limit,
            "offset": query.offset,
        }
        if query.filters:
            body["filter"] = query.filters
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/indexes/{query.index_name}/search",
                headers=self._headers(),
                json=body,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        hits = []
        for hit in data.get("hits", []):
            fields = dict(hit)
            document_id = str(fields.pop("id", ""))
            hits.append(SearchHit(document_id=document_id, fields=fields, raw=hit))
        return SearchResult(
            provider=self.provider,
            index_name=query.index_name,
            query=query.query,
            hits=hits,
            total=int(data.get("estimatedTotalHits") or len(hits)),
            raw=data,
        )


__all__ = ["MEILISEARCH_API_BASE", "MeilisearchAdapter"]
