"""FS.6.1 -- Tests for the shared hosted search adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.search import (
    SearchAdapter,
    SearchAdapterError,
    SearchAdapterRateLimitError,
    SearchDocument,
    SearchIndexRequest,
    SearchIndexResult,
    SearchQuery,
    SearchResult,
    get_adapter,
    list_providers,
)


class TestSearchProviderFactory:

    def test_list_providers_enumerates_three(self):
        assert list_providers() == ["algolia", "typesense", "meilisearch"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("algolia", "AlgoliaSearchAdapter"),
            ("typesense", "TypesenseSearchAdapter"),
            ("meilisearch", "MeilisearchAdapter"),
            ("meili", "MeilisearchAdapter"),
            ("MEILISEARCH", "MeilisearchAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, SearchAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("elastic")
        assert "Unknown search provider" in str(excinfo.value)
        for provider in list_providers():
            assert provider in str(excinfo.value)

    def test_every_adapter_has_unique_provider_classvar(self):
        seen = set()
        for provider in list_providers():
            cls = get_adapter(provider)
            assert cls.provider
            assert cls.provider not in seen
            seen.add(cls.provider)


class TestEncryptedTokenFactory:

    def test_from_encrypted_token_decrypts_via_secret_store(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-fs-6-1")
        secret_store._reset_for_tests()

        plaintext = "algolia_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        adapter_cls = get_adapter("algolia")
        adapter = adapter_cls.from_encrypted_token(ciphertext, app_id="demo")
        assert isinstance(adapter, SearchAdapter)
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        adapter = get_adapter("meili").from_plaintext_token("meili_1234567890")
        assert adapter.provider == "meilisearch"


class TestSearchDocument:

    def test_to_dict(self):
        doc = SearchDocument(" doc-1 ", {"title": "Camera"})
        assert doc.document_id == "doc-1"
        assert doc.to_dict() == {
            "document_id": "doc-1",
            "fields": {"title": "Camera"},
        }

    def test_requires_document_id(self):
        with pytest.raises(ValueError, match="document_id"):
            SearchDocument("  ", {"title": "Camera"})


class TestSearchIndexRequest:

    def test_to_dict(self):
        req = SearchIndexRequest(
            index_name=" products ",
            documents=[SearchDocument("sku-1", {"title": "Camera"})],
        )
        assert req.index_name == "products"
        assert req.to_dict() == {
            "index_name": "products",
            "documents": [
                {
                    "document_id": "sku-1",
                    "fields": {"title": "Camera"},
                },
            ],
        }

    def test_requires_document(self):
        with pytest.raises(ValueError, match="document"):
            SearchIndexRequest(index_name="products", documents=[])


class TestSearchQuery:

    def test_to_dict_normalizes_filters(self):
        query = SearchQuery(
            index_name="products",
            query=" camera ",
            filters=" category:=photo ",
            limit=10,
            offset=20,
        )

        assert query.to_dict() == {
            "index_name": "products",
            "query": "camera",
            "filters": "category:=photo",
            "limit": 10,
            "offset": 20,
        }

    def test_rejects_negative_offset(self):
        with pytest.raises(ValueError, match="offset"):
            SearchQuery(index_name="products", query="camera", offset=-1)


class TestSearchResult:

    def test_result_to_dict_omits_raw_payload(self):
        result = SearchResult(
            provider="algolia",
            index_name="products",
            query="camera",
            hits=[],
            total=0,
            raw={"token": "provider-secret"},
        )

        data = result.to_dict()

        assert data == {
            "provider": "algolia",
            "index_name": "products",
            "query": "camera",
            "hits": [],
            "total": 0,
        }
        assert "provider-secret" not in repr(data)

    def test_index_result_to_dict(self):
        result = SearchIndexResult(
            provider="typesense",
            index_name="products",
            document_ids=["sku-1"],
            operation_id="op-1",
            raw={"token": "provider-secret"},
        )

        assert result.to_dict() == {
            "provider": "typesense",
            "index_name": "products",
            "document_ids": ["sku-1"],
            "operation_id": "op-1",
            "status": "queued",
        }


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["algolia", "typesense", "meilisearch"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        assert callable(getattr(cls, "index_documents"))
        assert callable(getattr(cls, "delete_documents"))
        assert callable(getattr(cls, "search"))

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            SearchAdapter(token="t")  # type: ignore[abstract]

    def test_rate_limit_error_is_search_error_subclass(self):
        assert issubclass(SearchAdapterRateLimitError, SearchAdapterError)
