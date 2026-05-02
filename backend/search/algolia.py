"""FS.6.1 -- Algolia hosted search adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.search.base import (
    SearchAdapter,
    SearchAdapterError,
    SearchDeleteResult,
    SearchDocument,
    SearchHit,
    SearchIndexRequest,
    SearchIndexResult,
    SearchQuery,
    SearchResult,
)
from backend.search.http import raise_for_search_response

logger = logging.getLogger(__name__)

ALGOLIA_API_HOST_TEMPLATE = "https://{app_id}.algolia.net"
ALGOLIA_SEARCH_HOST_TEMPLATE = "https://{app_id}-dsn.algolia.net"


class AlgoliaSearchAdapter(SearchAdapter):
    """Algolia API adapter (``provider='algolia'``)."""

    provider = "algolia"

    def _configure(
        self,
        *,
        app_id: str,
        api_base: str | None = None,
        search_base: str | None = None,
        **_: Any,
    ) -> None:
        if not app_id:
            raise ValueError("AlgoliaSearchAdapter requires app_id")
        self._app_id = app_id
        self._api_base = (api_base or ALGOLIA_API_HOST_TEMPLATE.format(app_id=app_id)).rstrip("/")
        self._search_base = (
            search_base or ALGOLIA_SEARCH_HOST_TEMPLATE.format(app_id=app_id)
        ).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Algolia-API-Key": self._token,
            "X-Algolia-Application-Id": self._app_id,
        }

    def _document_body(self, doc: SearchDocument) -> dict[str, Any]:
        return {"objectID": doc.document_id, **dict(doc.fields)}

    async def index_documents(
        self,
        request: SearchIndexRequest,
        **kwargs: Any,
    ) -> SearchIndexResult:
        del kwargs
        payload = {
            "requests": [
                {"action": "addObject", "body": self._document_body(doc)}
                for doc in request.documents
            ],
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/1/indexes/{request.index_name}/batch",
                headers=self._headers(),
                json=payload,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        operation_id = str(data.get("taskID") or data.get("taskId") or "")
        if not operation_id:
            raise SearchAdapterError(
                "Algolia response missing taskID",
                status=resp.status_code,
                provider=self.provider,
            )
        logger.info(
            "algolia.search_index index=%s count=%s operation_id=%s fp=%s",
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
        payload = {
            "requests": [
                {"action": "deleteObject", "body": {"objectID": doc_id}}
                for doc_id in ids
            ],
        }
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._api_base}/1/indexes/{index_name.strip()}/batch",
                headers=self._headers(),
                json=payload,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        operation_id = str(data.get("taskID") or data.get("taskId") or "")
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
            "query": query.query,
            "hitsPerPage": query.limit,
            "page": query.offset // query.limit,
        }
        if query.filters:
            body["filters"] = query.filters
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.post(
                f"{self._search_base}/1/indexes/{query.index_name}/query",
                headers=self._headers(),
                json=body,
            )
        raise_for_search_response(resp, self.provider)
        data = resp.json() if resp.content else {}
        hits = []
        for hit in data.get("hits", []):
            fields = dict(hit)
            document_id = str(fields.pop("objectID", ""))
            score = fields.pop("_rankingInfo", {}).get("nbTypos") if "_rankingInfo" in fields else None
            hits.append(SearchHit(document_id=document_id, fields=fields, score=score, raw=hit))
        return SearchResult(
            provider=self.provider,
            index_name=query.index_name,
            query=query.query,
            hits=hits,
            total=int(data.get("nbHits") or len(hits)),
            raw=data,
        )


__all__ = [
    "ALGOLIA_API_HOST_TEMPLATE",
    "ALGOLIA_SEARCH_HOST_TEMPLATE",
    "AlgoliaSearchAdapter",
]
