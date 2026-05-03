"""FS.9.2 -- Blog CMS + storage + search end-to-end scenario test.

This capstone mirrors FS.9.1: use provider-mocked adapters, render the
generated app bundle, and assert that the handoff artifacts line up across
the already-landed FS rows.

Module-global state audit: this test writes no module-level mutable state;
all generated files live under ``tmp_path`` and every provider call is scoped
to ``respx.mock`` routes for the current test.

Read-after-write timing audit: no parallel writes are introduced; the scenario
serializes scaffold render, CMS fetch/webhook, storage provisioning, document
indexing, and search query in one async test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import respx

from backend.astro_scaffolder import ScaffoldOptions, pilot_report, render_project
from backend.cms import hmac_sha256_hex
from backend.cms.sanity import SanityCMSSource
from backend.search.base import SearchDocument, SearchIndexRequest, SearchQuery
from backend.search.meilisearch import MEILISEARCH_API_BASE, MeilisearchAdapter
from backend.storage_provisioning.r2 import R2StorageProvisionAdapter


APP_BASE_URL = "https://blog.example.com"
APP_NAME = "blog-cms"
R2 = "https://acct_blog.r2.cloudflarestorage.com"
MEILI = MEILISEARCH_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _blog_bundle_options() -> ScaffoldOptions:
    return ScaffoldOptions(
        project_name=APP_NAME,
        islands="react",
        cms="sanity",
        target="all",
        compliance=True,
        backend_url="http://localhost:8000",
    )


def _assert_blog_bundle(project_dir: Path) -> None:
    package_json = json.loads((project_dir / "package.json").read_text())
    for dep in ("astro", "@astrojs/mdx", "@sanity/client", "@astrojs/react"):
        assert dep in package_json["dependencies"]

    content_config = (project_dir / "src" / "content" / "config.ts").read_text()
    seed_post = (
        project_dir / "src" / "content" / "blog" / "hello-world.mdx"
    ).read_text()
    sanity_source = (project_dir / "src" / "lib" / "cms" / "sanity.ts").read_text()
    webhook_route = (
        project_dir / "src" / "pages" / "api" / "webhooks" / "sanity.ts"
    ).read_text()
    smoke_spec = (project_dir / "e2e" / "smoke.spec.ts").read_text()

    assert "heroImage: z.string().optional()" in content_config
    assert 'tags: ["astro", "mdx", "w8"]' in seed_post
    assert "fetchEntries" in sanity_source
    assert "verifyWebhook" in sanity_source
    assert "sanity-webhook-signature" in webhook_route
    assert "/blog/hello-world" in smoke_spec


async def _fetch_cms_posts_and_webhook() -> list[SearchDocument]:
    route = respx.get(
        re.compile(r"https://proj_blog\..*\.sanity\.io/v[^/]+/data/query/production")
    ).mock(
        return_value=_ok(
            {
                "result": [
                    {
                        "_id": "post-launch",
                        "_type": "post",
                        "_createdAt": "2026-05-01T00:00:00Z",
                        "_updatedAt": "2026-05-02T00:00:00Z",
                        "title": "Launch Notes",
                        "description": "CMS-backed launch post",
                        "slug": {"current": "launch-notes"},
                        "tags": ["cms", "storage", "search"],
                        "heroImage": "hero/launch.png",
                    },
                    {
                        "_id": "post-roadmap",
                        "_type": "post",
                        "title": "Roadmap",
                        "description": "What comes next",
                        "slug": {"current": "roadmap"},
                        "tags": ["cms"],
                    },
                ],
            }
        ),
    )

    source = SanityCMSSource(
        token="sanity_ABCDEF0123456789",
        webhook_secret="whsec_blog_1234567890",
        project_id="proj_blog",
        dataset="production",
    )
    entries = await source.fetch('*[_type == "post"]', content_type="post")

    body = json.dumps(
        {"_id": "post-launch", "_type": "post", "operation": "publish"}
    )
    sig = hmac_sha256_hex("whsec_blog_1234567890", body)
    event = await source.webhook_handler(
        body,
        headers={"sanity-webhook-signature": sig},
    )

    assert route.called
    assert [entry.id for entry in entries] == ["post-launch", "post-roadmap"]
    assert event.action == "publish"
    assert event.entry_id == "post-launch"

    return [
        SearchDocument(
            entry.id,
            {
                "title": entry.fields["title"],
                "description": entry.fields["description"],
                "slug": entry.fields["slug"]["current"],
                "tags": entry.fields.get("tags", []),
                "heroImage": entry.fields.get("heroImage"),
            },
        )
        for entry in entries
    ]


async def _provision_blog_asset_storage() -> str:
    respx.head(f"{R2}/{APP_NAME}-assets").mock(return_value=httpx.Response(404))
    create = respx.put(f"{R2}/{APP_NAME}-assets").mock(
        return_value=httpx.Response(200)
    )

    adapter = R2StorageProvisionAdapter(
        token="r2_secret_ABCDEF0123456789",
        access_key_id="r2_access_blog",
        account_id="acct_blog",
        bucket_name=f"{APP_NAME}-assets",
        public_url="https://assets.blog.example.com",
        cors_allowed_origins=[APP_BASE_URL],
    )
    result = await adapter.provision_bucket()
    upload = await adapter.generate_presigned_url(
        "hero/launch.png",
        method="PUT",
        expires_in=900,
    )

    assert result.provider == "r2"
    assert result.created is True
    assert result.public_url == "https://assets.blog.example.com"
    assert len(create.calls) == 2
    assert "/auto/s3/aws4_request" in create.calls[0].request.headers["authorization"]
    assert b"<AllowedOrigin>https://blog.example.com</AllowedOrigin>" in (
        create.calls[1].request.read()
    )

    parsed = urlparse(upload.url)
    query = parse_qs(parsed.query)
    assert parsed.path == f"/{APP_NAME}-assets/hero/launch.png"
    assert query["X-Amz-Expires"] == ["900"]
    assert upload.method == "PUT"
    return upload.object_key


async def _index_and_search_blog_posts(documents: list[SearchDocument]) -> None:
    index_route = respx.post(f"{MEILI}/indexes/blog-posts/documents").mock(
        return_value=httpx.Response(202, json={"taskUid": 901}),
    )
    search_route = respx.post(f"{MEILI}/indexes/blog-posts/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "id": "post-launch",
                        "title": "Launch Notes",
                        "description": "CMS-backed launch post",
                        "slug": "launch-notes",
                    },
                ],
                "estimatedTotalHits": 1,
            },
        ),
    )

    adapter = MeilisearchAdapter(token="meili_ABCDEF0123456789")
    indexed = await adapter.index_documents(
        SearchIndexRequest(index_name="blog-posts", documents=documents)
    )
    results = await adapter.search(
        SearchQuery(
            index_name="blog-posts",
            query="launch",
            filters="tags = cms",
            limit=5,
        )
    )

    assert indexed.provider == "meilisearch"
    assert indexed.operation_id == "901"
    assert indexed.document_ids == ["post-launch", "post-roadmap"]
    body = httpx.Response(200, content=index_route.calls.last.request.read()).json()
    assert body[0]["heroImage"] == "hero/launch.png"
    assert results.total == 1
    assert results.hits[0].document_id == "post-launch"
    query_body = httpx.Response(
        200,
        content=search_route.calls.last.request.read(),
    ).json()
    assert query_body == {
        "q": "launch",
        "limit": 5,
        "offset": 0,
        "filter": "tags = cms",
    }


@respx.mock
async def test_blog_cms_storage_search_complete_e2e(tmp_path):
    project_dir = tmp_path / APP_NAME
    opts = _blog_bundle_options()

    outcome = render_project(project_dir, opts)
    assert outcome.warnings == []
    _assert_blog_bundle(project_dir)

    documents = await _fetch_cms_posts_and_webhook()
    object_key = await _provision_blog_asset_storage()
    await _index_and_search_blog_posts(documents)

    report = pilot_report(project_dir, opts)
    assert report["options"]["cms"] == "sanity"
    assert report["options"]["target"] == "all"
    assert report["w4_deploy"]["vercel"]["artifact_valid"] is True
    assert report["w4_deploy"]["cloudflare"]["artifact_valid"] is True
    assert report["w5_compliance"]["failed_count"] == 0

    assert object_key == "hero/launch.png"
    assert documents[0].fields["heroImage"] == object_key
