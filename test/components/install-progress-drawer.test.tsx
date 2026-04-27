/**
 * BS.7.3 — InstallProgressDrawer tests.
 *
 * Locks the bottom-right floating drawer's contract for the BS.7
 * install pipeline. The drawer is purely presentational — it accepts
 * an array of `InstallJob` rows and renders chip / panel UI off them.
 * BS.7.4 (`hooks/use-install-jobs.ts`) will hand it the live SSE feed;
 * these tests verify the drawer behaves correctly in isolation so the
 * hook can be tested separately.
 *
 * Coverage:
 *   1. renders nothing when no jobs
 *   2. renders nothing when jobs are all completed/failed/cancelled
 *      (the drawer filters to in-flight states internally)
 *   3. collapsed by default — chip shows "N installing"
 *   4. clicking chip expands the panel + reveals per-job rows
 *   5. expanded panel shows percentage from bytes_done/bytes_total
 *   6. expanded panel shows backend-supplied ETA (eta_seconds)
 *   7. unknown bytes_total → indeterminate bar + "—%" placeholder
 *   8. successive ticks derive a non-zero speed (KB/s)
 *   9. cancel button only renders when onCancel is wired; click fires it
 *   10. collapse button hides the panel back to the chip
 *   11. format helpers — formatInstallBytes / Speed / Eta + deriveInstallPercent
 */

import { describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen } from "@testing-library/react"

import {
  InstallProgressDrawer,
  formatInstallBytes,
  formatInstallEta,
  formatInstallSpeed,
  deriveInstallPercent,
} from "@/components/omnisight/install-progress-drawer"
import type { InstallJob } from "@/lib/api"

function mkJob(overrides: Partial<InstallJob> = {}): InstallJob {
  return {
    id: overrides.id ?? "job-1",
    tenant_id: "t-default",
    entry_id: overrides.entry_id ?? "entry-foo",
    state: overrides.state ?? "running",
    idempotency_key: "idem-1",
    sidecar_id: "sidecar-1",
    protocol_version: 1,
    bytes_done: overrides.bytes_done ?? 0,
    bytes_total: overrides.bytes_total ?? 0,
    eta_seconds: overrides.eta_seconds ?? null,
    log_tail: overrides.log_tail ?? "",
    result_json: overrides.result_json ?? null,
    error_reason: null,
    pep_decision_id: null,
    requested_by: "u1",
    queued_at: "2026-04-27T00:00:00Z",
    claimed_at: null,
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

describe("InstallProgressDrawer — visibility gate", () => {
  it("renders nothing when jobs array is empty", () => {
    const { container } = render(<InstallProgressDrawer jobs={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing when all jobs are terminal (completed/failed/cancelled)", () => {
    const jobs: InstallJob[] = [
      mkJob({ id: "j-c", state: "completed" }),
      mkJob({ id: "j-f", state: "failed" }),
      mkJob({ id: "j-x", state: "cancelled" }),
    ]
    const { container } = render(<InstallProgressDrawer jobs={jobs} />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing without crash when jobs prop is omitted", () => {
    const { container } = render(<InstallProgressDrawer />)
    expect(container.firstChild).toBeNull()
  })
})

describe("InstallProgressDrawer — collapsed chip", () => {
  it("shows '⟳ N installing' chip when there is at least one in-flight job", () => {
    const jobs = [
      mkJob({ id: "j-1", state: "running" }),
      mkJob({ id: "j-2", state: "queued" }),
    ]
    render(<InstallProgressDrawer jobs={jobs} />)
    const chip = screen.getByTestId("install-drawer-chip")
    expect(chip).toBeInTheDocument()
    expect(chip).toHaveAttribute("aria-expanded", "false")
    expect(screen.getByTestId("install-drawer-chip-count")).toHaveTextContent("2")
    expect(screen.queryByTestId("install-drawer-panel")).toBeNull()
  })

  it("clicking the chip expands the drawer panel", () => {
    const jobs = [mkJob({ id: "j-1", state: "running" })]
    render(<InstallProgressDrawer jobs={jobs} />)
    fireEvent.click(screen.getByTestId("install-drawer-chip"))
    expect(screen.getByTestId("install-drawer-panel")).toBeInTheDocument()
    expect(screen.getByTestId("install-drawer-row-j-1")).toBeInTheDocument()
  })
})

describe("InstallProgressDrawer — expanded panel content", () => {
  it("shows percentage derived from bytes_done / bytes_total", () => {
    const jobs = [
      mkJob({
        id: "j-pct",
        state: "running",
        bytes_done: 250,
        bytes_total: 1000,
      }),
    ]
    render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    expect(screen.getByTestId("install-drawer-percent-j-pct")).toHaveTextContent("25%")
    expect(screen.getByTestId("install-drawer-bar-j-pct")).toHaveAttribute(
      "data-progress-known",
      "true",
    )
  })

  it("uses backend eta_seconds when provided", () => {
    const jobs = [
      mkJob({
        id: "j-eta",
        state: "running",
        bytes_done: 100,
        bytes_total: 1000,
        eta_seconds: 125, // 2:05
      }),
    ]
    render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    expect(screen.getByTestId("install-drawer-eta-j-eta")).toHaveTextContent("ETA 02:05")
  })

  it("falls back to indeterminate bar + '—%' when bytes_total is unknown", () => {
    const jobs = [
      mkJob({
        id: "j-unk",
        state: "running",
        bytes_done: 500,
        bytes_total: null,
      }),
    ]
    render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    expect(screen.getByTestId("install-drawer-percent-j-unk")).toHaveTextContent("—%")
    expect(screen.getByTestId("install-drawer-bar-j-unk")).toHaveAttribute(
      "data-progress-known",
      "false",
    )
  })

  it("derives a non-zero speed from successive bytes_done ticks", () => {
    let now = 1_000_000
    const clock = () => now
    const initial = [mkJob({ id: "j-spd", state: "running", bytes_done: 0, bytes_total: 1024 * 1024 })]
    const { rerender } = render(
      <InstallProgressDrawer jobs={initial} initialOpen nowMs={clock} />,
    )
    // First render seeds the sample with bytes=0 @ t=now.
    expect(screen.getByTestId("install-drawer-speed-j-spd")).toHaveTextContent("—")

    // Advance one second, push 64 KiB more bytes_done — drawer should
    // derive 64 KB/s (rounded to "64.0 KB/s" by formatInstallBytes).
    act(() => {
      now += 1000
      rerender(
        <InstallProgressDrawer
          jobs={[mkJob({ id: "j-spd", state: "running", bytes_done: 64 * 1024, bytes_total: 1024 * 1024 })]}
          initialOpen
          nowMs={clock}
        />,
      )
    })
    expect(screen.getByTestId("install-drawer-speed-j-spd")).toHaveTextContent("64.0 KB/s")
  })

  it("uses metadata.display_name from result_json when available", () => {
    const jobs = [
      mkJob({
        id: "j-name",
        state: "running",
        entry_id: "entry-id-fallback",
        result_json: { display_name: "Vendor X SDK" },
      }),
    ]
    render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    const row = screen.getByTestId("install-drawer-row-j-name")
    expect(row).toHaveTextContent("Vendor X SDK")
  })

  it("falls back to entry_id when no display_name metadata is present", () => {
    const jobs = [
      mkJob({
        id: "j-noname",
        state: "running",
        entry_id: "entry-id-bare",
      }),
    ]
    render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    expect(screen.getByTestId("install-drawer-row-j-noname")).toHaveTextContent("entry-id-bare")
  })
})

describe("InstallProgressDrawer — operator interactions", () => {
  it("only renders the cancel button when onCancel is wired", () => {
    const jobs = [mkJob({ id: "j-1", state: "running" })]
    const { rerender } = render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    expect(screen.queryByTestId("install-drawer-cancel-j-1")).toBeNull()

    const onCancel = vi.fn()
    rerender(<InstallProgressDrawer jobs={jobs} initialOpen onCancel={onCancel} />)
    const btn = screen.getByTestId("install-drawer-cancel-j-1")
    fireEvent.click(btn)
    expect(onCancel).toHaveBeenCalledWith("j-1")
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it("collapses back to the chip when the collapse button is clicked", () => {
    const jobs = [mkJob({ id: "j-1", state: "running" })]
    render(<InstallProgressDrawer jobs={jobs} initialOpen />)
    expect(screen.getByTestId("install-drawer-panel")).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("install-drawer-collapse"))
    expect(screen.queryByTestId("install-drawer-panel")).toBeNull()
    expect(screen.getByTestId("install-drawer-chip")).toBeInTheDocument()
  })
})

describe("InstallProgressDrawer — format helpers (pure)", () => {
  it("formatInstallBytes: cascade B / KB / MB / GB / TB with 1-decimal under 100, drop decimal at >= 100", () => {
    expect(formatInstallBytes(0)).toBe("0 B")
    expect(formatInstallBytes(512)).toBe("512 B")
    expect(formatInstallBytes(1024)).toBe("1.0 KB")
    expect(formatInstallBytes(99 * 1024)).toBe("99.0 KB")
    expect(formatInstallBytes(150 * 1024)).toBe("150 KB")
    expect(formatInstallBytes(5 * 1024 * 1024)).toBe("5.0 MB")
    expect(formatInstallBytes(250 * 1024 * 1024 * 1024)).toBe("250 GB")
    expect(formatInstallBytes(null)).toBe("—")
    expect(formatInstallBytes(undefined)).toBe("—")
    expect(formatInstallBytes(-1)).toBe("—")
    expect(formatInstallBytes(Number.NaN)).toBe("—")
  })

  it("formatInstallSpeed: returns '—' for null / 0 / negative; otherwise '<n> <unit>/s'", () => {
    expect(formatInstallSpeed(null)).toBe("—")
    expect(formatInstallSpeed(undefined)).toBe("—")
    expect(formatInstallSpeed(0)).toBe("—")
    expect(formatInstallSpeed(-100)).toBe("—")
    expect(formatInstallSpeed(2048)).toBe("2.0 KB/s")
    expect(formatInstallSpeed(5 * 1024 * 1024)).toBe("5.0 MB/s")
  })

  it("formatInstallEta: mm:ss under 1h, h:mm:ss at >= 1h, '—' for null/negative", () => {
    expect(formatInstallEta(null)).toBe("—")
    expect(formatInstallEta(undefined)).toBe("—")
    expect(formatInstallEta(-5)).toBe("—")
    expect(formatInstallEta(0)).toBe("00:00")
    expect(formatInstallEta(45)).toBe("00:45")
    expect(formatInstallEta(125)).toBe("02:05")
    expect(formatInstallEta(3600)).toBe("1:00:00")
    expect(formatInstallEta(3661)).toBe("1:01:01")
    // Cap at 99:59:59 — absurd values from a near-zero first speed
    // sample don't blow the chip width.
    expect(formatInstallEta(99 * 3600 + 59 * 60 + 59 + 10)).toBe("99:59:59")
  })

  it("deriveInstallPercent: bytes_done / bytes_total clamped 0..100; null for unknown total", () => {
    expect(deriveInstallPercent(mkJob({ bytes_done: 0, bytes_total: 0 }))).toBeNull()
    expect(deriveInstallPercent(mkJob({ bytes_done: 0, bytes_total: null }))).toBeNull()
    expect(deriveInstallPercent(mkJob({ bytes_done: 250, bytes_total: 1000 }))).toBe(25)
    expect(deriveInstallPercent(mkJob({ bytes_done: 1500, bytes_total: 1000 }))).toBe(100)
    expect(deriveInstallPercent(mkJob({ bytes_done: -10, bytes_total: 1000 }))).toBe(0)
  })
})
