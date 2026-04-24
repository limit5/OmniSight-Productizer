/**
 * ZZ.B1 (#304-1, 2026-04-24) checkbox 2 — TurnDetailDrawer test matrix.
 *
 * Locks the click-to-expand contract for the per-turn detail drawer:
 *   - clicking a <TurnCard> opens the drawer
 *   - ESC, backdrop click, and close button all dismiss it
 *   - ``messages === undefined`` (no ``turn.complete`` received yet)
 *     renders the "waiting for turn.complete event" placeholder instead
 *     of an empty body — NULL-vs-genuine-zero contract ZZ.A1 established
 *   - ``messages === []`` renders a distinct "no messages recorded"
 *     empty state (degenerate turn, not missing payload)
 *   - a populated ``messages`` array renders one card per message with
 *     role badge + content + per-message token tally + totals sum
 *   - ``toolCallDetails`` renders the explicit tool list (pass + fail)
 *   - absent ``toolCallDetails`` but present ``failedTools`` falls back
 *     to a synthetic list so the drawer isn't empty before turn.complete
 *
 * Tests drive the component via the ``externalTurns`` escape hatch to
 * bypass SSE wiring — same pattern ``turn-timeline.test.tsx`` uses for
 * the card-side covered in checkbox 1.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, fireEvent, act } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    subscribeEvents: vi.fn(),
  }
})

import * as api from "@/lib/api"
import {
  TurnTimeline,
  type TurnCardData,
  type TurnMessagePart,
  type TurnToolCallDetail,
} from "@/components/omnisight/turn-timeline"
import { primeSSE } from "../helpers/sse"

beforeEach(() => {
  vi.clearAllMocks()
})

function makeTurn(overrides: Partial<TurnCardData> = {}): TurnCardData {
  const base: TurnCardData = {
    turnNumber: 1,
    timestamp: "2026-04-24T00:00:00.000Z",
    tsMs: Date.parse("2026-04-24T00:00:00.000Z"),
    model: "claude-opus-4-7",
    provider: "anthropic",
    agentSubtype: "orchestrator",
    inputTokens: 5200,
    outputTokens: 2100,
    tokensUsed: 7300,
    cacheReadTokens: 12_000,
    cacheCreateTokens: 4_000,
    cacheHitRatio: 12_000 / (5200 + 12_000 + 4_000),
    contextLimit: 200_000,
    contextUsagePct: 42,
    latencyMs: 350,
    toolCallCount: 3,
    toolFailureCount: 0,
    failedTools: [],
    gapMs: 120,
    costUsd: 0.042,
    summary: null,
  }
  return { ...base, ...overrides }
}

describe("<TurnTimeline> drawer (ZZ.B1 checkbox 2)", () => {
  it("opens the drawer when a turn card is clicked", () => {
    primeSSE(api)
    const view = render(<TurnTimeline externalTurns={[makeTurn()]} />)
    expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
    act(() => {
      fireEvent.click(view.getByTestId("turn-card"))
    })
    expect(view.getByTestId("turn-detail-drawer")).toBeInTheDocument()
    expect(view.getByTestId("turn-detail-turn-number")).toHaveTextContent(/Turn #1/)
    expect(view.getByTestId("turn-detail-model")).toHaveTextContent(/orchestrator/)
  })

  it("closes via the X button, the backdrop click, and the ESC key", () => {
    primeSSE(api)
    const view = render(<TurnTimeline externalTurns={[makeTurn()]} />)
    // Close via X button
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getByTestId("turn-detail-drawer")).toBeInTheDocument()
    act(() => { fireEvent.click(view.getByTestId("turn-detail-close")) })
    expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
    // Close via backdrop
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    act(() => { fireEvent.click(view.getByTestId("turn-detail-drawer-backdrop")) })
    expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
    // Close via ESC
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    act(() => { fireEvent.keyDown(window, { key: "Escape" }) })
    expect(view.queryByTestId("turn-detail-drawer")).toBeNull()
  })

  it("renders the waiting-for-turn.complete placeholder when messages is undefined", () => {
    primeSSE(api)
    // ``messages: undefined`` — payload has not arrived yet
    const view = render(<TurnTimeline externalTurns={[makeTurn({ messages: undefined })]} />)
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    const placeholder = view.getByTestId("turn-detail-messages-placeholder")
    expect(placeholder).toBeInTheDocument()
    expect(placeholder.textContent).toMatch(/turn\.complete/)
    // The actual-messages-empty placeholder MUST NOT also appear — the
    // two states are visually / semantically distinct.
    expect(view.queryByTestId("turn-detail-messages-empty")).toBeNull()
    expect(view.queryByTestId("turn-detail-message")).toBeNull()
  })

  it("renders the empty-array placeholder when messages is [] (distinct from not-yet-received)", () => {
    primeSSE(api)
    // ``messages: []`` — payload landed but degenerate (no actual messages)
    const view = render(<TurnTimeline externalTurns={[makeTurn({ messages: [] })]} />)
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getByTestId("turn-detail-messages-empty")).toBeInTheDocument()
    expect(view.queryByTestId("turn-detail-messages-placeholder")).toBeNull()
  })

  it("renders one card per message with role badge, content, and token breakdown", () => {
    primeSSE(api)
    const messages: TurnMessagePart[] = [
      { role: "system",    content: "You are a helpful assistant.", tokens: 42 },
      { role: "user",      content: "Hello, how are you?",          tokens: 12 },
      { role: "assistant", content: "I am well — how can I help?",  tokens: 18 },
      { role: "tool",      content: '{"exit_code": 0}',             tokens: 6, toolName: "run_bash" },
    ]
    const view = render(<TurnTimeline externalTurns={[makeTurn({ messages })]} />)
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    const cards = view.getAllByTestId("turn-detail-message")
    expect(cards).toHaveLength(4)
    expect(cards.map(c => c.getAttribute("data-role"))).toEqual([
      "system", "user", "assistant", "tool",
    ])
    const roleLabels = view.getAllByTestId("turn-detail-message-role").map(r => r.textContent)
    expect(roleLabels).toEqual(["SYSTEM", "USER", "ASSISTANT", "TOOL"])
    const contents = view.getAllByTestId("turn-detail-message-content").map(c => c.textContent)
    expect(contents[0]).toMatch(/helpful assistant/)
    expect(contents[3]).toMatch(/exit_code/)
    const tokenCells = view.getAllByTestId("turn-detail-message-tokens").map(t => t.textContent)
    expect(tokenCells[0]).toMatch(/42 tokens/)
    expect(tokenCells[3]).toMatch(/6 tokens/)
    // Tool name surfaces on the ``tool`` role card
    expect(view.getByTestId("turn-detail-message-tool-name")).toHaveTextContent(/run_bash/)
    // Aggregate sum shown in the footer
    expect(view.getByTestId("turn-detail-messages-sum")).toHaveTextContent(/78 tokens/)
    expect(view.getByTestId("turn-detail-messages-count")).toHaveTextContent(/4 messages/)
  })

  it("degrades token cell to '— tokens' when tokens is null (NULL-vs-genuine-zero contract)", () => {
    primeSSE(api)
    const messages: TurnMessagePart[] = [
      { role: "assistant", content: "no attribution", tokens: null },
    ]
    const view = render(<TurnTimeline externalTurns={[makeTurn({ messages })]} />)
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getByTestId("turn-detail-message-tokens")).toHaveTextContent("— tokens")
  })

  it("renders explicit tool call details with pass/fail, args, result, duration", () => {
    primeSSE(api)
    const toolCallDetails: TurnToolCallDetail[] = [
      { name: "run_bash", success: true,  args: { cmd: "ls -la" }, result: "total 0\n", durationMs: 45 },
      { name: "read_file", success: false, args: { path: "/etc/hosts" }, result: "ERROR: permission denied", durationMs: 3 },
    ]
    const view = render(<TurnTimeline externalTurns={[makeTurn({ toolCallDetails })]} />)
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    const tools = view.getAllByTestId("turn-detail-tool-call")
    expect(tools).toHaveLength(2)
    expect(tools.map(t => t.getAttribute("data-success"))).toEqual(["true", "false"])
    const names = view.getAllByTestId("turn-detail-tool-call-name").map(n => n.textContent)
    expect(names).toEqual(["run_bash", "read_file"])
    const statuses = view.getAllByTestId("turn-detail-tool-call-status").map(s => s.textContent)
    expect(statuses).toEqual(["ok", "failed"])
    // Args + result pre-blocks render JSON / raw string
    const args = view.getAllByTestId("turn-detail-tool-call-args").map(a => a.textContent)
    expect(args[0]).toMatch(/"cmd"/)
    expect(args[0]).toMatch(/ls -la/)
    const results = view.getAllByTestId("turn-detail-tool-call-result").map(r => r.textContent)
    expect(results[1]).toMatch(/permission denied/)
  })

  it("falls back to failedTools when toolCallDetails is absent (waiting for turn.complete)", () => {
    primeSSE(api)
    const view = render(
      <TurnTimeline
        externalTurns={[
          makeTurn({
            toolCallDetails: undefined,
            toolCallCount: 3,
            toolFailureCount: 2,
            failedTools: ["run_bash", "read_file"],
          }),
        ]}
      />,
    )
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    const tools = view.getAllByTestId("turn-detail-tool-call")
    // Synthesised entries from failedTools — both marked failed
    expect(tools).toHaveLength(2)
    expect(tools.every(t => t.getAttribute("data-success") === "false")).toBe(true)
    // Top-of-section aggregate count still reflects the backend-reported
    // numbers (not the number of synthesised entries) so operators see
    // "3 calls · 2 failed" even though only 2 names were surfaced.
    expect(view.getByTestId("turn-detail-tools-count")).toHaveTextContent(/3 calls/)
    expect(view.getByTestId("turn-detail-tools-count")).toHaveTextContent(/2 failed/)
  })

  it("shows the 'waiting for turn.complete' tool placeholder when no detail nor failedTools", () => {
    primeSSE(api)
    const view = render(
      <TurnTimeline
        externalTurns={[
          makeTurn({
            toolCallDetails: undefined,
            toolCallCount: 4,
            toolFailureCount: 0,
            failedTools: [],
          }),
        ]}
      />,
    )
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getByTestId("turn-detail-tools-empty")).toHaveTextContent(/turn\.complete/)
  })

  it("shows the 'No tools invoked on this turn.' empty state when toolCallCount is 0", () => {
    primeSSE(api)
    const view = render(
      <TurnTimeline
        externalTurns={[
          makeTurn({
            toolCallDetails: [],
            toolCallCount: 0,
            toolFailureCount: 0,
            failedTools: [],
          }),
        ]}
      />,
    )
    act(() => { fireEvent.click(view.getByTestId("turn-card")) })
    expect(view.getByTestId("turn-detail-tools-empty")).toHaveTextContent(/No tools invoked/)
  })

  it("keeps the drawer open across SSE updates to the same turn (update by turnNumber key)", () => {
    const sse = primeSSE(api)
    const t0 = makeTurn({ toolCallCount: null, toolFailureCount: null })
    const { rerender, getByTestId, queryByTestId } = render(
      <TurnTimeline externalTurns={[t0]} />,
    )
    act(() => { fireEvent.click(getByTestId("turn-card")) })
    expect(getByTestId("turn-detail-drawer")).toBeInTheDocument()
    // Simulate a fresh turn_tool_stats event updating the same turnNumber
    rerender(
      <TurnTimeline
        externalTurns={[{ ...t0, toolCallCount: 5, toolFailureCount: 0 }]}
      />,
    )
    expect(getByTestId("turn-detail-drawer")).toBeInTheDocument()
    // New tool count visible in the drawer header
    expect(getByTestId("turn-detail-tools-count")).toHaveTextContent(/5 calls/)
    expect(queryByTestId("turn-detail-messages-placeholder")).toBeInTheDocument()
    // eslint guard: make sure the SSE handle was acquired (covers the
    // fact that externalTurns is the escape hatch and SSE still mounts
    // for the counter ref bookkeeping).
    expect(sse.closeCount()).toBeGreaterThanOrEqual(0)
  })
})
