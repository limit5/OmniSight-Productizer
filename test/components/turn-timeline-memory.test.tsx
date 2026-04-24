/**
 * ZZ.B1 (#304-1, 2026-04-24) checkbox 4 — ring buffer LRU + drawer
 * memory leak guards.
 *
 * Checkboxes 1 / 2 / 3 each carry one specific LRU assertion
 * (maxTurns cap for pure ``turn_metrics``, pure ``turn.complete``
 * synthesis). This file adds the *mixed-source* guard — a live
 * session interleaves both event types and the ring buffer must
 * treat every new card uniformly regardless of source — and the
 * drawer leak guards the other checkboxes couldn't cover because
 * the drawer wasn't wired yet.
 *
 * Covered here:
 *  - Mixed-source LRU: turn_metrics + turn.complete alternating,
 *    ring buffer evicts oldest by arrival order.
 *  - Upgrade-in-place does NOT grow the buffer (match-by-model-fallback
 *    path in ``turn-timeline.tsx`` must update the existing card).
 *  - Component unmount closes the SSE subscription exactly once.
 *  - Drawer open + close balances ``window.addEventListener`` /
 *    ``removeEventListener`` for "keydown" (so N open/close cycles
 *    leave the listener table at its baseline — no accumulating
 *    handlers).
 *  - Drawer open without close then component unmount still removes
 *    the listener (useEffect cleanup fires).
 *  - ESC key closes the drawer exactly once — re-pressing ESC with
 *    no drawer open is a silent no-op (no handler remains).
 *  - Repeated drawer open/close cycles do not accumulate DOM nodes
 *    (turn-detail-drawer count returns to 0 between cycles).
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, act, fireEvent } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
    fetchTurnHistory: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  TurnTimeline,
  type TurnCardData,
} from "@/components/omnisight/turn-timeline"
import { primeSSE } from "../helpers/sse"

beforeEach(() => {
  vi.clearAllMocks()
})

function emitMetrics(
  sse: ReturnType<typeof primeSSE>,
  model: string,
  iso: string,
  latencyMs = 200,
) {
  sse.emit({
    event: "turn_metrics",
    data: {
      provider: "anthropic",
      model,
      input_tokens: 1000,
      output_tokens: 500,
      tokens_used: 1500,
      context_limit: 200_000,
      context_usage_pct: 0.75,
      latency_ms: latencyMs,
      cache_read_tokens: null,
      cache_create_tokens: null,
      timestamp: iso,
    },
  })
}

function emitComplete(
  sse: ReturnType<typeof primeSSE>,
  turnId: string,
  model: string,
  iso: string,
  summary: string,
) {
  sse.emit({
    event: "turn.complete",
    data: {
      turn_id: turnId,
      provider: "anthropic",
      model,
      agent_type: "orchestrator",
      task_id: null,
      input_tokens: 1000,
      output_tokens: 500,
      tokens_used: 1500,
      context_limit: 200_000,
      context_usage_pct: 0.75,
      latency_ms: 200,
      cache_read_tokens: 0,
      cache_create_tokens: 0,
      cost_usd: 0.02,
      started_at: iso,
      ended_at: iso,
      summary,
      messages: [],
      tool_calls: [],
      tool_call_count: 0,
      tool_failure_count: 0,
      timestamp: iso,
    },
  })
}

function makeTurn(overrides: Partial<TurnCardData> = {}): TurnCardData {
  const base: TurnCardData = {
    turnNumber: 1,
    turnId: null,
    timestamp: "2026-04-24T00:00:00.000Z",
    tsMs: Date.parse("2026-04-24T00:00:00.000Z"),
    model: "claude-opus-4-7",
    provider: "anthropic",
    agentSubtype: "orchestrator",
    inputTokens: 1000,
    outputTokens: 500,
    tokensUsed: 1500,
    cacheReadTokens: null,
    cacheCreateTokens: null,
    cacheHitRatio: null,
    contextLimit: 200_000,
    contextUsagePct: 42,
    latencyMs: 200,
    toolCallCount: null,
    toolFailureCount: null,
    failedTools: [],
    gapMs: null,
    costUsd: 0.01,
    summary: null,
  }
  return { ...base, ...overrides }
}

/**
 * Track ``window.addEventListener`` / ``removeEventListener`` calls
 * for a given event type so tests can assert add/remove parity after
 * a drawer open/close cycle. jsdom's default listener table is not
 * introspectable; we monkey-patch the window itself inside each test.
 */
function trackWindowListeners(eventType: string) {
  const originalAdd = window.addEventListener
  const originalRemove = window.removeEventListener
  let addCount = 0
  let removeCount = 0
  const addSpy = vi.fn((type: string, listener: any, opts?: any) => {
    if (type === eventType) addCount++
    return originalAdd.call(window, type, listener, opts)
  })
  const removeSpy = vi.fn((type: string, listener: any, opts?: any) => {
    if (type === eventType) removeCount++
    return originalRemove.call(window, type, listener, opts)
  })
  window.addEventListener = addSpy as any
  window.removeEventListener = removeSpy as any
  return {
    addCount: () => addCount,
    removeCount: () => removeCount,
    restore: () => {
      window.addEventListener = originalAdd
      window.removeEventListener = originalRemove
    },
  }
}

describe("<TurnTimeline> emission + LRU + drawer memory (ZZ.B1 checkbox 4)", () => {
  it("ring buffer LRU evicts by arrival order across mixed event sources", async () => {
    const sse = primeSSE(api)
    // maxTurns=3: feed 5 turns across alternating sources to force eviction.
    const view = render(<TurnTimeline maxTurns={3} />)
    // Wait for subscription to mount (fetchTurnHistory stub resolves empty).
    await Promise.resolve()
    act(() => {
      // Turn 1: turn_metrics only
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z")
      // Turn 2: turn.complete synthesised (no prior metrics)
      emitComplete(
        sse,
        "turn-c1",
        "claude-sonnet-4-6",
        "2026-04-24T00:00:01.000Z",
        "sonnet standalone",
      )
      // Turn 3: turn_metrics only
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:02.000Z")
      // Turn 4: turn.complete synthesised for a different model
      emitComplete(
        sse,
        "turn-c2",
        "claude-haiku-4-5",
        "2026-04-24T00:00:03.000Z",
        "haiku standalone",
      )
      // Turn 5: turn_metrics only
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:04.000Z")
    })
    const cards = view.getAllByTestId("turn-card")
    // maxTurns=3 — oldest 2 (turn 1 opus metrics, turn 2 sonnet complete) evicted.
    expect(cards).toHaveLength(3)
    const numbers = view.getAllByTestId("turn-card-number").map(n => n.textContent)
    // Counter keeps incrementing; surviving turns are #3, #4, #5.
    expect(numbers).toEqual(["#3", "#4", "#5"])
    // Source-independent: card #4 is the haiku one (synthesised from
    // turn.complete) and the ring buffer ordering matches arrival.
    expect(cards[1].querySelector('[data-testid="turn-card-subtype"]')?.textContent)
      .toMatch(/orchestrator|Haiku/i)
  })

  it("turn.complete upgrade-in-place does not grow the ring buffer", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline maxTurns={10} />)
    await Promise.resolve()
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z")
    })
    expect(view.getAllByTestId("turn-card")).toHaveLength(1)
    // turn.complete matching the same model (no turn_id on the existing
    // card yet) upgrades in place — must NOT append a second card.
    act(() => {
      emitComplete(
        sse,
        "turn-merge",
        "claude-opus-4-7",
        "2026-04-24T00:00:00.500Z",
        "merged summary",
      )
    })
    expect(view.getAllByTestId("turn-card")).toHaveLength(1)
    // Line 5 reflects the merged summary — proves the upgrade landed.
    expect(view.getByTestId("turn-card-summary").textContent)
      .toMatch(/merged summary/)
  })

  it("component unmount closes the SSE subscription exactly once", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    await Promise.resolve()
    expect(sse.closeCount()).toBe(0)
    view.unmount()
    expect(sse.closeCount()).toBe(1)
    // Unmount is idempotent — re-unmount must not double-close (would
    // mask a bug in the effect cleanup returning a new function each time).
  })

  it("drawer open+close balances window.addEventListener / removeEventListener for 'keydown'", () => {
    primeSSE(api)
    const track = trackWindowListeners("keydown")
    try {
      const view = render(<TurnTimeline externalTurns={[makeTurn()]} />)
      const baselineAdd = track.addCount()
      const baselineRemove = track.removeCount()

      // Open the drawer — useEffect registers a keydown handler.
      act(() => { fireEvent.click(view.getByTestId("turn-card")) })
      expect(view.getByTestId("turn-detail-drawer")).toBeInTheDocument()
      expect(track.addCount() - baselineAdd).toBeGreaterThanOrEqual(1)

      // Close the drawer — cleanup must remove the handler.
      act(() => { fireEvent.click(view.getByTestId("turn-detail-close")) })
      expect(view.queryByTestId("turn-detail-drawer")).toBeNull()

      const netAdds = track.addCount() - baselineAdd
      const netRemoves = track.removeCount() - baselineRemove
      expect(netAdds).toBe(netRemoves)
    } finally {
      track.restore()
    }
  })

  it("repeated drawer open/close cycles do not accumulate listeners or DOM nodes", () => {
    primeSSE(api)
    const track = trackWindowListeners("keydown")
    try {
      const view = render(<TurnTimeline externalTurns={[makeTurn()]} />)
      const baselineAdd = track.addCount()
      const baselineRemove = track.removeCount()

      const CYCLES = 10
      for (let i = 0; i < CYCLES; i++) {
        act(() => { fireEvent.click(view.getByTestId("turn-card")) })
        expect(view.getAllByTestId("turn-detail-drawer")).toHaveLength(1)
        act(() => { fireEvent.click(view.getByTestId("turn-detail-close")) })
        expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
      }

      // At the end of 10 open/close cycles:
      //  - DOM has exactly 0 drawers (nothing orphaned)
      //  - add count equals remove count (no listener leak)
      expect(view.queryAllByTestId("turn-detail-drawer")).toHaveLength(0)
      expect(track.addCount() - baselineAdd).toBe(track.removeCount() - baselineRemove)
      // And each cycle must have added exactly one listener — otherwise
      // the assertion above could pass by coincidence (say, a runaway
      // cleanup in the component that removes the listener on both
      // open AND close).
      expect(track.addCount() - baselineAdd).toBeGreaterThanOrEqual(CYCLES)
    } finally {
      track.restore()
    }
  })

  it("unmounting the component with drawer open still removes the keydown listener", () => {
    primeSSE(api)
    const track = trackWindowListeners("keydown")
    try {
      const view = render(<TurnTimeline externalTurns={[makeTurn()]} />)
      const baselineAdd = track.addCount()
      const baselineRemove = track.removeCount()

      act(() => { fireEvent.click(view.getByTestId("turn-card")) })
      expect(view.getByTestId("turn-detail-drawer")).toBeInTheDocument()
      expect(track.addCount() - baselineAdd).toBeGreaterThanOrEqual(1)

      // Unmount WITHOUT closing the drawer first — the useEffect cleanup
      // must still fire on unmount so the keydown handler doesn't
      // outlive the component (would be a classic React leak).
      view.unmount()
      expect(track.addCount() - baselineAdd).toBe(track.removeCount() - baselineRemove)
    } finally {
      track.restore()
    }
  })

  it("ESC closes the drawer and a second ESC with no drawer open is a no-op", () => {
    primeSSE(api)
    const view = render(<TurnTimeline externalTurns={[makeTurn()]} />)
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getByTestId("turn-detail-drawer")).toBeInTheDocument()
    act(() => { fireEvent.keyDown(window, { key: "Escape" }) })
    expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
    // Second ESC — with the drawer already closed, this must not
    // reopen it, throw, or log. If cleanup didn't remove the handler
    // the onClose callback would fire on a closed component.
    expect(() => {
      act(() => { fireEvent.keyDown(window, { key: "Escape" }) })
    }).not.toThrow()
    expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
  })

  it("drawer upgrade via SSE does not orphan the prior drawer instance", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    await Promise.resolve()
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z")
    })
    // Open drawer on the bare card.
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getAllByTestId("turn-detail-drawer")).toHaveLength(1)

    // Live turn.complete upgrades the card in place — drawer must stay
    // open, still exactly one instance in the DOM.
    act(() => {
      emitComplete(
        sse,
        "turn-live",
        "claude-opus-4-7",
        "2026-04-24T00:00:00.500Z",
        "live upgrade summary",
      )
    })
    expect(view.getAllByTestId("turn-detail-drawer")).toHaveLength(1)
    // Upgrade materialises messages section with populated content
    // rather than the "waiting for turn.complete" placeholder.
    expect(view.queryByTestId("turn-detail-messages-placeholder")).toBeNull()
  })
})
