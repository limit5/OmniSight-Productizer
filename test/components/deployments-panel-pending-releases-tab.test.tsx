/**
 * OP-943 G7 — Tab-integration test for the D17 deployments dashboard.
 *
 * Asserts AC #1: the deployments dashboard now hosts a "Pending
 * releases" tab that mounts {@link PendingReleasesTable}. The
 * deployments view stays the default tab (so the OP-889 contract tests
 * keep passing); switching to the new tab renders the aggregator table
 * with the rows handed to it via `pendingReleasesProps`.
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
}))

import { DeploymentsPanel, emptySnapshot } from "@/components/omnisight/admin/DeploymentsPanel"
import type {
  FetchReleases,
  PendingReleaseRow,
} from "@/components/omnisight/admin/PendingReleasesTable"

function makeRow(over: Partial<PendingReleaseRow> = {}): PendingReleaseRow {
  return {
    release_id: "RELEASE-301",
    version: "v3.0.1",
    state: "canary_5",
    is_hotfix: false,
    last_transition_at: "2026-05-11T10:00:00Z",
    blocking_seconds: 600,
    approval_pending: true,
    approval_reason: "canary_gate",
    approval_requested_at: "2026-05-11T10:00:00Z",
    canary_percent: 5,
    slo_snapshot: null,
    can_approve: true,
    ...over,
  }
}

describe("DeploymentsPanel — Pending releases tab", () => {
  it("defaults to the deployments tab and exposes a pending-releases tab", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () => ({
      releases: [makeRow()],
      generated_at: "2026-05-11T11:30:00Z",
    }))
    render(
      <DeploymentsPanel
        initialSnapshot={emptySnapshot()}
        pendingReleasesProps={{ fetchReleases, nowImpl: () => Date.parse("2026-05-11T12:00:00Z") }}
      />,
    )
    // Deployments view is the default — its section is on screen.
    expect(screen.getByTestId("deployments-panel")).toBeInTheDocument()
    // The pending-releases tab content is not mounted yet.
    expect(screen.queryByTestId("pending-releases-table")).toBeNull()
    // Both tab triggers exist.
    expect(screen.getByTestId("deployments-panel-tab-deployments")).toBeInTheDocument()
    expect(screen.getByTestId("deployments-panel-tab-pending-releases")).toBeInTheDocument()
  })

  it("switching to the pending-releases tab mounts the aggregator table", async () => {
    const fetchReleases: FetchReleases = vi.fn(async () => ({
      releases: [makeRow({ release_id: "RELEASE-777" })],
      generated_at: "2026-05-11T11:30:00Z",
    }))
    render(
      <DeploymentsPanel
        initialSnapshot={emptySnapshot()}
        pendingReleasesProps={{ fetchReleases, nowImpl: () => Date.parse("2026-05-11T12:00:00Z") }}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId("deployments-panel-tab-pending-releases"))
    })
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table")).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByTestId("pending-releases-table-row-RELEASE-777")).toBeInTheDocument()
    })
    expect(fetchReleases).toHaveBeenCalled()
  })
})
