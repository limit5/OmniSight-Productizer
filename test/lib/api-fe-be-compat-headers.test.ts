/**
 * OP-1483 — `lib/api.ts` must emit the FE/BE compatibility bookkeeping
 * headers on every fetch.
 *
 * The runtime detector on the backend side
 * (`backend/main.py::_frontend_compat_observer`) keys on the exact
 * header pair below. If the FE drops one of them, the BE silently
 * stops detecting skew — exactly the Codex 2026-05-18 morning-incident
 * failure mode we're closing. This suite pins the contract.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { getFrontendCompatHeaders, getHealth } from "@/lib/api"

describe("OP-1483 — FE/BE compat bookkeeping headers", () => {
  const originalFetch = global.fetch

  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    global.fetch = originalFetch
  })

  it("getFrontendCompatHeaders() returns both required header keys", () => {
    const headers = getFrontendCompatHeaders()
    expect(Object.keys(headers).sort()).toEqual([
      "X-OmniSight-Frontend-Api-Contract",
      "X-OmniSight-Frontend-Bundle",
    ])
    // Both values must be non-empty — the backend treats blank values
    // as "no observation" and silently drops them, which would defeat
    // the detector.
    expect(headers["X-OmniSight-Frontend-Bundle"].length).toBeGreaterThan(0)
    expect(
      headers["X-OmniSight-Frontend-Api-Contract"].length,
    ).toBeGreaterThan(0)
  })

  it("default fallback values are 'unknown' bundle + 'v1' contract", () => {
    // Vitest jsdom doesn't bake the Next.js build-time env, so the
    // module-level fallback path is what runs here. Pinning the
    // fallbacks lets us spot a regression where the constants change.
    const headers = getFrontendCompatHeaders()
    expect(headers["X-OmniSight-Frontend-Bundle"]).toBe("unknown")
    expect(headers["X-OmniSight-Frontend-Api-Contract"]).toBe("v1")
  })

  it("request() attaches both headers to outbound fetches", async () => {
    const spy = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    global.fetch = spy as unknown as typeof fetch

    await getHealth()

    expect(spy).toHaveBeenCalledTimes(1)
    const [, init] = spy.mock.calls[0]
    const headers = init?.headers as Record<string, string>
    expect(headers).toBeTruthy()
    expect(headers["X-OmniSight-Frontend-Bundle"]).toBeTruthy()
    expect(headers["X-OmniSight-Frontend-Api-Contract"]).toBeTruthy()
    // Cross-check that the values match getFrontendCompatHeaders() so
    // the central wrapper and the helper agree.
    const expected = getFrontendCompatHeaders()
    expect(headers["X-OmniSight-Frontend-Bundle"]).toBe(
      expected["X-OmniSight-Frontend-Bundle"],
    )
    expect(headers["X-OmniSight-Frontend-Api-Contract"]).toBe(
      expected["X-OmniSight-Frontend-Api-Contract"],
    )
  })
})
