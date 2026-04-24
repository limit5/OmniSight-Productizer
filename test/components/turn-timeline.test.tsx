/**
 * ZZ.B1 (#304-1, 2026-04-24) sanity smoke test for the new
 * ``<TurnTimeline>`` component at the top of the ORCHESTRATOR AI
 * panel. This test only covers the first ZZ.B1 checkbox (the 5-line
 * card + horizontal/vertical layout toggle); the drawer, ring-buffer
 * LRU, and ``turn.complete`` SSE integration are subsequent checkboxes
 * and will land with their own tests.
 *
 * Covered here:
 *  - empty state renders a "waiting for LLM activity" placeholder
 *  - a ``turn_metrics`` event materialises a card with the five lines
 *    we care about (turn #, timestamp, cost, tokens in/out, ctx bar,
 *    gap, tools, cache badge, summary line)
 *  - the horizontal ↔ vertical layout toggle swaps ``data-testid``
 *    on the cards container
 *  - attaching ``turn_tool_stats`` to the latest turn lights up the
 *    failed-tools red badge per the NULL-vs-genuine-zero contract
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, act, fireEvent } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
  }
})

import * as api from "@/lib/api"
import { TurnTimeline } from "@/components/omnisight/turn-timeline"
import { primeSSE } from "../helpers/sse"

beforeEach(() => {
  vi.clearAllMocks()
})

function emitMetrics(
  sse: ReturnType<typeof primeSSE>,
  model: string,
  iso: string,
  latencyMs: number,
  extra: Partial<{
    input_tokens: number
    output_tokens: number
    tokens_used: number
    context_limit: number | null
    context_usage_pct: number | null
    cache_read_tokens: number | null
    cache_create_tokens: number | null
  }> = {},
) {
  sse.emit({
    event: "turn_metrics",
    data: {
      provider: "anthropic",
      model,
      input_tokens: extra.input_tokens ?? 5200,
      output_tokens: extra.output_tokens ?? 2100,
      tokens_used: extra.tokens_used ?? 7300,
      context_limit: extra.context_limit ?? 1_000_000,
      context_usage_pct: extra.context_usage_pct ?? 12,
      latency_ms: latencyMs,
      cache_read_tokens: extra.cache_read_tokens ?? null,
      cache_create_tokens: extra.cache_create_tokens ?? null,
      timestamp: iso,
    },
  })
}

function emitToolStats(
  sse: ReturnType<typeof primeSSE>,
  callCount: number,
  failureCount: number,
  failedTools: string[] = [],
) {
  sse.emit({
    event: "turn_tool_stats",
    data: {
      agent_type: "orchestrator",
      task_id: "task-1",
      tool_call_count: callCount,
      tool_failure_count: failureCount,
      failed_tools: failedTools,
      timestamp: "2026-04-24T00:00:02.000Z",
    },
  })
}

describe("<TurnTimeline>", () => {
  it("renders empty state when no turn_metrics have arrived", () => {
    primeSSE(api)
    const { getByTestId, queryAllByTestId } = render(<TurnTimeline />)
    expect(getByTestId("turn-timeline")).toBeInTheDocument()
    expect(getByTestId("turn-timeline-empty")).toHaveTextContent(/waiting for LLM activity/i)
    expect(queryAllByTestId("turn-card")).toHaveLength(0)
  })

  it("materialises a 5-line card from a turn_metrics event", () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z", 350, {
        input_tokens: 5200,
        output_tokens: 2100,
        context_limit: 200_000,
        context_usage_pct: 42,
        cache_read_tokens: 12_000,
      })
    })
    const card = view.getByTestId("turn-card")
    expect(card).toBeInTheDocument()
    expect(view.getByTestId("turn-card-number")).toHaveTextContent("#1")
    // Line 1 timestamp (first turn = +00:00:00 vs self)
    expect(view.getByTestId("turn-card-timestamp")).toHaveTextContent("+00:00:00")
    // Line 1 subtype defaults to model shortLabel when agent_subtype not seen yet
    expect(view.getByTestId("turn-card-subtype")).toHaveTextContent(/Opus/)
    // Line 2 cost estimate is > $0 for a known Claude model with real tokens
    const costText = view.getByTestId("turn-card-cost").textContent ?? ""
    expect(costText.startsWith("$")).toBe(true)
    expect(costText).not.toBe("$—")
    expect(view.getByTestId("turn-card-tokens-in")).toHaveTextContent(/in 5\.2k/)
    expect(view.getByTestId("turn-card-tokens-out")).toHaveTextContent(/out 2\.1k/)
    expect(view.getByTestId("turn-card-tokens-cache")).toHaveTextContent(/cache 12\.0k/)
    // Line 3 context bar + gap (first turn has no prior — gap "—")
    expect(view.getByTestId("turn-card-ctx-pct")).toHaveTextContent("42%")
    expect(view.getByTestId("turn-card-gap")).toHaveTextContent(/gap —/)
    // Line 4 tools badge is "—" until turn_tool_stats arrives
    expect(view.getByTestId("turn-card-tools")).toHaveTextContent(/tools —/)
    expect(view.getByTestId("turn-card-tools-failed")).toHaveTextContent(/failed —/)
    // Line 4 cache hit ratio visible (non-null when cache_read > 0)
    expect(view.getByTestId("turn-card-cache-hit")).not.toHaveTextContent(/cache —/)
    // Line 5 summary placeholder exists and is non-empty
    expect(view.getByTestId("turn-card-summary")).not.toHaveTextContent("")
  })

  it("toggles between horizontal scroll and vertical stack layouts", () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z", 200)
    })
    // Default = horizontal
    expect(view.getByTestId("turn-timeline-body-horizontal")).toBeInTheDocument()
    // Switch to vertical
    act(() => {
      fireEvent.click(view.getByTestId("turn-timeline-layout-vertical"))
    })
    expect(view.getByTestId("turn-timeline-body-vertical")).toBeInTheDocument()
    expect(view.queryByTestId("turn-timeline-body-horizontal")).toBeNull()
    // Back to horizontal
    act(() => {
      fireEvent.click(view.getByTestId("turn-timeline-layout-horizontal"))
    })
    expect(view.getByTestId("turn-timeline-body-horizontal")).toBeInTheDocument()
  })

  it("attaches turn_tool_stats to the most recent turn and reddens failed count", () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z", 200)
      emitToolStats(sse, 3, 1, ["run_bash"])
    })
    const failedEl = view.getByTestId("turn-card-tools-failed")
    expect(failedEl).toHaveTextContent(/failed 1/)
    // Failure > 0 → red + bold
    expect(failedEl.className).toMatch(/critical-red/)
    expect(failedEl.className).toMatch(/font-semibold/)
    // Subtype picked up agent_type once it arrived
    expect(view.getByTestId("turn-card-subtype")).toHaveTextContent(/orchestrator/)
  })

  it("computes relative timestamp from the anchor (first turn)", () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    act(() => {
      emitMetrics(sse, "claude-sonnet-4-6", "2026-04-24T00:00:00.000Z", 200)
      emitMetrics(sse, "claude-sonnet-4-6", "2026-04-24T00:01:05.000Z", 300)
    })
    const timestamps = view.getAllByTestId("turn-card-timestamp")
    expect(timestamps[0]).toHaveTextContent("+00:00:00")
    expect(timestamps[1]).toHaveTextContent("+00:01:05")
  })

  it("respects the maxTurns ring-buffer cap", () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline maxTurns={2} />)
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z", 200)
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:01.000Z", 200)
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:02.000Z", 200)
    })
    const cards = view.getAllByTestId("turn-card")
    expect(cards).toHaveLength(2)
    // Oldest evicted — remaining turn numbers are #2 and #3 (counter
    // keeps incrementing; LRU evicts by arrival order, not by number)
    const numbers = view.getAllByTestId("turn-card-number").map(n => n.textContent)
    expect(numbers).toEqual(["#2", "#3"])
  })
})
