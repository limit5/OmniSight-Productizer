/**
 * BS.7.1 — Unit tests for the installer API client in `lib/api.ts`.
 *
 * Locks the contract that the catalog-card "Install" button click flows
 * to `POST /installer/jobs` with the right body shape (so it lands in
 * the existing R20-A PEP gateway HOLD path on the backend without any
 * extra client-side work).
 *
 * Specifically:
 *   • `createInstallJob(entryId)` POSTs JSON `{ entry_id, idempotency_key,
 *     metadata: {} }` to `/api/v1/installer/jobs` with the standard
 *     CSRF / X-Tenant-Id headers the rest of `request()` emits.
 *   • An auto-generated idempotency_key matches the backend's
 *     `^[A-Za-z0-9_\-]{16,64}$` pattern (so the request can't 422 on
 *     the field-level regex before reaching PEP).
 *   • Caller-supplied options (`idempotencyKey`, `bytesTotal`, `metadata`)
 *     are forwarded verbatim, so an idempotent retry from a stale tab
 *     deduplicates server-side instead of producing a second HOLD.
 *   • A 200 response (idempotency_key collision — backend's
 *     "ON CONFLICT (idempotency_key) DO NOTHING" branch) is returned
 *     unchanged, so the caller can show the already-running job.
 *   • A 403 PEP-deny throws `ApiError`, so the global
 *     `<ApiErrorToastCenter />` lights up without the caller having to
 *     pattern-match on response bodies.
 *   • `generateInstallIdempotencyKey()` is deterministic enough to
 *     satisfy the backend pattern even when `crypto.randomUUID` is
 *     missing (jsdom in some Node versions exposes a partial Crypto
 *     binding without `randomUUID`).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  ApiError,
  bulkUninstallEntries,
  cancelInstallJob,
  createInstallJob,
  generateInstallIdempotencyKey,
  getInstallJob,
  INSTALLED_ENTRY_IDLE_THRESHOLD_MS,
  isCleanupCandidate,
  listEntryDependents,
  listInstalledEntries,
  retryInstallJob,
  type BulkUninstallResponse,
  type InstallJob,
  type InstalledEntryRow,
  type ListEntryDependentsResponse,
  type ListInstalledEntriesResponse,
} from "@/lib/api"

const ENDPOINT = "/api/v1/installer/jobs"

const SAMPLE_JOB: InstallJob = {
  id: "ij-0123456789ab",
  tenant_id: "t-abc",
  entry_id: "neural-blur-sdk",
  state: "queued",
  idempotency_key: "sample-key-1234567890abcdef",
  sidecar_id: null,
  protocol_version: 1,
  bytes_done: 0,
  bytes_total: null,
  eta_seconds: null,
  log_tail: "",
  result_json: null,
  error_reason: null,
  pep_decision_id: "de-abcdef012345",
  requested_by: "u-operator",
  queued_at: "2026-04-27T10:00:00Z",
  claimed_at: null,
  started_at: null,
  completed_at: null,
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

describe("BS.7.1 — installer API client", () => {
  beforeEach(() => {
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe("generateInstallIdempotencyKey()", () => {
    it("returns a string that matches the backend's idempotency_key regex", () => {
      const key = generateInstallIdempotencyKey()
      // Backend pattern (alembic 0051 + InstallJobCreate Field):
      // ^[A-Za-z0-9_\-]{16,64}$
      expect(key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
    })

    it("falls back to a 32-char hex token when crypto.randomUUID is unavailable", () => {
      const original = (
        globalThis as { crypto?: Crypto & { randomUUID?: () => string } }
      ).crypto
      try {
        // Strip randomUUID to force the fallback branch.
        Object.defineProperty(globalThis, "crypto", {
          configurable: true,
          value: {
            getRandomValues: original?.getRandomValues?.bind(original),
          } as Crypto,
        })
        const key = generateInstallIdempotencyKey()
        expect(key).toMatch(/^[a-f0-9]{16,32}$/)
        expect(key.length).toBeGreaterThanOrEqual(16)
      } finally {
        Object.defineProperty(globalThis, "crypto", {
          configurable: true,
          value: original,
        })
      }
    })

    it("produces distinct keys across consecutive calls", () => {
      const a = generateInstallIdempotencyKey()
      const b = generateInstallIdempotencyKey()
      expect(a).not.toBe(b)
    })
  })

  describe("createInstallJob()", () => {
    it("POSTs to /api/v1/installer/jobs with entry_id + auto idempotency_key + empty metadata", async () => {
      const spy = mockFetchOnce(201, SAMPLE_JOB)
      const result = await createInstallJob("neural-blur-sdk")
      expect(result).toEqual(SAMPLE_JOB)
      expect(spy).toHaveBeenCalledTimes(1)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(ENDPOINT)
      expect(init.method).toBe("POST")
      const body = JSON.parse(init.body as string)
      expect(body.entry_id).toBe("neural-blur-sdk")
      expect(body.metadata).toEqual({})
      expect(typeof body.idempotency_key).toBe("string")
      // Same regex the backend enforces so a 422 can't fire on the
      // field shape before classify() runs and decides HOLD.
      expect(body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
      // bytes_total is omitted entirely (not null) when caller does
      // not pass it, so the backend uses its default (`None`).
      expect(body).not.toHaveProperty("bytes_total")
    })

    it("forwards caller-supplied idempotencyKey + bytesTotal + metadata verbatim", async () => {
      const spy = mockFetchOnce(201, SAMPLE_JOB)
      await createInstallJob("neural-blur-sdk", {
        idempotencyKey: "operator-retry-key-0001",
        bytesTotal: 1_073_741_824,
        metadata: { vendor_channel: "stable", initiated_from: "platforms-tab" },
      })
      const [, init] = spy.mock.calls[0]!
      const body = JSON.parse(init.body as string)
      expect(body.idempotency_key).toBe("operator-retry-key-0001")
      expect(body.bytes_total).toBe(1_073_741_824)
      expect(body.metadata).toEqual({
        vendor_channel: "stable",
        initiated_from: "platforms-tab",
      })
    })

    it("emits Content-Type: application/json on the request", async () => {
      const spy = mockFetchOnce(201, SAMPLE_JOB)
      await createInstallJob("neural-blur-sdk")
      const [, init] = spy.mock.calls[0]!
      const headers = init.headers as Record<string, string>
      expect(headers["Content-Type"]).toBe("application/json")
    })

    it("returns the existing job row unchanged on a 200 idempotency-collision response", async () => {
      // Backend: ``ON CONFLICT (idempotency_key) DO NOTHING`` → returns
      // existing row at 200 (no second PEP HOLD). Frontend must surface
      // that row as-is so the UI can show "already installing".
      const existing: InstallJob = {
        ...SAMPLE_JOB,
        state: "running",
        sidecar_id: "omnisight-installer-1",
      }
      mockFetchOnce(200, existing)
      const result = await createInstallJob("neural-blur-sdk", {
        idempotencyKey: "operator-retry-key-0001",
      })
      expect(result.state).toBe("running")
      expect(result.sidecar_id).toBe("omnisight-installer-1")
    })

    it("throws ApiError on a 403 pep_denied response (PEP rejected the install)", async () => {
      const denial = {
        error: "pep_denied",
        reason: "pep_tier_unlisted",
        job_id: SAMPLE_JOB.id,
        job: { ...SAMPLE_JOB, state: "cancelled", error_reason: "pep_tier_unlisted" },
      }
      mockFetchOnce(403, denial)
      await expect(createInstallJob("neural-blur-sdk")).rejects.toBeInstanceOf(
        ApiError,
      )
    })

    it("throws ApiError on a 404 catalog-entry-not-found response", async () => {
      mockFetchOnce(404, { detail: "catalog entry 'ghost' not found" })
      await expect(createInstallJob("ghost")).rejects.toBeInstanceOf(ApiError)
    })

    it("does not send bytes_total when option is omitted", async () => {
      const spy = mockFetchOnce(201, SAMPLE_JOB)
      await createInstallJob("neural-blur-sdk", {
        metadata: { foo: "bar" },
      })
      const [, init] = spy.mock.calls[0]!
      const body = JSON.parse(init.body as string)
      expect(body).not.toHaveProperty("bytes_total")
      expect(body.metadata).toEqual({ foo: "bar" })
    })
  })

  // ─── BS.7.6 ──────────────────────────────────────────────────────────
  describe("retryInstallJob()", () => {
    const SOURCE_ID = "ij-failed01234"

    it("POSTs to /api/v1/installer/jobs/{id}/retry with auto idempotency_key", async () => {
      const spy = mockFetchOnce(201, { ...SAMPLE_JOB, id: "ij-retry00001" })
      const result = await retryInstallJob(SOURCE_ID)
      expect(result.id).toBe("ij-retry00001")
      expect(spy).toHaveBeenCalledTimes(1)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(`/api/v1/installer/jobs/${SOURCE_ID}/retry`)
      expect(init.method).toBe("POST")
      const body = JSON.parse(init.body as string)
      // Same regex the backend enforces — auto-generated key must match.
      expect(body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
      // No other fields beyond idempotency_key — the backend pulls
      // entry_id from the source row, not the body.
      expect(Object.keys(body)).toEqual(["idempotency_key"])
    })

    it("forwards a caller-supplied idempotencyKey verbatim", async () => {
      const spy = mockFetchOnce(201, SAMPLE_JOB)
      await retryInstallJob(SOURCE_ID, {
        idempotencyKey: "operator-double-click-guard-key",
      })
      const [, init] = spy.mock.calls[0]!
      const body = JSON.parse(init.body as string)
      expect(body.idempotency_key).toBe("operator-double-click-guard-key")
    })

    it("URL-encodes the job id segment so a malformed id cannot break the path", async () => {
      const spy = mockFetchOnce(201, SAMPLE_JOB)
      await retryInstallJob("ij with/slash")
      const [url] = spy.mock.calls[0]!
      // encodeURIComponent encodes spaces as %20 and / as %2F.
      expect(url).toBe("/api/v1/installer/jobs/ij%20with%2Fslash/retry")
    })

    it("throws ApiError on a 409 source-still-active response", async () => {
      mockFetchOnce(409, { detail: "source job is 'running'; cancel first before retry" })
      await expect(retryInstallJob(SOURCE_ID)).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 404 source-row-not-found response", async () => {
      mockFetchOnce(404, { detail: "install job not found" })
      await expect(retryInstallJob(SOURCE_ID)).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 403 PEP-deny response (the retry was held + denied)", async () => {
      mockFetchOnce(403, {
        error: "pep_denied",
        reason: "pep_tier_unlisted",
        job_id: SOURCE_ID,
      })
      await expect(retryInstallJob(SOURCE_ID)).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("getInstallJob()", () => {
    const JOB_ID = "ij-failed01234"

    it("GETs /api/v1/installer/jobs/{id} and returns the row verbatim", async () => {
      const failed: InstallJob = {
        ...SAMPLE_JOB,
        state: "failed",
        log_tail: "ERROR: layer download failed at byte 0x4f8\nlayer 3/8\n",
        error_reason: "sidecar:docker_pull:layer_unreachable",
      }
      const spy = mockFetchOnce(200, failed)
      const result = await getInstallJob(JOB_ID)
      expect(result).toEqual(failed)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(`/api/v1/installer/jobs/${JOB_ID}`)
      // Default method is GET (no init.method override).
      expect(init.method).toBe("GET")
    })

    it("URL-encodes the job id so reserved chars pass through safely", async () => {
      const spy = mockFetchOnce(200, SAMPLE_JOB)
      await getInstallJob("ij with/slash")
      const [url] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/installer/jobs/ij%20with%2Fslash")
    })

    it("throws ApiError on a 404 (row gone / wrong tenant)", async () => {
      mockFetchOnce(404, { detail: "install job not found" })
      await expect(getInstallJob(JOB_ID)).rejects.toBeInstanceOf(ApiError)
    })
  })

  // ─── BS.7.7 ──────────────────────────────────────────────────────────
  describe("cancelInstallJob()", () => {
    const JOB_ID = "ij-running00abc"
    const CANCELLED_ROW: InstallJob = {
      ...SAMPLE_JOB,
      id: JOB_ID,
      state: "cancelled",
      error_reason: "operator_cancelled",
      completed_at: "2026-04-27T10:00:30Z",
    }

    it("POSTs to /api/v1/installer/jobs/{id}/cancel with no body when reason is omitted", async () => {
      const spy = mockFetchOnce(200, CANCELLED_ROW)
      const result = await cancelInstallJob(JOB_ID)
      expect(result).toEqual(CANCELLED_ROW)
      expect(spy).toHaveBeenCalledTimes(1)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(`/api/v1/installer/jobs/${JOB_ID}/cancel`)
      expect((init as RequestInit).method).toBe("POST")
      // Zero-byte POST when no reason — backend defaults to
      // ``operator_cancelled`` so the typical click flow is the
      // smallest possible request.
      expect((init as RequestInit).body).toBeUndefined()
    })

    it("forwards a non-empty reason as JSON body", async () => {
      const spy = mockFetchOnce(200, CANCELLED_ROW)
      await cancelInstallJob(JOB_ID, { reason: "wrong vendor channel" })
      const [, init] = spy.mock.calls[0]!
      const body = JSON.parse((init as RequestInit).body as string)
      expect(body).toEqual({ reason: "wrong vendor channel" })
    })

    it("omits the body when reason is null / undefined / empty string", async () => {
      const spyA = mockFetchOnce(200, CANCELLED_ROW)
      await cancelInstallJob(JOB_ID, { reason: null })
      expect((spyA.mock.calls[0]![1] as RequestInit).body).toBeUndefined()

      const spyB = mockFetchOnce(200, CANCELLED_ROW)
      await cancelInstallJob(JOB_ID, { reason: undefined })
      expect((spyB.mock.calls[0]![1] as RequestInit).body).toBeUndefined()

      const spyC = mockFetchOnce(200, CANCELLED_ROW)
      await cancelInstallJob(JOB_ID, { reason: "" })
      expect((spyC.mock.calls[0]![1] as RequestInit).body).toBeUndefined()
    })

    it("URL-encodes the job id segment", async () => {
      const spy = mockFetchOnce(200, CANCELLED_ROW)
      await cancelInstallJob("ij with/slash")
      const [url] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/installer/jobs/ij%20with%2Fslash/cancel")
    })

    it("throws ApiError on a 404 (row not found / wrong tenant)", async () => {
      mockFetchOnce(404, { detail: "install job not found" })
      await expect(cancelInstallJob(JOB_ID)).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 409 (row is already terminal — completed/failed/cancelled)", async () => {
      mockFetchOnce(409, {
        detail: "job is in terminal state 'completed'; cannot cancel",
      })
      await expect(cancelInstallJob(JOB_ID)).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 403 (caller lacks operator role)", async () => {
      mockFetchOnce(403, { detail: "operator role required" })
      await expect(cancelInstallJob(JOB_ID)).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 422 (malformed job id)", async () => {
      mockFetchOnce(422, { detail: "invalid job id" })
      await expect(cancelInstallJob("not!valid")).rejects.toBeInstanceOf(
        ApiError,
      )
    })
  })

  // ─── BS.8.2 — installed list + bulk uninstall + cleanup predicate ─────
  describe("BS.8.2 — listInstalledEntries()", () => {
    it("GETs /api/v1/installer/installed and returns the response payload", async () => {
      const sample: InstalledEntryRow = {
        entry_id: "neural-blur-sdk",
        display_name: "Neural Blur SDK",
        vendor: "Acme",
        family: "mobile",
        version: "1.2.3",
        description: null,
        disk_usage_bytes: 1024,
        used_by_workspace_count: 0,
        last_used_at: null,
        installed_at: "2026-04-25T08:00:00Z",
        update_available: false,
        available_version: null,
        source: "operator",
      }
      const payload: ListInstalledEntriesResponse = {
        items: [sample],
        count: 1,
      }
      const spy = mockFetchOnce(200, payload)
      const res = await listInstalledEntries()
      expect(res).toEqual(payload)
      expect(spy).toHaveBeenCalledTimes(1)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/installer/installed")
      expect((init as RequestInit).method).toBe("GET")
    })

    it("throws ApiError on a 403 (caller lacks operator role)", async () => {
      mockFetchOnce(403, { detail: "operator role required" })
      await expect(listInstalledEntries()).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("BS.8.2 — bulkUninstallEntries()", () => {
    const SAMPLE: BulkUninstallResponse = {
      items: [
        {
          entry_id: "neural-blur-sdk",
          job_id: "ij-aaaaaaaaaaaa",
          action: "approved",
          state: "completed",
          reason: null,
          pep_decision_id: "de-deadbeefcafe",
        },
      ],
      approved_count: 1,
      denied_count: 0,
      pep_decision_id: "de-deadbeefcafe",
    }

    it("POSTs to /api/v1/installer/uninstall with the entry_ids array", async () => {
      const spy = mockFetchOnce(200, SAMPLE)
      const res = await bulkUninstallEntries(["neural-blur-sdk"])
      expect(res).toEqual(SAMPLE)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe("/api/v1/installer/uninstall")
      expect((init as RequestInit).method).toBe("POST")
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({
        entry_ids: ["neural-blur-sdk"],
      })
    })

    it("forwards a non-empty reason as JSON body", async () => {
      const spy = mockFetchOnce(200, SAMPLE)
      await bulkUninstallEntries(["a"], { reason: "long idle" })
      const body = JSON.parse(
        (spy.mock.calls[0]![1] as RequestInit).body as string,
      )
      expect(body).toEqual({ entry_ids: ["a"], reason: "long idle" })
    })

    it("omits the reason field when null / undefined / empty string", async () => {
      const spyA = mockFetchOnce(200, SAMPLE)
      await bulkUninstallEntries(["a"], { reason: null })
      expect(
        JSON.parse((spyA.mock.calls[0]![1] as RequestInit).body as string).reason,
      ).toBeUndefined()

      const spyB = mockFetchOnce(200, SAMPLE)
      await bulkUninstallEntries(["a"], { reason: undefined })
      expect(
        JSON.parse((spyB.mock.calls[0]![1] as RequestInit).body as string).reason,
      ).toBeUndefined()

      const spyC = mockFetchOnce(200, SAMPLE)
      await bulkUninstallEntries(["a"], { reason: "" })
      expect(
        JSON.parse((spyC.mock.calls[0]![1] as RequestInit).body as string).reason,
      ).toBeUndefined()
    })

    it("snapshots the entry_ids array (caller-side mutation does not bleed into the body)", async () => {
      const spy = mockFetchOnce(200, SAMPLE)
      const ids = ["a", "b"]
      await bulkUninstallEntries(ids)
      ids.push("c")  // operator side-effect
      const body = JSON.parse((spy.mock.calls[0]![1] as RequestInit).body as string)
      expect(body.entry_ids).toEqual(["a", "b"])
    })

    it("throws ApiError on a 403 PEP-deny response (whole batch rejected)", async () => {
      mockFetchOnce(403, {
        detail: {
          error: "pep_denied",
          reason: "pep_tier_unlisted",
          count: 1,
          items: [{ entry_id: "a", action: "denied", state: "cancelled" }],
        },
      })
      await expect(bulkUninstallEntries(["a"])).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 422 (empty list / oversized payload / malformed entry_id)", async () => {
      mockFetchOnce(422, { detail: "entry_ids must not be empty" })
      await expect(bulkUninstallEntries([])).rejects.toBeInstanceOf(ApiError)
    })
  })

  describe("BS.8.2 — isCleanupCandidate()", () => {
    const NOW = new Date("2026-04-27T12:00:00Z")
    const idle = (deltaMs: number): string =>
      new Date(NOW.getTime() - deltaMs).toISOString()

    it("treats lastUsedAt > 30d ago as a candidate (zero workspace dependants)", () => {
      const e = { lastUsedAt: idle(31 * 24 * 60 * 60 * 1000), usedByWorkspaceCount: 0 }
      expect(isCleanupCandidate(e, NOW)).toBe(true)
    })

    it("excludes entries with at least one workspace dependant regardless of idle time", () => {
      const e = { lastUsedAt: idle(60 * 24 * 60 * 60 * 1000), usedByWorkspaceCount: 1 }
      expect(isCleanupCandidate(e, NOW)).toBe(false)
    })

    it("excludes entries used within the last 30 days", () => {
      const e = { lastUsedAt: idle(20 * 24 * 60 * 60 * 1000), usedByWorkspaceCount: 0 }
      expect(isCleanupCandidate(e, NOW)).toBe(false)
    })

    it("falls back to installedAt when lastUsedAt is null", () => {
      const e = {
        lastUsedAt: null,
        installedAt: idle(31 * 24 * 60 * 60 * 1000),
        usedByWorkspaceCount: 0,
      }
      expect(isCleanupCandidate(e, NOW)).toBe(true)
    })

    it("treats both timestamps missing as a candidate (operator can decide)", () => {
      const e = { lastUsedAt: null, installedAt: null, usedByWorkspaceCount: 0 }
      expect(isCleanupCandidate(e, NOW)).toBe(true)
    })

    it("treats malformed timestamps as a candidate (defence-in-depth)", () => {
      const e = { lastUsedAt: "not-a-date", usedByWorkspaceCount: 0 }
      expect(isCleanupCandidate(e, NOW)).toBe(true)
    })

    it("INSTALLED_ENTRY_IDLE_THRESHOLD_MS equals 30 days in ms", () => {
      expect(INSTALLED_ENTRY_IDLE_THRESHOLD_MS).toBe(30 * 24 * 60 * 60 * 1000)
    })

    it("locks the boundary: exactly 30 days ago is a candidate (>=, not >)", () => {
      const e = {
        lastUsedAt: idle(INSTALLED_ENTRY_IDLE_THRESHOLD_MS),
        usedByWorkspaceCount: 0,
      }
      expect(isCleanupCandidate(e, NOW)).toBe(true)
    })
  })

  // ─── BS.8.4 — per-entry dependents lookup ─────────────────────────
  describe("BS.8.4 — listEntryDependents()", () => {
    const SAMPLE_DEP: InstalledEntryRow = {
      entry_id: "android-sdk-platform-34",
      display_name: "Android SDK Platform 34",
      vendor: "Google",
      family: "mobile",
      version: "34",
      description: null,
      disk_usage_bytes: 256 * 1024 * 1024,
      used_by_workspace_count: 0,
      last_used_at: null,
      installed_at: "2026-04-25T08:00:00Z",
      update_available: false,
      available_version: null,
      source: "shipped",
    }

    it("GETs /api/v1/installer/installed/{entry_id}/dependents and returns the response payload", async () => {
      const payload: ListEntryDependentsResponse = {
        entry_id: "android-sdk-base",
        items: [SAMPLE_DEP],
        count: 1,
      }
      const spy = mockFetchOnce(200, payload)
      const res = await listEntryDependents("android-sdk-base")
      expect(res).toEqual(payload)
      expect(spy).toHaveBeenCalledTimes(1)
      const [url, init] = spy.mock.calls[0]!
      expect(url).toBe(
        "/api/v1/installer/installed/android-sdk-base/dependents",
      )
      expect((init as RequestInit).method).toBe("GET")
    })

    it("URL-encodes the entry_id path segment", async () => {
      // Path encoding sanity: even though the backend regex disallows
      // most special characters, the helper must still encode them so
      // a malformed id doesn't break the fetch URL parser before the
      // backend can return 422.
      const spy = mockFetchOnce(200, {
        entry_id: "weird/id",
        items: [],
        count: 0,
      })
      await listEntryDependents("weird/id")
      const [url] = spy.mock.calls[0]!
      expect(url).toBe(
        "/api/v1/installer/installed/weird%2Fid/dependents",
      )
    })

    it("returns an empty list when the entry has no dependents", async () => {
      const payload: ListEntryDependentsResponse = {
        entry_id: "stand-alone-tool",
        items: [],
        count: 0,
      }
      mockFetchOnce(200, payload)
      const res = await listEntryDependents("stand-alone-tool")
      expect(res.count).toBe(0)
      expect(res.items).toEqual([])
    })

    it("throws ApiError on a 422 (malformed entry_id)", async () => {
      mockFetchOnce(422, { detail: "invalid entry_id" })
      await expect(
        listEntryDependents("BAD ID"),
      ).rejects.toBeInstanceOf(ApiError)
    })

    it("throws ApiError on a 403 (caller lacks operator role)", async () => {
      mockFetchOnce(403, { detail: "operator role required" })
      await expect(
        listEntryDependents("any-entry"),
      ).rejects.toBeInstanceOf(ApiError)
    })
  })
})
