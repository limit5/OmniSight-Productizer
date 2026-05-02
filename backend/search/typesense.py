"""FS.6.1 -- Typesense hosted search adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.search.base import (
    SearchAdapter,
    SearchDeleteResult,
    SearchHit,
    SearchIndexRequest,
    SearchIndexResult,
    SearchQuery,
    SearchResult,
)
from backend.search.http import raise_for_search_response

logger = logging.getLogger(__name__)

TYPESENSE_API_BASE = "http://localhost:8108"


class TypesenseSearchAdapter(SearchAdapter):
    """Typesense API adapter (``provider='typesense'``)."""

    provider = "typesense"

    def _configure(
        self,
        *,
        api_base: str = TYPESENSE_API_BASE,
        query_by: str = "title,description,body",
        **_: Any,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._query_by = query_by

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-TYPESENSE-API-KEY": self._token,
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
                f"{self._api_base}/collections/{request.index_name}/documents/import",
                headers=self._headers(),
                params={"action": "upsert"},
                json=documents,
            )
        raise_for_search_response(resp, self.provider)
        data: dict[str, Any] = {"raw": resp.text}
        logger.info(
            "typesense.search_index index=%s count=%s fp=%s",
            request.index_name, len(request.documents), self.token_fp(),
        )
        return SearchIndexResult(
            provider=self.provider,
            index_name=request.index_name,
            document_ids=[doc.document_id for doc in request.documents],
            status="indexed",
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
            resp = await c.delete(
                f"{self._api_base}/collections/{index_name.strip()}/documents",
                headers=self._headers(),
                params={"filter_by": f"id:=[{','.join(ids)}]"},
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        return SearchDeleteResult(
            provider=self.provider,
            index_name=index_name.strip(),
            document_ids=ids,
            operation_id=str(data.get("num_deleted") or ""),
            status="deleted",
            raw=data,
        )

    async def search(self, query: SearchQuery, **kwargs: Any) -> SearchResult:
        del kwargs
        params: dict[str, Any] = {
            "q": query.query or "*",
            "query_by": self._query_by,
            "per_page": query.limit,
            "page": (query.offset // query.limit) + 1,
        }
        if query.filters:
            params["filter_by"] = query.filters
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(
                f"{self._api_base}/collections/{query.index_name}/documents/search",
                headers=self._headers(),
                params=params,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        hits = []
        for hit in data.get("hits", []):
            document = dict(hit.get("document") or {})
            document_id = str(document.pop("id", ""))
            score = hit.get("text_match")
            hits.append(SearchHit(document_id=document_id, fields=document, score=score, raw=hit))
        return SearchResult(
            provider=self.provider,
            index_name=query.index_name,
            query=query.query,
            hits=hits,
            total=int(data.get("found") or len(hits)),
            raw=data,
        )


__all__ = ["TYPESENSE_API_BASE", "TypesenseSearchAdapter"]
