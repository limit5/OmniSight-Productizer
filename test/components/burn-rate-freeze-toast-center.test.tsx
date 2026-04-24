/**
 * ZZ.B3 #304-3 checkbox 3 (2026-04-24) — BurnRateFreezeToastCenter tests.
 *
 * Covers the four contract axes for the "linear-extrapolation freeze ETA"
 * warning toast:
 *   (a) TRIGGER — projected daily burn (cost_per_hour × 24) > budget fires
 *       the toast with a localtime "HH:MM" ETA in the spec's exact prose
 *       「目前速率將於 HH:MM 觸發 freeze」.
 *   (b) NO-TRIGGER — sustainable rate / unlimited budget / already-frozen /
 *       zero burn rate / zero remaining — each must NOT show a toast so
 *       operators aren't drowned in noise when the state is benign.
 *   (c) DEDUPE — once the toast is up, subsequent polls with an ETA inside
 *       the 5-minute deadband must not re-render the toast (shifts the
 *       same minute value would otherwise re-toast once per poll).
 *   (d) DISMISS + RE-EMIT — the X button clears the toast; the next poll
 *       must not re-show it while the ETA is within deadband, BUT a
 *       significant ETA shift (beyond deadband) is allowed to re-emit
 *       so operators still see meaningful new projections.
 *
 * The unit-level ``computeFreezeEta`` helper is also tested separately so
 * the trigger math is locked without render overhead — all threshold
 * edge cases come through the pure function.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getTokenBudget: vi.fn(),
    fetchTokenBurnRate: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  BurnRateFreezeToastCenter,
  computeFreezeEta,
} from "@/components/omnisight/burn-rate-freeze-toast-center"

function makeBudget(
  overrides: Partial<api.TokenBudgetInfo> = {},
): api.TokenBudgetInfo {
  return {
    budget: 10,
    usage: 2,
    ratio: 0.2,
    frozen: false,
    level: "normal",
    warn_threshold: 0.8,
    downgrade_threshold: 0.9,
    freeze_threshold: 1.0,
    fallback_provider: "",
    fallback_model: "",
    ...overrides,
  }
}

function makeBurnResponse(
  costPerHour: number,
): api.TokenBurnRateResponse {
  return {
    window: "1h",
    bucket_seconds: 60,
    points:
      costPerHour > 0
        ? [
            {
              timestamp: "2026-04-24T12:00:00Z",
              tokens_per_hour: 100_000,
              cost_per_hour: costPerHour,
            },
          ]
        : [],
  }
}

beforeEach(() => {
  ;(api.getTokenBudget as ReturnType<typeof vi.fn>).mockReset()
  ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe("computeFreezeEta (trigger math)", () => {
  // Using a fixed now so HH:MM is deterministic. 2026-04-24T09:00:00Z =
  // different wall-clock hour in each local tz, so we don't assert on
  // the exact label here — the render tests below do that with a
  // locale-agnostic regex.
  const NOW = Date.UTC(2026, 3, 24, 9, 0, 0)

  it("triggers when projectedDaily exceeds budget (5/hr × 24 = 120 > 10)", () => {
    const res = computeFreezeEta(
      makeBudget({ budget: 10, usage: 2 }),
      [
        {
          timestamp: "2026-04-24T08:59:00Z",
          tokens_per_hour: 100_000,
          cost_per_hour: 5,
        },
      ],
      NOW,
    )
    expect(res).not.toBeNull()
    // remaining = 10 - 2 = 8; hours_to_freeze = 8 / 5 = 1.6 h
    expect(res!.remaining).toBe(8)
    expect(res!.costPerHour).toBe(5)
    expect(res!.projectedDaily).toBe(120)
    // etaMs = NOW + 1.6 h
    expect(res!.etaMs - NOW).toBeCloseTo(1.6 * 3_600_000, -2)
  })

  it("does not trigger when projectedDaily ≤ budget (sustainable rate)", () => {
    // 0.4/hr × 24 = 9.6 ≤ 10
    expect(
      computeFreezeEta(
        makeBudget({ budget: 10, usage: 0 }),
        [
          {
            timestamp: "2026-04-24T08:59:00Z",
            tokens_per_hour: 10_000,
            cost_per_hour: 0.4,
          },
        ],
        NOW,
      ),
    ).toBeNull()
  })

  it("does not trigger when budget is unlimited (budget <= 0)", () => {
    expect(
      computeFreezeEta(
        makeBudget({ budget: 0 }),
        [
          {
            timestamp: "2026-04-24T08:59:00Z",
            tokens_per_hour: 1_000_000,
            cost_per_hour: 100,
          },
        ],
        NOW,
      ),
    ).toBeNull()
  })

  it("does not trigger when already frozen (backend has cut us off)", () => {
    expect(
      computeFreezeEta(
        makeBudget({ frozen: true }),
        [
          {
            timestamp: "2026-04-24T08:59:00Z",
            tokens_per_hour: 1_000_000,
            cost_per_hour: 100,
          },
        ],
        NOW,
      ),
    ).toBeNull()
  })

  it("does not trigger when remaining <= 0 (budget spent but not flagged frozen yet)", () => {
    expect(
      computeFreezeEta(
        makeBudget({ budget: 10, usage: 10 }),
        [
          {
            timestamp: "2026-04-24T08:59:00Z",
            tokens_per_hour: 1_000_000,
            cost_per_hour: 100,
          },
        ],
        NOW,
      ),
    ).toBeNull()
  })

  it("does not trigger when burn rate is zero (no recent turns)", () => {
    expect(computeFreezeEta(makeBudget(), [], NOW)).toBeNull()
    expect(
      computeFreezeEta(
        makeBudget(),
        [
          {
            timestamp: "2026-04-24T08:59:00Z",
            tokens_per_hour: 0,
            cost_per_hour: 0,
          },
        ],
        NOW,
      ),
    ).toBeNull()
  })

  it("does not trigger when budget is null (endpoint down / first boot)", () => {
    expect(
      computeFreezeEta(
        null,
        [
          {
            timestamp: "2026-04-24T08:59:00Z",
            tokens_per_hour: 1_000_000,
            cost_per_hour: 100,
          },
        ],
        NOW,
      ),
    ).toBeNull()
  })

  it("formats etaLabel as 24-hour HH:MM (locale-stable pattern)", () => {
    const res = computeFreezeEta(
      makeBudget({ budget: 10, usage: 2 }),
      [
        {
          timestamp: "2026-04-24T08:59:00Z",
          tokens_per_hour: 100_000,
          cost_per_hour: 5,
        },
      ],
      NOW,
    )
    expect(res).not.toBeNull()
    // ``HH:MM`` — 2 digits, colon, 2 digits. The exact hour depends on
    // the test runner's local TZ, so we lock the shape not the value.
    expect(res!.etaLabel).toMatch(/^\d{2}:\d{2}$/)
  })
})

describe("BurnRateFreezeToastCenter — render", () => {
  it("renders nothing when the rate is sustainable", async () => {
    ;(api.getTokenBudget as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBudget({ budget: 100, usage: 0 }),
    )
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBurnResponse(1), // 1/hr × 24 = 24 ≤ 100
    )
    render(<BurnRateFreezeToastCenter pollMs={100_000} />)
    // Let the mount-time tick resolve.
    await waitFor(() => {
      expect(api.getTokenBudget).toHaveBeenCalled()
    })
    expect(
      screen.queryByTestId("burn-rate-freeze-toast"),
    ).not.toBeInTheDocument()
  })

  it("renders the 「目前速率將於 HH:MM 觸發 freeze」 toast when burn × 24 > budget", async () => {
    ;(api.getTokenBudget as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBudget({ budget: 10, usage: 2 }),
    )
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBurnResponse(5), // 5/hr × 24 = 120 > 10 → trigger
    )
    render(<BurnRateFreezeToastCenter pollMs={100_000} />)
    const toast = await screen.findByTestId("burn-rate-freeze-toast")
    expect(toast).toBeInTheDocument()
    // Message reads「目前速率將於 HH:MM 觸發 freeze」 — locale-agnostic
    // regex anchors the exact prose and the HH:MM shape.
    const msg = await screen.findByTestId("burn-rate-freeze-message")
    expect(msg.textContent).toMatch(/^目前速率將於 \d{2}:\d{2} 觸發 freeze$/)
    // Rate badge inside the toast mirrors the spec's $x.xx/hr format.
    expect(screen.getByTestId("burn-rate-freeze-rate").textContent).toBe(
      "$5.00/hr",
    )
  })

  it("hides the toast once the backend reports frozen=true (warning is moot)", async () => {
    const getBudget = api.getTokenBudget as ReturnType<typeof vi.fn>
    const getBurn = api.fetchTokenBurnRate as ReturnType<typeof vi.fn>
    getBudget.mockResolvedValueOnce(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValueOnce(makeBurnResponse(5))
    // Second poll: frozen.
    getBudget.mockResolvedValueOnce(
      makeBudget({ budget: 10, usage: 10, frozen: true, level: "frozen" }),
    )
    getBurn.mockResolvedValueOnce(makeBurnResponse(5))

    const pollMs = 30
    render(<BurnRateFreezeToastCenter pollMs={pollMs} />)
    await screen.findByTestId("burn-rate-freeze-toast")

    await waitFor(
      () => {
        expect(
          screen.queryByTestId("burn-rate-freeze-toast"),
        ).not.toBeInTheDocument()
      },
      { timeout: 1000 },
    )
  })

  it("does not re-render when subsequent polls land in the 5-minute ETA deadband", async () => {
    const getBudget = api.getTokenBudget as ReturnType<typeof vi.fn>
    const getBurn = api.fetchTokenBurnRate as ReturnType<typeof vi.fn>
    // First poll: 5/hr → eta1
    getBudget.mockResolvedValueOnce(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValueOnce(makeBurnResponse(5))
    // Second poll: 5.05/hr → ~same eta (drift well under 5 min). The
    // remaining=8, so hours 8/5=1.6 vs 8/5.05≈1.584 ≈ 58 s drift.
    getBudget.mockResolvedValueOnce(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValueOnce(makeBurnResponse(5.05))

    const pollMs = 30
    render(<BurnRateFreezeToastCenter pollMs={pollMs} />)
    const first = await screen.findByTestId("burn-rate-freeze-toast")
    const firstEta = first.getAttribute("data-eta-ms")
    expect(firstEta).not.toBeNull()

    // Wait a few polls and confirm the ETA attribute is unchanged —
    // the deadband kept the same toast instance.
    await new Promise((r) => setTimeout(r, 200))
    const latest = screen.getByTestId("burn-rate-freeze-toast")
    expect(latest.getAttribute("data-eta-ms")).toBe(firstEta)
  })

  it("dismiss button hides the toast; re-emission requires a >5 min ETA shift", async () => {
    const user = userEvent.setup()
    const getBudget = api.getTokenBudget as ReturnType<typeof vi.fn>
    const getBurn = api.fetchTokenBurnRate as ReturnType<typeof vi.fn>
    // Poll 1: eta1 (5/hr, remaining 8 → 1.6h)
    getBudget.mockResolvedValueOnce(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValueOnce(makeBurnResponse(5))
    // Poll 2: tiny drift, still within deadband — dismissal should
    // continue to suppress the toast.
    getBudget.mockResolvedValueOnce(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValueOnce(makeBurnResponse(5.02))
    // Poll 3: burn rate doubled → eta halves → deadband broken → re-emit.
    // 10/hr, remaining 8 → 0.8h → shift from 1.6h to 0.8h = 48 min >> 5 min.
    getBudget.mockResolvedValueOnce(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValueOnce(makeBurnResponse(10))
    // Any further polls after these three return the same "dramatic
    // shift" state so the toast stays up deterministically.
    getBudget.mockResolvedValue(makeBudget({ budget: 10, usage: 2 }))
    getBurn.mockResolvedValue(makeBurnResponse(10))

    const pollMs = 30
    render(<BurnRateFreezeToastCenter pollMs={pollMs} />)
    await screen.findByTestId("burn-rate-freeze-toast")

    await user.click(screen.getByTestId("burn-rate-freeze-toast-dismiss"))
    expect(
      screen.queryByTestId("burn-rate-freeze-toast"),
    ).not.toBeInTheDocument()

    // After the deadband-breaking poll, the toast comes back.
    await waitFor(
      () => {
        expect(
          screen.queryByTestId("burn-rate-freeze-toast"),
        ).toBeInTheDocument()
      },
      { timeout: 2000 },
    )
  })

  it("stays silent when getTokenBudget throws (endpoint flaky)", async () => {
    ;(api.getTokenBudget as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("flake"),
    )
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBurnResponse(5),
    )
    render(<BurnRateFreezeToastCenter pollMs={100_000} />)
    await waitFor(() => {
      expect(api.getTokenBudget).toHaveBeenCalled()
    })
    expect(
      screen.queryByTestId("burn-rate-freeze-toast"),
    ).not.toBeInTheDocument()
  })

  it("clears polling on unmount (no leak)", async () => {
    ;(api.getTokenBudget as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBudget({ budget: 10, usage: 2 }),
    )
    ;(api.fetchTokenBurnRate as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeBurnResponse(5),
    )
    const { unmount } = render(
      <BurnRateFreezeToastCenter pollMs={100_000} />,
    )
    await screen.findByTestId("burn-rate-freeze-toast")

    const callsBefore = (api.getTokenBudget as ReturnType<typeof vi.fn>).mock
      .calls.length
    act(() => {
      unmount()
    })
    // Give any stray intervals time to fire.
    await new Promise((r) => setTimeout(r, 150))
    const callsAfter = (api.getTokenBudget as ReturnType<typeof vi.fn>).mock
      .calls.length
    expect(callsAfter).toBe(callsBefore)
  })
})
