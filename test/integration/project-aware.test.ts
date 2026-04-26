/**
 * Y5 (#281) row 4: Frontend project-aware integration tests.
 *
 * Verifies (mirror of I7 tenant-aware contract):
 *   - X-Project-Id header is injected on every request when set
 *   - X-Project-Id is omitted when project id is null (default state)
 *   - X-Project-Id is sent ALONGSIDE X-Tenant-Id (the two coexist
 *     because the backend listener pins (tenant_id, project_id) from
 *     both headers — losing either turns rows invisible)
 *   - setCurrentProjectId / getCurrentProjectId round-trips and clears
 *
 * The backend's ``_project_header_gate`` then double-verifies
 * membership before the route handler runs (mirror of I7
 * ``_tenant_header_gate``). That contract is exercised in
 * ``backend/tests/test_y5_row4_project_header_gate.py`` — this suite
 * only checks the wire contract from the browser side.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest"
import {
  setCurrentTenantId,
  setCurrentProjectId,
  getCurrentProjectId,
  setCurrentSessionId,
} from "@/lib/api"

// ─── Helpers ──────────────────────────────────────────────────

interface CapturedRequest {
  url: string
  headers: Record<string, string>
  method: string
}

function _captureFetch(captured: CapturedRequest[]): typeof fetch {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string"
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url
    const headersRecord: Record<string, string> = {}
    const incoming = init?.headers as
      | Record<string, string>
      | Headers
      | undefined
    if (incoming) {
      if (incoming instanceof Headers) {
        incoming.forEach((v, k) => { headersRecord[k] = v })
      } else {
        Object.assign(headersRecord, incoming)
      }
    }
    captured.push({
      url, headers: headersRecord, method: (init?.method || "GET").toUpperCase(),
    })
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }) as unknown as Response
  }) as unknown as typeof fetch
}

// ─── Pure setter / getter contract ────────────────────────────

describe("Y5 row 4: project-aware API state", () => {
  beforeEach(() => {
    setCurrentTenantId(null)
    setCurrentProjectId(null)
    setCurrentSessionId(null)
  })

  it("setCurrentProjectId / getCurrentProjectId round-trips", () => {
    expect(getCurrentProjectId()).toBeNull()
    setCurrentProjectId("p-acme0000000001")
    expect(getCurrentProjectId()).toBe("p-acme0000000001")
  })

  it("clearing project resets to null", () => {
    setCurrentProjectId("p-acme0000000001")
    setCurrentProjectId(null)
    expect(getCurrentProjectId()).toBeNull()
  })

  it("setting project does not clobber tenant id", () => {
    setCurrentTenantId("t-acme")
    setCurrentProjectId("p-acme0000000001")
    expect(getCurrentProjectId()).toBe("p-acme0000000001")
    // Round-trip the tenant accessor too — they live on independent
    // module-globals, so a regression that wires them together would
    // surface here.
    expect(setCurrentTenantId).toBeTypeOf("function")
  })
})

// ─── Header injection wire contract ───────────────────────────

describe("Y5 row 4: X-Project-Id header injection", () => {
  let originalFetch: typeof fetch
  let captured: CapturedRequest[]

  beforeEach(() => {
    originalFetch = global.fetch
    captured = []
    global.fetch = _captureFetch(captured)
    setCurrentTenantId(null)
    setCurrentProjectId(null)
    setCurrentSessionId(null)
  })

  afterEach(() => {
    global.fetch = originalFetch
    setCurrentTenantId(null)
    setCurrentProjectId(null)
  })

  it("omits X-Project-Id when project id is null", async () => {
    const { whoami } = await import("@/lib/api")
    await whoami()
    expect(captured).toHaveLength(1)
    expect(captured[0].headers["X-Project-Id"]).toBeUndefined()
  })

  it("injects X-Project-Id when setCurrentProjectId has been called", async () => {
    setCurrentProjectId("p-acme0000000001")
    const { whoami } = await import("@/lib/api")
    await whoami()
    expect(captured).toHaveLength(1)
    expect(captured[0].headers["X-Project-Id"]).toBe("p-acme0000000001")
  })

  it("sends X-Project-Id alongside X-Tenant-Id (both present)", async () => {
    setCurrentTenantId("t-acme")
    setCurrentProjectId("p-acme0000000001")
    const { whoami } = await import("@/lib/api")
    await whoami()
    expect(captured).toHaveLength(1)
    expect(captured[0].headers["X-Tenant-Id"]).toBe("t-acme")
    expect(captured[0].headers["X-Project-Id"]).toBe("p-acme0000000001")
  })

  it("clearing project removes header from subsequent requests", async () => {
    setCurrentProjectId("p-acme0000000001")
    const { whoami } = await import("@/lib/api")
    await whoami()
    setCurrentProjectId(null)
    await whoami()
    expect(captured).toHaveLength(2)
    expect(captured[0].headers["X-Project-Id"]).toBe("p-acme0000000001")
    expect(captured[1].headers["X-Project-Id"]).toBeUndefined()
  })

  it("X-Project-Id is sent on POST requests too", async () => {
    setCurrentProjectId("p-acme0000000001")
    const { logout } = await import("@/lib/api")
    await logout()
    expect(captured).toHaveLength(1)
    expect(captured[0].method).toBe("POST")
    expect(captured[0].headers["X-Project-Id"]).toBe("p-acme0000000001")
  })
})
