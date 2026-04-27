/**
 * BS.8.5 — catalog sources API client + form validators.
 *
 * Locks the contract that the SourcesTab + page wrapper rely on:
 *   • `listCatalogSources()` — GET /catalog/sources (and ?enabled_only).
 *   • `createCatalogSource()` — POST with the right body shape.
 *   • `patchCatalogSource()` — PATCH with the entry id encoded.
 *   • `deleteCatalogSource()` — DELETE.
 *   • `syncCatalogSource()` — POST /catalog/sources/{id}/sync.
 *   • Validation helpers: feed URL / auth secret ref / refresh interval.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  ApiError,
  CATALOG_SOURCE_AUTH_METHODS,
  CATALOG_SOURCE_REFRESH_DEFAULT_S,
  CATALOG_SOURCE_REFRESH_MAX_S,
  CATALOG_SOURCE_REFRESH_MIN_S,
  createCatalogSource,
  deleteCatalogSource,
  listCatalogSources,
  normaliseCatalogSourceFeedUrl,
  patchCatalogSource,
  syncCatalogSource,
  validateCatalogSourceAuthSecretRef,
  validateCatalogSourceFeedUrl,
  validateCatalogSourceRefreshInterval,
  type CatalogSource,
} from "@/lib/api"

const SAMPLE: CatalogSource = {
  id: "sub-deadbeef01234567",
  tenant_id: "t-abc",
  feed_url: "https://feeds.example.com/catalog.json",
  auth_method: "bearer",
  auth_secret_ref: "tenant_token_a",
  refresh_interval_s: 86400,
  last_synced_at: null,
  last_sync_status: null,
  enabled: true,
  created_at: "2026-04-27T10:00:00Z",
  updated_at: "2026-04-27T10:00:00Z",
}

function mockFetchOnce(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
) {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  const res = new Response(text, {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  })
  const spy = vi.fn().mockResolvedValueOnce(res)
  global.fetch = spy as unknown as typeof fetch
  return spy
}

describe("BS.8.5 — catalog sources API client", () => {
  beforeEach(() => {
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe("constants", () => {
    it("CATALOG_SOURCE_REFRESH bounds match the alembic 0051 + Pydantic Field range", () => {
      expect(CATALOG_SOURCE_REFRESH_MIN_S).toBe(60)
      expect(CATALOG_SOURCE_REFRESH_MAX_S).toBe(30 * 86400)
      expect(CATALOG_SOURCE_REFRESH_DEFAULT_S).toBe(86400)
    })

    it("CATALOG_SOURCE_AUTH_METHODS lists exactly the four backend literals", () => {
      expect([...CATALOG_SOURCE_AUTH_METHODS]).toEqual([
        "none",
        "basic",
        "bearer",
        "signed_url",
      ])
    })
  })

  describe("listCatalogSources()", () => {
    it("GETs /api/v1/catalog/sources without query string by default", async () => {
      const payload = { items: [SAMPLE], count: 1 }
      const spy = mockFetchOnce(200, payload)
      const res = await listCatalogSources()
      expect(res).toEqual(payload)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/sources")
      expect((init as RequestInit).method).toBe("GET")
    })

    it("appends ?enabled_only=true when the option is set", async () => {
      const spy = mockFetchOnce(200, { items: [], count: 0 })
      await listCatalogSources({ enabledOnly: true })
      const [url] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/sources?enabled_only=true")
    })

    it("throws ApiError on a 403 (caller lacks admin role)", async () => {
      mockFetchOnce(403, { detail: "admin role required" })
      await expect(listCatalogSources()).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("createCatalogSource()", () => {
    it("POSTs to /api/v1/catalog/sources with the body verbatim", async () => {
      const spy = mockFetchOnce(201, SAMPLE)
      const res = await createCatalogSource({
        feed_url: SAMPLE.feed_url,
        auth_method: "bearer",
        auth_secret_ref: "tenant_token_a",
        refresh_interval_s: 86400,
      })
      expect(res).toEqual(SAMPLE)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/sources")
      expect((init as RequestInit).method).toBe("POST")
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({
        feed_url: SAMPLE.feed_url,
        auth_method: "bearer",
        auth_secret_ref: "tenant_token_a",
        refresh_interval_s: 86400,
      })
    })

    it("throws ApiError on a 409 duplicate-feed-url response", async () => {
      mockFetchOnce(409, { detail: "duplicate feed_url" })
      await expect(
        createCatalogSource({ feed_url: SAMPLE.feed_url }),
      ).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("patchCatalogSource()", () => {
    it("PATCHes /api/v1/catalog/sources/{id} with the body and URL-encodes the id", async () => {
      const spy = mockFetchOnce(200, { ...SAMPLE, enabled: false })
      await patchCatalogSource("sub-x/y", { enabled: false })
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/sources/sub-x%2Fy")
      expect((init as RequestInit).method).toBe("PATCH")
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({
        enabled: false,
      })
    })

    it("throws ApiError on a 404 unknown subscription", async () => {
      mockFetchOnce(404, { detail: "subscription not found" })
      await expect(
        patchCatalogSource("sub-missing", { enabled: false }),
      ).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("deleteCatalogSource()", () => {
    it("DELETEs /api/v1/catalog/sources/{id} and returns the deleted shape", async () => {
      const spy = mockFetchOnce(200, {
        status: "deleted",
        id: SAMPLE.id,
        tenant_id: SAMPLE.tenant_id,
      })
      const res = await deleteCatalogSource(SAMPLE.id)
      expect(res.status).toBe("deleted")
      expect(res.id).toBe(SAMPLE.id)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(`/api/v1/catalog/sources/${SAMPLE.id}`)
      expect((init as RequestInit).method).toBe("DELETE")
    })
  })

  describe("syncCatalogSource()", () => {
    it("POSTs to /api/v1/catalog/sources/{id}/sync with no body", async () => {
      const updated = {
        ...SAMPLE,
        last_sync_status: "pending_manual",
        last_synced_at: null,
      }
      const spy = mockFetchOnce(200, updated)
      const res = await syncCatalogSource(SAMPLE.id)
      expect(res).toEqual(updated)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(`/api/v1/catalog/sources/${SAMPLE.id}/sync`)
      expect((init as RequestInit).method).toBe("POST")
      expect((init as RequestInit).body).toBeUndefined()
    })

    it("URL-encodes the sub_id path segment", async () => {
      const spy = mockFetchOnce(200, SAMPLE)
      await syncCatalogSource("sub-weird/chars")
      const [url] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/sources/sub-weird%2Fchars/sync")
    })

    it("throws ApiError on a 404 unknown subscription", async () => {
      mockFetchOnce(404, { detail: "subscription not found" })
      await expect(syncCatalogSource("sub-missing")).rejects.toBeInstanceOf(
        ApiError,
      )
    })
  })

  describe("validators", () => {
    it("normaliseCatalogSourceFeedUrl trims whitespace", () => {
      expect(normaliseCatalogSourceFeedUrl("  https://x.test  ")).toBe(
        "https://x.test",
      )
    })

    it("validateCatalogSourceFeedUrl accepts http / https URLs", () => {
      expect(validateCatalogSourceFeedUrl("https://x.test/a.json")).toBeNull()
      expect(validateCatalogSourceFeedUrl("http://x.test/a.json")).toBeNull()
    })

    it("validateCatalogSourceFeedUrl rejects empty / non-http schemes", () => {
      expect(validateCatalogSourceFeedUrl("")).not.toBeNull()
      expect(validateCatalogSourceFeedUrl("   ")).not.toBeNull()
      expect(validateCatalogSourceFeedUrl("ftp://x.test/a")).not.toBeNull()
      expect(validateCatalogSourceFeedUrl("just a string")).not.toBeNull()
    })

    it("validateCatalogSourceFeedUrl rejects URLs longer than 2048 chars", () => {
      const tooLong = "https://x.test/" + "a".repeat(2050)
      expect(validateCatalogSourceFeedUrl(tooLong)).not.toBeNull()
    })

    it("validateCatalogSourceAuthSecretRef accepts null / empty / clean strings", () => {
      expect(validateCatalogSourceAuthSecretRef(null)).toBeNull()
      expect(validateCatalogSourceAuthSecretRef(undefined)).toBeNull()
      expect(validateCatalogSourceAuthSecretRef("")).toBeNull()
      expect(validateCatalogSourceAuthSecretRef("tenant_secret_a")).toBeNull()
    })

    it("validateCatalogSourceAuthSecretRef rejects whitespace and overlong values", () => {
      expect(validateCatalogSourceAuthSecretRef("has spaces")).not.toBeNull()
      expect(validateCatalogSourceAuthSecretRef("has\ttabs")).not.toBeNull()
      expect(
        validateCatalogSourceAuthSecretRef("a".repeat(257)),
      ).not.toBeNull()
    })

    it("validateCatalogSourceRefreshInterval accepts the inclusive bounds + a typical value", () => {
      expect(validateCatalogSourceRefreshInterval(60)).toBeNull()
      expect(validateCatalogSourceRefreshInterval(86400)).toBeNull()
      expect(
        validateCatalogSourceRefreshInterval(CATALOG_SOURCE_REFRESH_MAX_S),
      ).toBeNull()
    })

    it("validateCatalogSourceRefreshInterval rejects out-of-range / NaN / non-integer", () => {
      expect(validateCatalogSourceRefreshInterval(59)).not.toBeNull()
      expect(
        validateCatalogSourceRefreshInterval(CATALOG_SOURCE_REFRESH_MAX_S + 1),
      ).not.toBeNull()
      expect(validateCatalogSourceRefreshInterval(NaN)).not.toBeNull()
      expect(validateCatalogSourceRefreshInterval(60.5)).not.toBeNull()
    })
  })
})
