/**
 * Z.4 #293 checkbox 5 — useEngine 60 s provider-balance poll contract.
 *
 * The hook polls ``GET /runtime/providers/balance`` on a dedicated 60 s
 * interval decoupled from the 10 s dashboard-summary tick (balance is
 * low-frequency data — ``llm_balance_refresher`` writes the cache every
 * 10 min — so the shorter dashboard cadence would waste six out of
 * seven requests per minute). These tests lock:
 *
 *   1. Initial state before the first poll lands is ``null`` (so the
 *      downstream ``<ProviderStatusBadge>`` can render its gray
 *      "loading" tier instead of a misleading green).
 *   2. First tick populates ``providerBalances`` with the envelope
 *      array the endpoint returned, verbatim.
 *   3. Subsequent ticks fire every 60 s — specifically the second
 *      fetch lands after ``advanceTimersByTime(60_000)``.
 *   4. A transient endpoint failure keeps the last-known envelope
 *      array rather than clobbering it with ``null`` (flicker-resistance
 *      matches the same "stale cache" rule the backend uses for the
 *      provider 5xx case).
 *   5. The poll is NOT wired into the 10 s dashboard-summary tick —
 *      advancing by 10 s does NOT trigger a second fetch.
 *   6. The interval is cleared on unmount (no leaked timer).
 *
 * Fake-timer pattern mirrors ``decision-dashboard.test.tsx``: vitest 4
 * has a known race where testing-library's ``waitFor`` polls via real
 * timers while the hook's ``setInterval`` runs on fake timers; we pin
 * the clock with ``shouldAdvanceTime: true`` so pending microtasks
 * (mocked fetch promises) resolve, then advance synchronously.
 */
import { renderHook, act } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => {
  const reject = () => Promise.reject(new Error("offline-mock"))
  return {
    listAgents: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    listTasks: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    getSystemStatus: reject,
    getSystemInfo: reject,
    getDevices: reject,
    getSpec: reject,
    getRepos: reject,
    getLogs: reject,
    getTokenUsage: reject,
    getTokenBudget: reject,
    getUnreadCount: reject,
    getCompressionStats: reject,
    listSimulations: reject,
    getNPIState: reject,
    createAgent: vi.fn(),
    deleteAgent: vi.fn(),
    updateAgentStatus: vi.fn(),
    assignTask: vi.fn(),
    completeTask: vi.fn(),
    forceAssign: vi.fn(),
    sendChatMessage: vi.fn(),
    invoke: vi.fn(),
    subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
    getChatHistory: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    getDashboardSummary: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    getProvidersBalance: vi.fn(),
  }
})

import * as api from "@/lib/api"
import { useEngine } from "@/hooks/use-engine"

beforeEach(() => {
  // Reset every mock's call history + implementation between tests so the
  // 60 s interval assertions are isolated. ``vi.restoreAllMocks()`` in
  // setup.ts afterEach clears implementations but mockImplementation set
  // from the previous test can still leak into the next describe block
  // when mocks are defined inside a factory; explicitly clearing here
  // is the belt-and-suspenders fix.
  ;(api.getProvidersBalance as ReturnType<typeof vi.fn>).mockReset()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function okBatch() {
  return {
    providers: [
      {
        status: "ok" as const,
        provider: "deepseek",
        currency: "CNY",
        balance_remaining: 80.0,
        granted_total: 100.0,
        usage_total: 20.0,
        last_refreshed_at: 1_714_000_000.0,
        source: "cache" as const,
        raw: {},
        stale_since: null,
      },
      {
        status: "unsupported" as const,
        provider: "anthropic",
        reason: "provider does not expose a public balance API with API-key authentication",
      },
    ],
  }
}

/** Drain pending microtasks a few times so that
 *  ``await api.getProvidersBalance()``'s mocked-resolved promise +
 *  React's subsequent commit both settle before the next assertion.
 *  vitest's fake-timer scheduler does not auto-drain microtasks; wrap
 *  in ``act`` so React's internal commit queue flushes too. */
async function flushPromises() {
  await act(async () => {
    for (let i = 0; i < 5; i++) {
      await Promise.resolve()
    }
  })
}

describe("useEngine — provider-balance 60 s poll", () => {
  it("starts with providerBalances === null before the first fetch lands", () => {
    // Fetch hangs forever so the initial state stays pre-poll.
    ;(api.getProvidersBalance as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(() => {}),
    )
    const { result } = renderHook(() => useEngine())
    expect(result.current.providerBalances).toBeNull()
  })

  it("populates providerBalances from the first fetch", async () => {
    ;(api.getProvidersBalance as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve(okBatch()),
    )
    const { result } = renderHook(() => useEngine())
    await flushPromises()
    expect(result.current.providerBalances).not.toBeNull()
    expect(result.current.providerBalances).toHaveLength(2)
    const deepseek = result.current.providerBalances?.find(
      (e) => e.provider === "deepseek",
    )
    expect(deepseek?.status).toBe("ok")
    expect(deepseek?.balance_remaining).toBe(80.0)
    expect(deepseek?.currency).toBe("CNY")
    const anthropic = result.current.providerBalances?.find(
      (e) => e.provider === "anthropic",
    )
    expect(anthropic?.status).toBe("unsupported")
  })

  it("re-polls every 60 s (second fetch lands after the interval elapses)", async () => {
    const fetch = api.getProvidersBalance as ReturnType<typeof vi.fn>
    fetch.mockImplementation(() => Promise.resolve(okBatch()))
    renderHook(() => useEngine())
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
    // Advance exactly one interval — the second call must fire.
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(2)
    // Another interval — third call fires.
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(3)
  })

  it("does NOT piggy-back on the 10 s dashboard-summary tick", async () => {
    // The spec explicitly asks for a dedicated 60 s poll decoupled
    // from the 10 s dashboard cadence ("5s 太密，單獨一個 60s 輪詢就夠,
    // 這是低頻資料"). Advancing by 50 s must NOT trigger a second
    // balance fetch — if the poll was accidentally mounted on the
    // 10 s tick we'd see 5 extra calls.
    const fetch = api.getProvidersBalance as ReturnType<typeof vi.fn>
    fetch.mockImplementation(() => Promise.resolve(okBatch()))
    renderHook(() => useEngine())
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
    await act(async () => {
      vi.advanceTimersByTime(50_000)
    })
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("keeps last-known envelopes when a subsequent fetch fails (no flicker)", async () => {
    const fetch = api.getProvidersBalance as ReturnType<typeof vi.fn>
    fetch.mockImplementationOnce(() => Promise.resolve(okBatch()))
    fetch.mockImplementation(() => Promise.reject(new Error("transient 502")))
    const { result } = renderHook(() => useEngine())
    await flushPromises()
    expect(result.current.providerBalances).not.toBeNull()
    const snapshot = result.current.providerBalances
    // Second tick — the mock rejects. The state must NOT flip to null;
    // operators should keep seeing the last-known badge tier until
    // the next successful refresh.
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(result.current.providerBalances).toEqual(snapshot)
  })

  it("stops polling after the hook unmounts (no leaked interval)", async () => {
    const fetch = api.getProvidersBalance as ReturnType<typeof vi.fn>
    fetch.mockImplementation(() => Promise.resolve(okBatch()))
    const { unmount } = renderHook(() => useEngine())
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
    unmount()
    await act(async () => {
      vi.advanceTimersByTime(180_000)
    })
    await flushPromises()
    // Three intervals after unmount — the count must stay at 1 (the
    // initial fetch). A leaked interval would bump this to 4.
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
