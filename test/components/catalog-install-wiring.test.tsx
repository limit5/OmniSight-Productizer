/**
 * BS.7.1 — End-to-end install button wiring contract test.
 *
 * Mounts a `<CatalogCard />` with the same `onInstall` handler the
 * platforms page wires up (a thin closure around
 * `createInstallJob(entry.id)`), then asserts that clicking the install
 * button:
 *   1. flips the BS.6.7 pending-tooltip wrapper off (wired path),
 *   2. fires a single `POST /api/v1/installer/jobs` against the global
 *      `fetch`,
 *   3. with a body that carries the right `entry_id`,
 *      `idempotency_key` (matching the backend's
 *      `^[A-Za-z0-9_\-]{16,64}$` regex), and an empty `metadata` dict.
 *
 * The PEP gateway HOLD path is owned by the backend
 * (`tool="install_entry"` lands in `tier_unlisted`); the frontend just
 * has to reach the route. Once the request hits, the existing R20-A
 * coaching toast and `<ApiErrorToastCenter />` chain take over — none
 * of which is exercised here. This test only locks the click → POST
 * contract that BS.7.1 is responsible for.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"

import type { CatalogEntry } from "@/components/omnisight/catalog-tab"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "normal",
  usePrefersReducedMotion: () => false,
}))

// Vitest's ESM transform evaluates side-effects of `category-strip`
// before `catalog-tab` finishes initialising — its `["all",
// ...CATALOG_FAMILIES]` spread reads `undefined` and throws "not
// iterable". The card under test never renders the chip strip, so we
// stub it with a no-op component to break the cycle. Mirrors the
// existing BS.6.8 catalog-card test mock.
vi.mock("@/components/omnisight/category-strip", () => ({
  CategoryStrip: () => null,
  CATEGORY_STRIP_FAMILIES: [
    "all",
    "mobile",
    "embedded",
    "web",
    "software",
    "custom",
  ],
  getCategoryStripPalette: () => ({}),
}))

import { CatalogCard } from "@/components/omnisight/catalog-card"
import { createInstallJob } from "@/lib/api"
import { TooltipProvider } from "@/components/ui/tooltip"

const ENDPOINT = "/api/v1/installer/jobs"

const SAMPLE_ENTRY: CatalogEntry = {
  id: "neural-blur-sdk",
  displayName: "Neural Blur SDK",
  vendor: "Acme",
  family: "software",
  version: "1.4.0",
  installState: "available",
  description: "Edge-blur primitives for embedded AI cameras.",
}

function mockFetchOnce(status: number, body: unknown) {
  const text = JSON.stringify(body)
  const res = new Response(text, {
    status,
    headers: { "Content-Type": "application/json" },
  })
  const spy = vi.fn().mockResolvedValueOnce(res)
  global.fetch = spy as unknown as typeof fetch
  return spy
}

describe("BS.7.1 — catalog card install button wiring", () => {
  it("clicking install posts /api/v1/installer/jobs with entry_id + auto idempotency_key", async () => {
    const spy = mockFetchOnce(201, {
      id: "ij-0123456789ab",
      tenant_id: "t-abc",
      entry_id: SAMPLE_ENTRY.id,
      state: "queued",
      idempotency_key: "auto-key-from-frontend",
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
    })

    // The exact closure shape `app/settings/platforms/page.tsx` wires
    // up — caller hands the click through to `createInstallJob`.
    const handleInstall = (entry: CatalogEntry) => {
      void createInstallJob(entry.id)
    }

    render(
      <TooltipProvider>
        <CatalogCard
          entry={SAMPLE_ENTRY}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
          onInstall={handleInstall}
        />
      </TooltipProvider>,
    )

    // 1. BS.6.7 pending-tooltip wrapper must be absent on the wired
    //    path so there is no extra DOM around the live button.
    expect(
      screen.queryByTestId("catalog-card-action-install-pending-tooltip"),
    ).toBeNull()

    const btn = screen.getByTestId(
      "catalog-card-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(false)

    // 2. Click → fetch fires.
    fireEvent.click(btn)
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))

    const [url, init] = spy.mock.calls[0]!
    expect(url).toBe(ENDPOINT)
    expect((init as RequestInit).method).toBe("POST")

    const body = JSON.parse((init as RequestInit).body as string) as {
      entry_id: string
      idempotency_key: string
      metadata: Record<string, unknown>
    }
    expect(body.entry_id).toBe(SAMPLE_ENTRY.id)
    expect(body.metadata).toEqual({})
    expect(body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
  })

  it("does not fire a request when the install handler is omitted", () => {
    const spy = vi.fn()
    global.fetch = spy as unknown as typeof fetch

    render(
      <TooltipProvider>
        <CatalogCard
          entry={SAMPLE_ENTRY}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
        />
      </TooltipProvider>,
    )

    // No handler → BS.6.7 pending tooltip wraps the button + the button
    // is disabled. Click is a no-op, fetch never runs.
    const btn = screen.getByTestId(
      "catalog-card-action-install",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(
      screen.getByTestId("catalog-card-action-install-pending-tooltip"),
    ).toBeTruthy()

    fireEvent.click(btn)
    expect(spy).not.toHaveBeenCalled()
  })
})
