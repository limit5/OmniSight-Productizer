/**
 * ZZ.B1 (#304-1, 2026-04-24) checkbox 3 — turn.complete SSE + history
 * backfill integration test.
 *
 * Locks the new wiring on the frontend:
 *  - On mount, ``<TurnTimeline>`` calls ``fetchTurnHistory`` to seed
 *    the ring buffer (oldest-first) so reconnects don't start blank.
 *  - The live ``turn.complete`` SSE event upgrades the card previously
 *    materialised by ``turn_metrics`` (match by turn_id, fallback to
 *    "latest card for this model without a turn_id").
 *  - Ring buffer LRU still caps at ``maxTurns`` when ``turn.complete``
 *    would otherwise grow it past the cap (reconnect mid-turn path).
 *  - Drawer renders populated messages + tool details after
 *    ``turn.complete`` arrives (no more "Waiting for turn.complete
 *    event" placeholder).
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, act, waitFor } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
    fetchTurnHistory: vi.fn(),
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
  latencyMs = 350,
) {
  sse.emit({
    event: "turn_metrics",
    data: {
      provider: "anthropic",
      model,
      input_tokens: 5200,
      output_tokens: 2100,
      tokens_used: 7300,
      context_limit: 1_000_000,
      context_usage_pct: 0.73,
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
  overrides: Partial<{
    cost_usd: number | null
    summary: string | null
    messages: Array<{ role: string; content: string; tokens?: number | null; tool_name?: string | null }>
    tool_calls: Array<{ name: string; success: boolean; args?: unknown; result?: string | null; duration_ms?: number | null }>
    tool_call_count: number
    tool_failure_count: number
    timestamp: string
  }> = {},
) {
  sse.emit({
    event: "turn.complete",
    data: {
      turn_id: turnId,
      provider: "anthropic",
      model,
      agent_type: "orchestrator",
      task_id: "task-1",
      input_tokens: 5200,
      output_tokens: 2100,
      tokens_used: 7300,
      context_limit: 1_000_000,
      context_usage_pct: 0.73,
      latency_ms: 350,
      cache_read_tokens: 0,
      cache_create_tokens: 0,
      cost_usd: overrides.cost_usd ?? 0.2355,
      started_at: "2026-04-24T00:00:00Z",
      ended_at: "2026-04-24T00:00:00.350Z",
      summary: overrides.summary ?? "certainly — here is the answer.",
      messages: overrides.messages ?? [
        { role: "system", content: "you are a coding assistant" },
        { role: "user", content: "what is 2+2?" },
        { role: "assistant", content: "certainly — here is the answer." },
      ],
      tool_calls: overrides.tool_calls ?? [],
      tool_call_count: overrides.tool_call_count ?? 0,
      tool_failure_count: overrides.tool_failure_count ?? 0,
      timestamp: overrides.timestamp ?? "2026-04-24T00:00:01.000Z",
    },
  })
}

describe("<TurnTimeline> turn.complete + history (ZZ.B1 checkbox 3)", () => {
  it("calls fetchTurnHistory on mount and seeds the ring buffer", async () => {
    const sse = primeSSE(api, {
      history: [
        {
          turn_id: "turn-h2",
          provider: "anthropic",
          model: "claude-sonnet-4-20250514",
          agent_type: "firmware",
          task_id: null,
          input_tokens: 1000,
          output_tokens: 500,
          tokens_used: 1500,
          context_limit: 200_000,
          context_usage_pct: 0.75,
          latency_ms: 120,
          cache_read_tokens: 0,
          cache_create_tokens: 0,
          cost_usd: 0.0105,
          started_at: "2026-04-23T23:59:30Z",
          ended_at: "2026-04-23T23:59:30.120Z",
          summary: "sonnet reply",
          messages: [],
          tool_calls: [],
          tool_call_count: 0,
          tool_failure_count: 0,
          timestamp: "2026-04-23T23:59:30Z",
        },
        {
          turn_id: "turn-h1",
          provider: "anthropic",
          model: "claude-opus-4-7",
          agent_type: "orchestrator",
          task_id: null,
          input_tokens: 2000,
          output_tokens: 1000,
          tokens_used: 3000,
          context_limit: 1_000_000,
          context_usage_pct: 0.3,
          latency_ms: 400,
          cache_read_tokens: 0,
          cache_create_tokens: 0,
          cost_usd: 0.105,
          started_at: "2026-04-23T23:59:00Z",
          ended_at: "2026-04-23T23:59:00.400Z",
          summary: "opus reply",
          messages: [],
          tool_calls: [],
          tool_call_count: 0,
          tool_failure_count: 0,
          timestamp: "2026-04-23T23:59:00Z",
        },
      ],
    })
    const view = render(<TurnTimeline />)
    // fetchTurnHistory must have been called.
    expect((api.fetchTurnHistory as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0)
    // After the fetch resolves, two cards should render oldest-first.
    await waitFor(() => {
      const cards = view.queryAllByTestId("turn-card")
      expect(cards).toHaveLength(2)
    })
    const cards = view.getAllByTestId("turn-card")
    // Endpoint returns newest-first; component reverses to oldest-first
    // so the first rendered card is the older "turn-h1" (opus).
    expect(cards[0].querySelector('[data-testid="turn-card-summary"]')?.textContent).toMatch(/opus reply/i)
    expect(cards[1].querySelector('[data-testid="turn-card-summary"]')?.textContent).toMatch(/sonnet reply/i)
    // Sanity: silence unused-var lint for sse
    expect(sse.listeners.length).toBeGreaterThanOrEqual(0)
  })

  it("turn.complete upgrades the card materialised by turn_metrics", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    // Wait for mount-time fetch stub to resolve + live SSE subscription.
    await waitFor(() => expect(sse.listeners.length).toBe(1))

    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z", 350)
    })
    expect(view.getAllByTestId("turn-card")).toHaveLength(1)
    // Pre-complete: cost is the frontend estimate (≈ $0.16 for 5.2k in + 2.1k out @ opus).
    const preCost = view.getByTestId("turn-card-cost").textContent
    expect(preCost).toMatch(/\$/)

    act(() => {
      emitComplete(sse, "turn-xyz", "claude-opus-4-7", {
        cost_usd: 0.2355,
        summary: "backend-authoritative summary",
      })
    })

    // Still one card (upgrade in place, not duplication).
    expect(view.getAllByTestId("turn-card")).toHaveLength(1)
    // Summary upgrade: Line 5 now shows the backend summary.
    expect(view.getByTestId("turn-card-summary").textContent).toMatch(/backend-authoritative summary/i)
  })

  it("turn.complete upgrades drawer messages when the card is clicked open", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    await waitFor(() => expect(sse.listeners.length).toBe(1))

    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z")
      emitComplete(sse, "turn-xyz", "claude-opus-4-7", {
        messages: [
          { role: "system", content: "sys" },
          { role: "user", content: "hi" },
          { role: "assistant", content: "hello!" },
        ],
      })
    })

    const card = view.getByTestId("turn-card")
    act(() => { card.click() })

    const drawer = view.getByTestId("turn-detail-drawer")
    expect(drawer).toBeInTheDocument()
    // The "Waiting for turn.complete event" placeholder must be gone.
    expect(view.queryByTestId("turn-detail-messages-placeholder")).toBeNull()
    const messages = view.getAllByTestId("turn-detail-message")
    expect(messages).toHaveLength(3)
    expect(messages[0].getAttribute("data-role")).toBe("system")
    expect(messages[2].getAttribute("data-role")).toBe("assistant")
  })

  it("turn.complete renders tool_calls detail in the drawer", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    await waitFor(() => expect(sse.listeners.length).toBe(1))

    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z")
      emitComplete(sse, "turn-xyz", "claude-opus-4-7", {
        tool_call_count: 2,
        tool_failure_count: 1,
        tool_calls: [
          { name: "run_bash", success: true, args: { cmd: "ls" }, result: "a\nb\n", duration_ms: 12 },
          { name: "web_fetch", success: false, result: "timeout" },
        ],
      })
    })

    const card = view.getByTestId("turn-card")
    act(() => { card.click() })

    const toolCalls = view.getAllByTestId("turn-detail-tool-call")
    expect(toolCalls).toHaveLength(2)
    expect(toolCalls[0].getAttribute("data-success")).toBe("true")
    expect(toolCalls[1].getAttribute("data-success")).toBe("false")
    // Args + result panels rendered for the successful call.
    expect(view.getByTestId("turn-detail-tool-call-args").textContent).toMatch(/ls/)
    // Both tool calls have a ``result`` entry (timeout string for the failed one).
    const resultNodes = view.getAllByTestId("turn-detail-tool-call-result")
    expect(resultNodes).toHaveLength(2)
    expect(resultNodes[0].textContent).toMatch(/a\s*b/)
    expect(resultNodes[1].textContent).toMatch(/timeout/)
  })

  it("turn.complete without a prior turn_metrics creates a fresh card", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    await waitFor(() => expect(sse.listeners.length).toBe(1))

    // No turn_metrics — turn.complete arrives first (reconnect mid-turn
    // or dropped metrics event). The component must synthesise a card
    // from the complete payload so nothing is lost.
    act(() => {
      emitComplete(sse, "turn-solo", "claude-opus-4-7", {
        summary: "standalone turn",
      })
    })
    const cards = view.getAllByTestId("turn-card")
    expect(cards).toHaveLength(1)
    expect(view.getByTestId("turn-card-summary").textContent).toMatch(/standalone turn/i)
  })

  it("ring buffer LRU caps turn.complete synthesised cards at maxTurns", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline maxTurns={3} />)
    await waitFor(() => expect(sse.listeners.length).toBe(1))

    // Flood 5 turn.complete events — without prior turn_metrics so each
    // one materialises a fresh card via the synthesis path.
    act(() => {
      for (let i = 0; i < 5; i++) {
        emitComplete(sse, `turn-${i}`, "claude-opus-4-7", {
          summary: `turn summary ${i}`,
          timestamp: `2026-04-24T00:00:0${i}.000Z`,
        })
      }
    })
    const cards = view.getAllByTestId("turn-card")
    // maxTurns=3 ring buffer — oldest 2 evicted.
    expect(cards).toHaveLength(3)
    // Last card is the newest (turn-4).
    expect(cards[2].querySelector('[data-testid="turn-card-summary"]')?.textContent).toMatch(/turn summary 4/)
  })

  it("turn.complete match by turn_id beats positional match-by-model", async () => {
    const sse = primeSSE(api)
    const view = render(<TurnTimeline />)
    await waitFor(() => expect(sse.listeners.length).toBe(1))

    // Two opus turn_metrics back-to-back, then turn.complete for the
    // second one (by turn_id). The first card's turn_id must stay null
    // and get its own turn.complete later — the turn_id-based match is
    // strict enough not to collapse two legitimate turns into one.
    act(() => {
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:00.000Z")
      emitMetrics(sse, "claude-opus-4-7", "2026-04-24T00:00:01.000Z")
    })
    expect(view.getAllByTestId("turn-card")).toHaveLength(2)

    act(() => {
      emitComplete(sse, "turn-SECOND", "claude-opus-4-7", {
        summary: "second turn body",
      })
    })

    // Still 2 cards — the second one got upgraded.
    const cards = view.getAllByTestId("turn-card")
    expect(cards).toHaveLength(2)
    expect(cards[1].querySelector('[data-testid="turn-card-summary"]')?.textContent).toMatch(/second turn body/)
    // And the first card's summary is still the fallback (not the second turn's body).
    expect(cards[0].querySelector('[data-testid="turn-card-summary"]')?.textContent).not.toMatch(/second turn body/)
  })
})
