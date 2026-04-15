/**
 * B11 — ForecastPanel component tests.
 *
 * Validates:
 *   1. Initial render fetches forecast and displays KPI cards.
 *   2. RECOMPUTE button triggers a POST refetch.
 *   3. omnisight:spec-updated event triggers a debounced recompute.
 *   4. Delta banner shows when estimate changes after spec update.
 *   5. Delta banner is dismissable.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { render, screen, waitFor, act } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/components/omnisight/panel-help", () => ({
  PanelHelp: () => null,
}))

const baseForecast = {
  project_name: "test-project",
  target_platform: "host_native",
  project_track: "firmware",
  tasks: { total: 74, by_phase: { concept: 4, spec: 6, sample: 12, ev: 14, dvt: 16, pvt: 12, mp: 6, sustaining: 4 }, by_track: "firmware" },
  agents: { total: 4, by_type: ["firmware", "validator", "reviewer", "reporter"] },
  duration: { total_hours: 22.2, optimistic_hours: 17.3, pessimistic_hours: 30.3 },
  tokens: { total: 555000, by_tier: { premium: 55500, default: 388500, budget: 111000 } },
  cost: { total_usd: 0.42, provider: "anthropic", by_tier_usd: { premium: 0.2, default: 0.15, budget: 0.07 } },
  confidence: 0.5,
  method: "template",
  profile_sensitivity: [
    { profile: "STRICT", hours: 28.9, multiplier: 1.3 },
    { profile: "BALANCED", hours: 22.2, multiplier: 1.0 },
    { profile: "AUTONOMOUS", hours: 17.3, multiplier: 0.78 },
    { profile: "GHOST", hours: 14.4, multiplier: 0.65 },
  ],
  generated_at: Date.now() / 1000,
}

const updatedForecast = {
  ...baseForecast,
  target_platform: "aarch64",
  duration: { total_hours: 26.6, optimistic_hours: 19.7, pessimistic_hours: 36.3 },
  tokens: { total: 666000, by_tier: { premium: 66600, default: 466200, budget: 133200 } },
  profile_sensitivity: [
    { profile: "STRICT", hours: 34.6, multiplier: 1.3 },
    { profile: "BALANCED", hours: 26.6, multiplier: 1.0 },
    { profile: "AUTONOMOUS", hours: 20.7, multiplier: 0.78 },
    { profile: "GHOST", hours: 17.3, multiplier: 0.65 },
  ],
  generated_at: Date.now() / 1000 + 1,
}

let fetchCallCount = 0

describe("ForecastPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ shouldAdvanceTime: true })
    fetchCallCount = 0

    global.fetch = vi.fn(async (url: string | URL | Request, _opts?: RequestInit) => {
      fetchCallCount++
      const path = typeof url === "string" ? url : url.toString()
      if (path.includes("/forecast")) {
        const body = fetchCallCount <= 1 ? baseForecast : updatedForecast
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      }
      return new Response("Not found", { status: 404 })
    }) as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it("renders KPI cards after initial fetch", async () => {
    const { ForecastPanel } = await import("@/components/omnisight/forecast-panel")
    await act(async () => {
      render(<ForecastPanel />)
    })

    await waitFor(() => {
      expect(screen.getByText("74")).toBeInTheDocument()
    })
    expect(screen.getByText("22.2h")).toBeInTheDocument()
    expect(screen.getByText("50%")).toBeInTheDocument()
    expect(screen.getByText("template")).toBeInTheDocument()
  })

  it("RECOMPUTE button triggers POST refetch", async () => {
    const { ForecastPanel } = await import("@/components/omnisight/forecast-panel")
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await act(async () => {
      render(<ForecastPanel />)
    })

    await waitFor(() => expect(screen.getByText("74")).toBeInTheDocument())

    const btn = screen.getByRole("button", { name: /recompute forecast/i })
    await user.click(btn)

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/forecast/recompute"),
        expect.objectContaining({ method: "POST" }),
      )
    })
  })

  it("spec-updated event triggers debounced recompute and shows delta", async () => {
    const { ForecastPanel } = await import("@/components/omnisight/forecast-panel")
    await act(async () => {
      render(<ForecastPanel />)
    })

    await waitFor(() => expect(screen.getByText("22.2h")).toBeInTheDocument())

    await act(async () => {
      window.dispatchEvent(
        new CustomEvent("omnisight:spec-updated", {
          detail: {
            spec: {
              target_arch: { value: "arm64", confidence: 0.9 },
              framework: { value: "embedded", confidence: 0.9 },
              hardware_required: { value: "yes", confidence: 0.9 },
              target_os: { value: "linux", confidence: 0.9 },
              project_type: { value: "embedded_firmware", confidence: 0.9 },
            },
          },
        }),
      )
      vi.advanceTimersByTime(900)
    })

    await waitFor(() => {
      expect(screen.getByText("26.6h")).toBeInTheDocument()
    }, { timeout: 3000 })

    await waitFor(() => {
      const deltaEl = screen.getByRole("status", { name: /forecast delta/i })
      expect(deltaEl).toBeInTheDocument()
      expect(deltaEl.textContent).toContain("+4.4h")
      expect(deltaEl.textContent).toContain("+111.0k tok")
    })
  })

  it("delta banner is dismissable", async () => {
    const { ForecastPanel } = await import("@/components/omnisight/forecast-panel")
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await act(async () => {
      render(<ForecastPanel />)
    })

    await waitFor(() => expect(screen.getByText("22.2h")).toBeInTheDocument())

    await act(async () => {
      window.dispatchEvent(
        new CustomEvent("omnisight:spec-updated", {
          detail: {
            spec: {
              target_arch: { value: "arm64", confidence: 0.9 },
              framework: { value: "embedded", confidence: 0.9 },
              hardware_required: { value: "yes", confidence: 0.9 },
              target_os: { value: "linux", confidence: 0.9 },
              project_type: { value: "embedded_firmware", confidence: 0.9 },
            },
          },
        }),
      )
      vi.advanceTimersByTime(900)
    })

    await waitFor(() => {
      expect(screen.getByRole("status", { name: /forecast delta/i })).toBeInTheDocument()
    }, { timeout: 3000 })

    const dismiss = screen.getByRole("button", { name: /dismiss delta/i })
    await user.click(dismiss)

    expect(screen.queryByRole("status", { name: /forecast delta/i })).not.toBeInTheDocument()
  })

  it("ignores spec-updated when arch and framework are unknown", async () => {
    const { ForecastPanel } = await import("@/components/omnisight/forecast-panel")
    await act(async () => {
      render(<ForecastPanel />)
    })

    await waitFor(() => expect(screen.getByText("22.2h")).toBeInTheDocument())

    const callsBefore = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.length

    await act(async () => {
      window.dispatchEvent(
        new CustomEvent("omnisight:spec-updated", {
          detail: {
            spec: {
              target_arch: { value: "unknown", confidence: 0 },
              framework: { value: "unknown", confidence: 0 },
              hardware_required: { value: "unknown", confidence: 0 },
              target_os: { value: "unknown", confidence: 0 },
              project_type: { value: "unknown", confidence: 0 },
            },
          },
        }),
      )
      vi.advanceTimersByTime(1000)
    })

    expect((global.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore)
  })
})
