import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  loadInitialEffectiveFeatureFlags,
  resolveServerApiBase,
} from "@/lib/feature-flags-ssr"

function headers(map: Record<string, string>) {
  return {
    get(name: string): string | null {
      return map[name.toLowerCase()] ?? null
    },
  }
}

describe("resolveServerApiBase", () => {
  const originalBackend = process.env.BACKEND_URL
  const originalPublic = process.env.NEXT_PUBLIC_API_URL

  beforeEach(() => {
    delete process.env.BACKEND_URL
    delete process.env.NEXT_PUBLIC_API_URL
  })

  afterEach(() => {
    if (originalBackend === undefined) delete process.env.BACKEND_URL
    else process.env.BACKEND_URL = originalBackend
    if (originalPublic === undefined) delete process.env.NEXT_PUBLIC_API_URL
    else process.env.NEXT_PUBLIC_API_URL = originalPublic
  })

  it("prefers the server-only BACKEND_URL over the public host header", () => {
    // OP-1728: inside the frontend container the host header would resolve to
    // a port with no listener (ECONNREFUSED). BACKEND_URL (http://caddy) wins.
    process.env.BACKEND_URL = "http://caddy"
    const base = resolveServerApiBase(
      headers({ host: "localhost:18080", "x-forwarded-proto": "http" }),
    )
    expect(base).toBe("http://caddy/api/v1")
  })

  it("prefers BACKEND_URL even when NEXT_PUBLIC_API_URL is also set", () => {
    process.env.BACKEND_URL = "http://caddy/"
    process.env.NEXT_PUBLIC_API_URL = "https://public.example.com"
    expect(resolveServerApiBase(headers({}))).toBe("http://caddy/api/v1")
  })

  it("falls back to NEXT_PUBLIC_API_URL when BACKEND_URL is unset", () => {
    process.env.NEXT_PUBLIC_API_URL = "https://public.example.com/"
    expect(resolveServerApiBase(headers({ host: "localhost:18080" }))).toBe(
      "https://public.example.com/api/v1",
    )
  })

  it("falls back to the host header as a last resort", () => {
    const base = resolveServerApiBase(
      headers({ host: "example.com", "x-forwarded-proto": "https" }),
    )
    expect(base).toBe("https://example.com/api/v1")
  })

  it("returns null when no base can be resolved", () => {
    expect(resolveServerApiBase(headers({}))).toBeNull()
  })
})

describe("loadInitialEffectiveFeatureFlags", () => {
  const originalBackend = process.env.BACKEND_URL
  const originalPublic = process.env.NEXT_PUBLIC_API_URL

  beforeEach(() => {
    delete process.env.BACKEND_URL
    delete process.env.NEXT_PUBLIC_API_URL
  })

  afterEach(() => {
    vi.restoreAllMocks()
    if (originalBackend === undefined) delete process.env.BACKEND_URL
    else process.env.BACKEND_URL = originalBackend
    if (originalPublic === undefined) delete process.env.NEXT_PUBLIC_API_URL
    else process.env.NEXT_PUBLIC_API_URL = originalPublic
  })

  it("fetches the effective flags from the internal BACKEND_URL so first paint reflects them", async () => {
    process.env.BACKEND_URL = "http://caddy"
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({ flags: { "ui.block_model.enabled": true } }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )

    const flags = await loadInitialEffectiveFeatureFlags(
      headers({ host: "localhost:18080", cookie: "session=abc" }),
    )

    expect(fetchMock).toHaveBeenCalledWith(
      "http://caddy/api/v1/feature-flags/effective",
      expect.anything(),
    )
    expect(flags["ui.block_model.enabled"]).toBe(true)
  })
})
