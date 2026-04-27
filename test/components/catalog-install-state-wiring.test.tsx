/**
 * BS.7.5 — End-to-end SSE → catalog card state-3 wiring contract.
 *
 * Mounts the BS.6.2 ``<CatalogCard />`` against a fixture render that
 * mirrors the platforms page's wiring (``useInstallJobs()`` →
 * ``pickInstallJobForEntry`` → ``deriveCatalogStateFromInstallJob``
 * + ``deriveCatalogProgressPercent`` → entry override). Then pushes
 * fake ``installer_progress`` SSE events into the hook and asserts:
 *
 *   1. baseline ``available`` card paints state 1 (``data-state="available"``)
 *   2. first SSE ``running`` event flips to state 3 (``installing``)
 *      with the conic-gradient + ring-spin + percentage readout
 *   3. follow-up bytes_done ticks update the conic-gradient angle
 *   4. ``completed`` SSE flips to state 2 (``installed``)
 *   5. ``failed`` SSE flips to state 5 (``failed``)
 *   6. ``cancelled`` SSE reverts the card to the entry's static state
 *      (so ``update-available`` chips are preserved across an aborted
 *      install)
 *
 * The PEP HOLD path is owned by the backend; this test only locks the
 * SSE → card visual contract that BS.7.5 is responsible for.
 */

import { describe, expect, it, vi } from "vitest"
import { act, render, screen } from "@testing-library/react"

import type { CatalogEntry } from "@/components/omnisight/catalog-tab"
import type { InstallJobState } from "@/lib/api"

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "normal",
  usePrefersReducedMotion: () => false,
}))

// Same mock pattern as the existing BS.6.8 / BS.7.1 catalog-card tests
// — break the category-strip → catalog-tab init cycle so the card
// renders standalone in jsdom without dragging the chip strip in.
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

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
}))

import * as api from "@/lib/api"
import { CatalogCard } from "@/components/omnisight/catalog-card"
import {
  deriveCatalogProgressPercent,
  deriveCatalogStateFromInstallJob,
  pickInstallJobForEntry,
  useInstallJobs,
} from "@/hooks/use-install-jobs"
import { TooltipProvider } from "@/components/ui/tooltip"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

const SAMPLE_ENTRY: CatalogEntry = {
  id: "neural-blur-sdk",
  displayName: "Neural Blur SDK",
  vendor: "Acme",
  family: "software",
  version: "1.4.0",
  installState: "available",
  description: "Edge-blur primitives for embedded AI cameras.",
}

interface ProgressOverrides {
  job_id?: string
  state?: InstallJobState
  stage?: string
  bytes_done?: number
  bytes_total?: number | null
  eta_seconds?: number | null
  log_tail?: string
  sidecar_id?: string | null
  entry_id?: string | null
}

function mkProgress(overrides: ProgressOverrides = {}): {
  event: "installer_progress"
  data: {
    job_id: string
    state: InstallJobState
    stage: string
    bytes_done: number
    bytes_total: number | null
    eta_seconds: number | null
    log_tail: string
    sidecar_id: string | null
    entry_id: string | null
    timestamp: string
  }
} {
  return {
    event: "installer_progress",
    data: {
      job_id: "job-A",
      state: "running",
      stage: "download",
      bytes_done: 0,
      bytes_total: 1000,
      eta_seconds: null,
      log_tail: "",
      sidecar_id: "sidecar-1",
      entry_id: SAMPLE_ENTRY.id,
      timestamp: "2026-04-27T00:00:00",
      ...overrides,
    },
  }
}

/**
 * Mirrors the relevant logic in
 * ``app/settings/platforms/page.tsx::PlatformsPageInner.renderCardOverlay``
 * so this test exercises the same wiring the page uses (helpers + hook
 * → entry override → catalog card props).
 */
function CatalogCardWithLiveState({ entry }: { entry: CatalogEntry }) {
  const { jobs } = useInstallJobs()
  const fallback = entry.installState ?? "available"
  const job = pickInstallJobForEntry(jobs, entry.id)
  const installState = deriveCatalogStateFromInstallJob(job, fallback)
  const installProgressPercent = deriveCatalogProgressPercent(job)
  const liveEntry =
    installState !== entry.installState ? { ...entry, installState } : entry
  return (
    <CatalogCard
      entry={liveEntry}
      density="comfortable"
      cardPaddingClass="p-3 text-xs"
      installProgressPercent={installProgressPercent}
    />
  )
}

describe("BS.7.5 — SSE installer_progress flips catalog card visual state", () => {
  it("baseline: card paints state 1 (available) until the first SSE event", () => {
    primeSSE()

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={SAMPLE_ENTRY} />
      </TooltipProvider>,
    )

    const card = screen.getByTestId(`catalog-card-${SAMPLE_ENTRY.id}`)
    expect(card).toHaveAttribute("data-state", "available")
    expect(screen.getByTestId("catalog-card-state-chip")).toHaveTextContent(
      "Available",
    )
    expect(screen.queryByTestId("catalog-card-progress-block")).toBeNull()
  })

  it("first running SSE flips to state 3 (installing) with conic-gradient + ring-spin + bytes counter", () => {
    const sse = primeSSE()

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={SAMPLE_ENTRY} />
      </TooltipProvider>,
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 250, bytes_total: 1000 }))
    })

    const card = screen.getByTestId(`catalog-card-${SAMPLE_ENTRY.id}`)
    expect(card).toHaveAttribute("data-state", "installing")
    expect(card).toHaveAttribute("data-progress", "25.00")

    // BS.6.2 visual: conic-gradient outer wrapper + hazard overlay +
    // progress ring + ring-spin icon + percentage readout all present.
    expect(screen.getByTestId("catalog-card-hazard-overlay")).toBeInTheDocument()
    expect(screen.getByTestId("catalog-card-progress-ring")).toBeInTheDocument()
    expect(
      screen.getByTestId("catalog-card-progress-block"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("catalog-card-progress-value")).toHaveTextContent(
      "25%",
    )

    const icon = screen.getByTestId("catalog-card-state-icon")
    expect(icon).toHaveAttribute("data-state-icon", "installing")
    // SVG elements expose ``className`` as ``SVGAnimatedString``; read
    // the raw class attribute instead so the substring check works.
    expect(icon.getAttribute("class") ?? "").toContain("ring-spin")
  })

  it("follow-up SSE bytes_done ticks update the live progress angle", () => {
    const sse = primeSSE()

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={SAMPLE_ENTRY} />
      </TooltipProvider>,
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 100, bytes_total: 1000 }))
    })
    expect(screen.getByTestId("catalog-card-progress-value")).toHaveTextContent(
      "10%",
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 750, bytes_total: 1000 }))
    })
    expect(screen.getByTestId("catalog-card-progress-value")).toHaveTextContent(
      "75%",
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 1000, bytes_total: 1000 }))
    })
    expect(screen.getByTestId("catalog-card-progress-value")).toHaveTextContent(
      "100%",
    )
  })

  it("completed SSE flips card to state 2 (installed)", () => {
    const sse = primeSSE()

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={SAMPLE_ENTRY} />
      </TooltipProvider>,
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 500, bytes_total: 1000 }))
    })
    act(() => {
      sse.emit(
        mkProgress({ state: "completed", bytes_done: 1000, bytes_total: 1000 }),
      )
    })

    const card = screen.getByTestId(`catalog-card-${SAMPLE_ENTRY.id}`)
    expect(card).toHaveAttribute("data-state", "installed")
    expect(screen.getByTestId("catalog-card-state-chip")).toHaveTextContent(
      "Installed",
    )
    // Installing-state visual artefacts are gone.
    expect(screen.queryByTestId("catalog-card-progress-block")).toBeNull()
    expect(screen.queryByTestId("catalog-card-hazard-overlay")).toBeNull()
  })

  it("failed SSE flips card to state 5 (failed)", () => {
    const sse = primeSSE()

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={SAMPLE_ENTRY} />
      </TooltipProvider>,
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 500, bytes_total: 1000 }))
    })
    act(() => {
      sse.emit(mkProgress({ state: "failed", bytes_done: 500, bytes_total: 1000 }))
    })

    const card = screen.getByTestId(`catalog-card-${SAMPLE_ENTRY.id}`)
    expect(card).toHaveAttribute("data-state", "failed")
    expect(screen.getByTestId("catalog-card-state-chip")).toHaveTextContent(
      "Failed",
    )
    // Retry CTA is rendered in the failed state.
    expect(screen.getByTestId("catalog-card-action-retry")).toBeInTheDocument()
  })

  it("cancelled SSE reverts to the entry's static state (preserves update-available)", () => {
    const sse = primeSSE()
    const entry: CatalogEntry = {
      ...SAMPLE_ENTRY,
      installState: "update-available",
    }

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={entry} />
      </TooltipProvider>,
    )

    act(() => {
      sse.emit(mkProgress({ state: "running", bytes_done: 100, bytes_total: 1000 }))
    })
    expect(
      screen.getByTestId(`catalog-card-${entry.id}`),
    ).toHaveAttribute("data-state", "installing")

    act(() => {
      sse.emit(
        mkProgress({ state: "cancelled", bytes_done: 100, bytes_total: 1000 }),
      )
    })

    // Cancelled rows are treated as if absent — the entry's seeded
    // ``update-available`` chip survives the aborted install.
    const card = screen.getByTestId(`catalog-card-${entry.id}`)
    expect(card).toHaveAttribute("data-state", "update-available")
    expect(screen.getByTestId("catalog-card-state-chip")).toHaveTextContent(
      "Update available",
    )
  })

  it("install for a different entry does not flip this card", () => {
    const sse = primeSSE()

    render(
      <TooltipProvider>
        <CatalogCardWithLiveState entry={SAMPLE_ENTRY} />
      </TooltipProvider>,
    )

    act(() => {
      sse.emit(
        mkProgress({
          job_id: "job-other",
          entry_id: "some-other-entry",
          state: "running",
          bytes_done: 100,
          bytes_total: 1000,
        }),
      )
    })

    const card = screen.getByTestId(`catalog-card-${SAMPLE_ENTRY.id}`)
    expect(card).toHaveAttribute("data-state", "available")
  })
})
