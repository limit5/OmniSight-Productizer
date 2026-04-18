/**
 * Unit tests for the B13 Part C (#339) global API error handler in
 * `lib/api.ts`.
 *
 * We test the *observable contract* of `request()` via:
 *   - a stubbed `global.fetch` that returns the shaped responses we care
 *     about (401 / 403 / 500 / 502 / 503 bootstrap / 503 maintenance /
 *     offline / timeout);
 *   - the exported `onApiError` bus, which receives every terminal
 *     failure with its classified `kind`, `status`, `traceId`, etc.;
 *   - an assignable `window.location` stub so we can assert on the 401
 *     → `/login?next=<current>` and 503-bootstrap → `/setup-required`
 *     redirects without actually navigating the JSDOM window.
 *
 * The fetch retry loop backs off with `setTimeout(..., seconds)`; the
 * suite uses Vitest fake timers and drives time forward manually so the
 * tests complete in tens of ms instead of tens of seconds.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiError, getHealth, onApiError } from "@/lib/api"

type LocationStub = {
  href: string
  pathname: string
  search: string
  origin: string
  assign: ReturnType<typeof vi.fn>
}

function installLocation(pathname: string, search: string = ""): LocationStub {
  const stub: LocationStub = {
    href: `http://localhost${pathname}${search}`,
    pathname,
    search,
    origin: "http://localhost",
    assign: vi.fn(),
  }
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: stub,
  })
  return stub
}

function mockFetchOnce(status: number, body: unknown, headers: Record<string, string> = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  const res = new Response(text, {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  })
  const spy = vi.fn().mockResolvedValueOnce(res)
  global.fetch = spy as unknown as typeof fetch
  return spy
}

function mockFetchAlways(status: number, body: unknown, headers: Record<string, string> = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body)
  const spy = vi.fn().mockImplementation(() =>
    Promise.resolve(
      new Response(text, {
        status,
        headers: { "Content-Type": "application/json", ...headers },
      }),
    ),
  )
  global.fetch = spy as unknown as typeof fetch
  return spy
}

describe("B13 Part C — global API error handler", () => {
  const originalLocation = window.location
  let unsubscribers: Array<() => void> = []

  beforeEach(() => {
    unsubscribers = []
    installLocation("/dashboard", "?ok=1")
  })

  afterEach(() => {
    for (const u of unsubscribers) u()
    unsubscribers = []
    vi.useRealTimers()
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    })
  })

  describe("onApiError bus", () => {
    it("emits a typed ApiError with classified kind for 500", async () => {
      vi.useFakeTimers()
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchAlways(500, { detail: "boom", trace_id: "req_xyz_123" })
      // `.catch` attaches a sync handler so vitest never sees an
      // "unhandled rejection" even if the rejection is produced between
      // the fake-timer tick and the test's await.
      const p = getHealth().catch((e) => e)
      // Idempotent GET retries up to twice with 1s + 2s backoff.
      await vi.advanceTimersByTimeAsync(5000)
      const result = await p
      expect(result).toBeInstanceOf(ApiError)

      expect(errs).toHaveLength(1)
      expect(errs[0].kind).toBe("server_error")
      expect(errs[0].status).toBe(500)
      expect(errs[0].traceId).toBe("req_xyz_123")
    })

    it("emits kind=forbidden for 403 and does not redirect", async () => {
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      const loc = window.location as unknown as LocationStub
      mockFetchOnce(403, { detail: "nope" })
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

      expect(errs[0].kind).toBe("forbidden")
      expect(errs[0].status).toBe(403)
      expect(loc.assign).not.toHaveBeenCalled()
    })

    it("emits kind=bad_gateway for 502 and exposes it on the bus", async () => {
      vi.useFakeTimers()
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchAlways(502, { detail: "upstream down" })
      const p = getHealth().catch((e) => e)
      await vi.advanceTimersByTimeAsync(5000)
      const result = await p
      expect(result).toBeInstanceOf(ApiError)

      expect(errs[0].kind).toBe("bad_gateway")
      expect(errs[0].status).toBe(502)
    })

    it("emits kind=service_unavailable for 503 without bootstrap_required", async () => {
      vi.useFakeTimers()
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchAlways(503, { detail: "maintenance" })
      const p = getHealth().catch((e) => e)
      await vi.advanceTimersByTimeAsync(10_000)
      const result = await p
      expect(result).toBeInstanceOf(ApiError)

      expect(errs[0].kind).toBe("service_unavailable")
      expect(errs[0].status).toBe(503)
    })
  })

  describe("401 → /login?next=<current>", () => {
    // Contract pinned by lib/api.ts:542-562 (B14 Part A row 3 cd750c01):
    // 401 is *redirect XOR emit*. When the redirect fires, the handler
    // short-circuits and does NOT publish on the onApiError bus — the
    // page is about to unload, so a toast would race the navigation and
    // flash. When the redirect is skipped (already on /login or
    // /setup-required), the bus DOES receive the event so the surface
    // that's staying mounted can react.
    it("redirects to /login with the current path encoded in ?next and does NOT emit on the bus", async () => {
      const loc = installLocation("/dashboard", "?tab=agents")
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchOnce(401, { detail: "session expired" })
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

      expect(loc.assign).toHaveBeenCalledWith(
        "/login?next=" + encodeURIComponent("/dashboard?tab=agents"),
      )
      // Redirect path short-circuits — bus stays silent (no toast race).
      expect(errs).toHaveLength(0)
    })

    it("does NOT redirect when already on /login (avoids infinite loop) and emits on the bus", async () => {
      const loc = installLocation("/login", "?next=/x")
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchOnce(401, { detail: "bad credentials" })
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

      expect(loc.assign).not.toHaveBeenCalled()
      // skipRedirect branch — bus DOES receive so the login form can react.
      expect(errs[0].kind).toBe("unauthorized")
      expect(errs[0].status).toBe(401)
    })

    it("does NOT redirect when on /setup-required (bootstrap operator path) and emits on the bus", async () => {
      const loc = installLocation("/setup-required")
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchOnce(401, { detail: "not logged in yet" })
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

      expect(loc.assign).not.toHaveBeenCalled()
      expect(errs[0].kind).toBe("unauthorized")
    })
  })

  describe("503 bootstrap_required → /setup-required", () => {
    it("short-circuits into a /setup-required redirect and does not resolve", async () => {
      const loc = installLocation("/dashboard")
      mockFetchOnce(503, { error: "bootstrap_required" })

      // The short-circuit returns a never-resolving promise (the page is
      // unloading), so we race it against a timeout and assert that
      // location.assign fired synchronously.
      const p = getHealth()
      await Promise.race([
        p.then(
          () => { throw new Error("unexpectedly resolved") },
          () => { /* may reject when we're not on /dashboard — fine */ },
        ),
        new Promise((r) => setTimeout(r, 20)),
      ])

      expect(loc.assign).toHaveBeenCalledWith("/setup-required")
    })

    it("does NOT redirect when already on /setup-required and emits ApiError", async () => {
      const loc = installLocation("/setup-required")
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchOnce(503, { error: "bootstrap_required" })
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

      expect(loc.assign).not.toHaveBeenCalled()
      expect(errs[0].kind).toBe("bootstrap_required")
      expect(errs[0].status).toBe(503)
    })
  })

  describe("Network offline + timeout", () => {
    it("classifies TypeError from fetch as kind=offline", async () => {
      vi.useFakeTimers()
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      global.fetch = vi
        .fn()
        .mockImplementation(() => Promise.reject(new TypeError("Failed to fetch"))) as unknown as typeof fetch

      const p = getHealth().catch((e) => e)
      await vi.advanceTimersByTimeAsync(10_000)
      const result = await p
      expect(result).toBeInstanceOf(ApiError)

      expect(errs[0].kind).toBe("offline")
      expect(errs[0].status).toBe(0)
    })

    it("classifies AbortError (fetch timeout) as kind=timeout", async () => {
      vi.useFakeTimers()
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      global.fetch = vi.fn().mockImplementation(() => {
        // Simulate an AbortError the way fetch rejects on abort().
        const err = new DOMException("The operation was aborted.", "AbortError")
        return Promise.reject(err)
      }) as unknown as typeof fetch

      const p = getHealth().catch((e) => e)
      await vi.advanceTimersByTimeAsync(30_000)
      const result = await p
      expect(result).toBeInstanceOf(ApiError)

      expect(errs[0].kind).toBe("timeout")
    })
  })

  describe("trace ID extraction", () => {
    it("prefers the X-Trace-Id response header over parsed body", async () => {
      vi.useFakeTimers()
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      mockFetchAlways(
        500,
        { trace_id: "from_body_xxx" },
        { "X-Trace-Id": "from_header_yyy" },
      )
      const p = getHealth().catch((e) => e)
      await vi.advanceTimersByTimeAsync(5000)
      const result = await p
      expect(result).toBeInstanceOf(ApiError)

      expect(errs[0].traceId).toBe("from_header_yyy")
    })

    it("falls back to parsed.trace_id when no header is present", async () => {
      const errs: ApiError[] = []
      unsubscribers.push(onApiError((e) => errs.push(e)))

      // 400 is not retried, so a single response is enough.
      mockFetchOnce(400, { trace_id: "body_only_zzz", detail: "bad" })
      await expect(getHealth()).rejects.toBeInstanceOf(ApiError)

      expect(errs[0].traceId).toBe("body_only_zzz")
    })
  })
})
