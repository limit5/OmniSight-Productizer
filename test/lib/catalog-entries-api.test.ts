/**
 * BS.8.6 — catalog entry admin CRUD API client + form validators.
 *
 * Locks the contract that the CustomEntryForm + page wrapper rely on:
 *   • `listCatalogEntries()` — GET /catalog/entries (with filter query).
 *   • `createCatalogEntry()` — POST with body verbatim.
 *   • `patchCatalogEntry()` — PATCH with the entry id encoded.
 *   • `deleteCatalogEntry()` — DELETE.
 *   • Validators: id / install URL / sha256 / size_bytes / vendor /
 *     display_name / version.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  ApiError,
  CATALOG_ENTRY_FAMILIES,
  CATALOG_ENTRY_INSTALL_METHODS,
  CATALOG_ENTRY_SIZE_BYTES_MAX,
  CATALOG_ENTRY_WRITABLE_SOURCES,
  createCatalogEntry,
  deleteCatalogEntry,
  listCatalogEntries,
  normaliseCatalogEntryId,
  patchCatalogEntry,
  validateCatalogEntryDisplayName,
  validateCatalogEntryId,
  validateCatalogEntryInstallUrl,
  validateCatalogEntrySha256,
  validateCatalogEntrySizeBytes,
  validateCatalogEntryVendor,
  validateCatalogEntryVersion,
  type CatalogEntryDetail,
} from "@/lib/api"

const SAMPLE: CatalogEntryDetail = {
  id: "vendor-sdk-x",
  source: "operator",
  schema_version: 1,
  tenant_id: "t-abc",
  vendor: "Acme",
  family: "embedded",
  display_name: "Acme SDK",
  version: "1.0.0",
  install_method: "shell_script",
  install_url: "https://downloads.example.com/sdk.tar.gz",
  sha256: "a".repeat(64),
  size_bytes: 1024 * 1024,
  depends_on: [],
  metadata: { license: "MIT" },
  hidden: false,
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

describe("BS.8.6 — catalog entries API client", () => {
  beforeEach(() => {
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe("constants", () => {
    it("CATALOG_ENTRY_FAMILIES exposes all 7 backend literals", () => {
      expect([...CATALOG_ENTRY_FAMILIES]).toEqual([
        "mobile",
        "embedded",
        "web",
        "software",
        "rtos",
        "cross-toolchain",
        "custom",
      ])
    })

    it("CATALOG_ENTRY_INSTALL_METHODS exposes the 4 backend literals", () => {
      expect([...CATALOG_ENTRY_INSTALL_METHODS]).toEqual([
        "noop",
        "docker_pull",
        "shell_script",
        "vendor_installer",
      ])
    })

    it("CATALOG_ENTRY_WRITABLE_SOURCES exposes operator + override only", () => {
      expect([...CATALOG_ENTRY_WRITABLE_SOURCES]).toEqual([
        "operator",
        "override",
      ])
    })

    it("CATALOG_ENTRY_SIZE_BYTES_MAX matches backend SIZE_BYTES_MAX (1 TiB)", () => {
      expect(CATALOG_ENTRY_SIZE_BYTES_MAX).toBe(2 ** 40)
    })
  })

  describe("listCatalogEntries()", () => {
    it("GETs /api/v1/catalog/entries without query string by default", async () => {
      const payload = { items: [SAMPLE], count: 1, total: 1, limit: 100, offset: 0 }
      const spy = mockFetchOnce(200, payload)
      const res = await listCatalogEntries()
      expect(res).toEqual(payload)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/entries")
      expect((init as RequestInit).method).toBe("GET")
    })

    it("appends filter query string when options are passed", async () => {
      const spy = mockFetchOnce(200, {
        items: [],
        count: 0,
        total: 0,
        limit: 500,
        offset: 0,
      })
      await listCatalogEntries({
        family: "embedded",
        source: "operator",
        sort: "display_name",
        order: "asc",
        limit: 500,
        offset: 0,
      })
      const [url] = spy.mock.calls[0]!
      const u = url as string
      expect(u.startsWith("/api/v1/catalog/entries?")).toBe(true)
      expect(u).toContain("family=embedded")
      expect(u).toContain("source=operator")
      expect(u).toContain("sort=display_name")
      expect(u).toContain("order=asc")
      expect(u).toContain("limit=500")
      expect(u).toContain("offset=0")
    })

    it("appends include_hidden=true when requested", async () => {
      const spy = mockFetchOnce(200, {
        items: [],
        count: 0,
        total: 0,
        limit: 100,
        offset: 0,
      })
      await listCatalogEntries({ includeHidden: true })
      const [url] = spy.mock.calls[0]!
      expect(url).toContain("include_hidden=true")
    })

    it("throws ApiError on a 403 (caller lacks operator role)", async () => {
      mockFetchOnce(403, { detail: "operator role required" })
      await expect(listCatalogEntries()).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("createCatalogEntry()", () => {
    it("POSTs to /api/v1/catalog/entries with the body verbatim", async () => {
      const spy = mockFetchOnce(201, SAMPLE)
      const res = await createCatalogEntry({
        id: SAMPLE.id,
        source: "operator",
        vendor: SAMPLE.vendor!,
        family: "embedded",
        display_name: SAMPLE.display_name!,
        version: SAMPLE.version!,
        install_method: "shell_script",
        install_url: SAMPLE.install_url,
        sha256: SAMPLE.sha256,
        size_bytes: SAMPLE.size_bytes,
        depends_on: [],
        metadata: { license: "MIT" },
      })
      expect(res).toEqual(SAMPLE)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/entries")
      expect((init as RequestInit).method).toBe("POST")
      expect(JSON.parse((init as RequestInit).body as string).id).toBe(SAMPLE.id)
    })

    it("throws ApiError on a 409 duplicate-id response", async () => {
      mockFetchOnce(409, { detail: "duplicate" })
      await expect(
        createCatalogEntry({ id: SAMPLE.id }),
      ).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 422 validation failure", async () => {
      mockFetchOnce(422, { detail: "bad sha256" })
      await expect(
        createCatalogEntry({ id: SAMPLE.id }),
      ).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("patchCatalogEntry()", () => {
    it("PATCHes /api/v1/catalog/entries/{id} with the body and URL-encodes the id", async () => {
      const spy = mockFetchOnce(200, { ...SAMPLE, version: "2.0.0" })
      await patchCatalogEntry("entry/with-slash", { version: "2.0.0" })
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/catalog/entries/entry%2Fwith-slash")
      expect((init as RequestInit).method).toBe("PATCH")
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({
        version: "2.0.0",
      })
    })

    it("throws ApiError on a 404 unknown entry", async () => {
      mockFetchOnce(404, { detail: "not found" })
      await expect(
        patchCatalogEntry("missing", { vendor: "x" }),
      ).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("deleteCatalogEntry()", () => {
    it("DELETEs /api/v1/catalog/entries/{id} and returns the deleted shape", async () => {
      const spy = mockFetchOnce(200, {
        status: "deleted",
        id: SAMPLE.id,
        tenant_id: SAMPLE.tenant_id,
      })
      const res = await deleteCatalogEntry(SAMPLE.id)
      expect(res.status).toBe("deleted")
      expect(res.id).toBe(SAMPLE.id)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(`/api/v1/catalog/entries/${SAMPLE.id}`)
      expect((init as RequestInit).method).toBe("DELETE")
    })
  })

  describe("validators", () => {
    it("normaliseCatalogEntryId trims whitespace", () => {
      expect(normaliseCatalogEntryId("  vendor-sdk  ")).toBe("vendor-sdk")
    })

    it("validateCatalogEntryId accepts valid kebab-case", () => {
      expect(validateCatalogEntryId("vendor-sdk")).toBeNull()
      expect(validateCatalogEntryId("v1")).toBeNull()
      expect(validateCatalogEntryId("a-b-c-1-2-3")).toBeNull()
    })

    it("validateCatalogEntryId rejects empty / overlong / bad chars", () => {
      expect(validateCatalogEntryId("")).not.toBeNull()
      expect(validateCatalogEntryId("a".repeat(65))).not.toBeNull()
      expect(validateCatalogEntryId("UPPER")).not.toBeNull()
      expect(validateCatalogEntryId("with_underscore")).not.toBeNull()
      expect(validateCatalogEntryId("-leading")).not.toBeNull()
      expect(validateCatalogEntryId("trailing-")).not.toBeNull()
      expect(validateCatalogEntryId("double--hyphen")).not.toBeNull()
    })

    it("validateCatalogEntryInstallUrl accepts http(s) URLs and empty", () => {
      expect(validateCatalogEntryInstallUrl(null)).toBeNull()
      expect(validateCatalogEntryInstallUrl(undefined)).toBeNull()
      expect(validateCatalogEntryInstallUrl("")).toBeNull()
      expect(validateCatalogEntryInstallUrl("https://x.test/y")).toBeNull()
      expect(validateCatalogEntryInstallUrl("http://x.test/y")).toBeNull()
    })

    it("validateCatalogEntryInstallUrl rejects bad schemes / overlong", () => {
      expect(validateCatalogEntryInstallUrl("ftp://x.test/y")).not.toBeNull()
      expect(validateCatalogEntryInstallUrl("not a url")).not.toBeNull()
      expect(validateCatalogEntryInstallUrl(`https://x/${"a".repeat(2050)}`)).not.toBeNull()
    })

    it("validateCatalogEntrySha256 accepts 64 lowercase hex chars and empty", () => {
      expect(validateCatalogEntrySha256(null)).toBeNull()
      expect(validateCatalogEntrySha256("")).toBeNull()
      expect(validateCatalogEntrySha256("a".repeat(64))).toBeNull()
      expect(validateCatalogEntrySha256("0".repeat(64))).toBeNull()
    })

    it("validateCatalogEntrySha256 rejects wrong length / uppercase / non-hex", () => {
      expect(validateCatalogEntrySha256("a".repeat(63))).not.toBeNull()
      expect(validateCatalogEntrySha256("a".repeat(65))).not.toBeNull()
      expect(validateCatalogEntrySha256("A".repeat(64))).not.toBeNull()
      expect(validateCatalogEntrySha256("z".repeat(64))).not.toBeNull()
    })

    it("validateCatalogEntrySizeBytes accepts non-negative integers up to 1 TiB", () => {
      expect(validateCatalogEntrySizeBytes(null)).toBeNull()
      expect(validateCatalogEntrySizeBytes(undefined)).toBeNull()
      expect(validateCatalogEntrySizeBytes(0)).toBeNull()
      expect(validateCatalogEntrySizeBytes(1024)).toBeNull()
      expect(validateCatalogEntrySizeBytes(CATALOG_ENTRY_SIZE_BYTES_MAX)).toBeNull()
    })

    it("validateCatalogEntrySizeBytes rejects negatives / non-integers / above 1 TiB / NaN", () => {
      expect(validateCatalogEntrySizeBytes(-1)).not.toBeNull()
      expect(validateCatalogEntrySizeBytes(1.5)).not.toBeNull()
      expect(validateCatalogEntrySizeBytes(CATALOG_ENTRY_SIZE_BYTES_MAX + 1)).not.toBeNull()
      expect(validateCatalogEntrySizeBytes(NaN)).not.toBeNull()
      expect(validateCatalogEntrySizeBytes(Infinity)).not.toBeNull()
    })

    it("validateCatalogEntryVendor / DisplayName / Version respect their max-length caps", () => {
      expect(validateCatalogEntryVendor("Acme")).toBeNull()
      expect(validateCatalogEntryVendor(null)).toBeNull()
      expect(validateCatalogEntryVendor("a".repeat(129))).not.toBeNull()
      expect(validateCatalogEntryDisplayName("Acme SDK")).toBeNull()
      expect(validateCatalogEntryDisplayName("a".repeat(257))).not.toBeNull()
      expect(validateCatalogEntryVersion("1.0.0")).toBeNull()
      expect(validateCatalogEntryVersion("a".repeat(65))).not.toBeNull()
    })
  })
})
