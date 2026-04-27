/**
 * BS.7.6 — Failed-state retry + view-log button wiring.
 *
 * Mounts a `<CatalogCard />` in state-5 (failed) with the same
 * `onRetry` / `onViewLog` closures the platforms page wires up:
 *   • `onRetry`  → `retryInstallJob(job.id)` → POST
 *                  `/api/v1/installer/jobs/{id}/retry`.
 *   • `onViewLog` → opens `<InstallLogModal />` showing the row's
 *                   `log_tail` + `error_reason`.
 *
 * The test exercises the click → handler → fetch chain end-to-end so
 * a regression in the install-failure path lights up CI without
 * needing a backend round-trip. It mirrors the BS.7.1
 * `catalog-install-wiring.test.tsx` pattern.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { useState } from "react"

import type { CatalogEntry } from "@/components/omnisight/catalog-tab"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "normal",
  usePrefersReducedMotion: () => false,
}))

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
import { InstallLogModal } from "@/components/omnisight/install-log-modal"
import { retryInstallJob, type InstallJob } from "@/lib/api"
import { TooltipProvider } from "@/components/ui/tooltip"

const FAILED_ENTRY: CatalogEntry = {
  id: "neural-blur-sdk",
  displayName: "Neural Blur SDK",
  vendor: "Acme",
  family: "software",
  version: "1.4.0",
  installState: "failed",
  description: "Edge-blur primitives for embedded AI cameras.",
  metadata: {
    failureReason: "sidecar:docker_pull:layer_unreachable",
  },
}

const FAILED_JOB: InstallJob = {
  id: "ij-failed01234",
  tenant_id: "t-abc",
  entry_id: "neural-blur-sdk",
  state: "failed",
  idempotency_key: "key-1234567890abcdef",
  sidecar_id: "omnisight-installer-1",
  protocol_version: 1,
  bytes_done: 524_288,
  bytes_total: 1_073_741_824,
  eta_seconds: null,
  log_tail: "ERROR: layer 3/8 download failed at byte 0x4f8\n",
  result_json: null,
  error_reason: "sidecar:docker_pull:layer_unreachable",
  pep_decision_id: "de-abc",
  requested_by: "u-operator",
  queued_at: "2026-04-27T10:00:00Z",
  claimed_at: "2026-04-27T10:00:01Z",
  started_at: "2026-04-27T10:00:02Z",
  completed_at: "2026-04-27T10:00:30Z",
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

describe("BS.7.6 — failed-state retry + view-log wiring", () => {
  it("clicking retry POSTs /api/v1/installer/jobs/{id}/retry with the source job id", async () => {
    const spy = mockFetchOnce(201, {
      ...FAILED_JOB,
      id: "ij-retry00001",
      state: "queued",
    })

    // Simulate the platforms page's handleRetry: pick the most recent
    // install job for the entry then call retryInstallJob.
    const handleRetry = (_entry: CatalogEntry) => {
      void retryInstallJob(FAILED_JOB.id)
    }

    render(
      <TooltipProvider>
        <CatalogCard
          entry={FAILED_ENTRY}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
          onRetry={handleRetry}
          onViewLog={() => {}}
        />
      </TooltipProvider>,
    )

    // BS.6.7 pending tooltip is absent because both handlers are wired.
    expect(
      screen.queryByTestId("catalog-card-action-retry-pending-tooltip"),
    ).toBeNull()

    const retryBtn = screen.getByTestId(
      "catalog-card-action-retry",
    ) as HTMLButtonElement
    expect(retryBtn.disabled).toBe(false)

    fireEvent.click(retryBtn)
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))

    const [url, init] = spy.mock.calls[0]!
    expect(url).toBe(`/api/v1/installer/jobs/${FAILED_JOB.id}/retry`)
    expect((init as RequestInit).method).toBe("POST")

    const body = JSON.parse((init as RequestInit).body as string) as {
      idempotency_key: string
    }
    // Auto-generated idempotency_key matches backend regex.
    expect(body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
  })

  it("clicking view-log opens the install log modal with the row's log_tail + error_reason", () => {
    // Page wrapper wires `onViewLog` → opens modal with the InstallJob.
    function Harness() {
      const [open, setOpen] = useState<InstallJob | null>(null)
      return (
        <TooltipProvider>
          <CatalogCard
            entry={FAILED_ENTRY}
            density="comfortable"
            cardPaddingClass="p-3 text-xs"
            onRetry={() => {}}
            onViewLog={() => setOpen(FAILED_JOB)}
          />
          <InstallLogModal
            job={open}
            entryDisplayName={FAILED_ENTRY.displayName}
            onClose={() => setOpen(null)}
          />
        </TooltipProvider>
      )
    }

    render(<Harness />)

    // Modal closed at mount.
    expect(screen.queryByTestId("install-log-modal")).toBeNull()

    fireEvent.click(screen.getByTestId("catalog-card-action-view-log"))

    const modal = screen.getByTestId("install-log-modal")
    expect(modal).toBeTruthy()
    // Header carries the entry display name + state label.
    expect(screen.getByTestId("install-log-modal-title").textContent).toMatch(
      /Neural Blur SDK/,
    )
    expect(screen.getByTestId("install-log-modal-state").textContent).toBe(
      "Failed",
    )
    // Error reason banner pulls from the InstallJob row.
    expect(
      screen.getByTestId("install-log-modal-error-reason").textContent,
    ).toBe("sidecar:docker_pull:layer_unreachable")
    // Log body shows the tail verbatim.
    expect(
      screen.getByTestId("install-log-modal-log-body").textContent,
    ).toBe(FAILED_JOB.log_tail)
  })

  it("does not fire a retry request when onRetry handler is omitted (BS.6.7 pending fallback)", () => {
    const spy = vi.fn()
    global.fetch = spy as unknown as typeof fetch

    render(
      <TooltipProvider>
        <CatalogCard
          entry={FAILED_ENTRY}
          density="comfortable"
          cardPaddingClass="p-3 text-xs"
        />
      </TooltipProvider>,
    )

    // Without onRetry the BS.6.7 pending tooltip wraps the disabled
    // button — tooltip wrapper testid renders, button is disabled.
    expect(
      screen.getByTestId("catalog-card-action-retry-pending-tooltip"),
    ).toBeTruthy()
    const btn = screen.getByTestId(
      "catalog-card-action-retry",
    ) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(spy).not.toHaveBeenCalled()
  })
})
