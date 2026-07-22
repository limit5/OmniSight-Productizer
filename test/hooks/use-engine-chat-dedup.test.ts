/** OP-2724 — originator-echo dedup in the ``chat.message`` SSE handler.
 *
 * Prod symptom (2026-07-22, v0.8.0): the originating tab rendered every
 * assistant reply TWICE. ``sendCommand()`` appends the final reply from
 * the REST/stream response body, then the backend's ``chat.message``
 * broadcast (Q.3-SUB-6 #297 cross-device sync) echoes the same content
 * back under a DIFFERENT id — so the handler's ``prev.find(m => m.id
 * === d.id)`` guard never matched the originator's own echo.
 *
 * The fix dedups on (role, content) equality within a bounded recency
 * window (CHAT_ECHO_DEDUP_WINDOW). These tests lock the contract:
 *   - a broadcast of already-rendered content under a mismatched id is
 *     dropped (exactly one rendered line),
 *   - a broadcast of content NOT rendered locally still appends — the
 *     cross-device feature must keep working.
 */
import { renderHook, act, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

// Mock the entire api module before importing the hook (same wholesale
// pattern as test/hooks/use-engine.test.tsx — the hook's mount-time
// init() fires ~12 api calls plus the SSE subscribe).
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
    invoke: vi.fn(),
    subscribeEvents: vi.fn(() => ({ close: () => {}, readyState: 1 })),
    getChatHistory: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    getProvidersBalance: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    getNotifications: vi.fn(() => Promise.reject(new Error("offline-mock"))),
    streamInvoke: vi.fn(async function* () {}),
    // OP-2724: sendCommand() prefers the streaming endpoint; each test
    // overrides streamChat to deliver its scripted reply. sendChat is
    // the sync fallback — stubbed rejecting so a test that accidentally
    // falls through fails loudly instead of silently going offline.
    streamChat: vi.fn(async function* () {}),
    sendChat: vi.fn(() => Promise.reject(new Error("offline-mock"))),
  }
})

import * as api from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"
import { useEngine } from "@/hooks/use-engine"

const primeSSE = () => _primeSSE(api)

afterEach(() => {
  vi.restoreAllMocks()
})

function goOnline(): void {
  ;(api.listAgents as ReturnType<typeof vi.fn>).mockImplementation(
    () => Promise.resolve([]))
  ;(api.listTasks as ReturnType<typeof vi.fn>).mockImplementation(
    () => Promise.resolve([]))
}

/** Script the streaming endpoint to yield a single final reply row. */
function primeStreamReply(row: api.ApiChatMessage): void {
  ;(api.streamChat as ReturnType<typeof vi.fn>).mockImplementation(
    async function* () {
      yield { event: "done", data: row }
    },
  )
}

describe("useEngine — chat.message originator-echo dedup (OP-2724)", () => {
  it("drops the originator's own echo broadcast under a mismatched id", async () => {
    goOnline()
    const sse = primeSSE()
    // REST/stream response body id (backend microsecond timestamp)…
    primeStreamReply({
      id: "srv-row-777",
      role: "orchestrator",
      content: "Deployment looks healthy.",
      timestamp: "2026-07-22T10:00:00.123456",
    })
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    await act(async () => {
      await result.current.sendCommand("status?")
    })
    await waitFor(() => {
      expect(
        result.current.messages.some(m => m.id === "srv-row-777"),
      ).toBe(true)
    })

    // …then the broadcast echoes the SAME content under a DIFFERENT id
    // (the prod duplicate-reply symptom).
    act(() => {
      sse.emit({
        event: "chat.message",
        data: {
          id: "evt-broadcast-999",
          user_id: "anonymous",
          role: "orchestrator" as const,
          content: "Deployment looks healthy.",
          ts: "2026-07-22T10:00:00.900Z",
          timestamp: "2026-07-22T10:00:00.900Z",
        },
      })
    })

    // Exactly one rendered reply line; the echo id never materialises.
    await waitFor(() => {
      const replies = result.current.messages.filter(
        m => m.role === "orchestrator" && m.content === "Deployment looks healthy.",
      )
      expect(replies).toHaveLength(1)
    })
    expect(
      result.current.messages.some(m => m.id === "evt-broadcast-999"),
    ).toBe(false)
  })

  it("drops the echo of the originator's own user line too", async () => {
    goOnline()
    const sse = primeSSE()
    primeStreamReply({
      id: "srv-row-778",
      role: "orchestrator",
      content: "ack",
      timestamp: "2026-07-22T10:01:00.123456",
    })
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    await act(async () => {
      await result.current.sendCommand("run the nightly sweep")
    })
    await waitFor(() => {
      expect(
        result.current.messages.some(
          m => m.role === "user" && m.content === "run the nightly sweep",
        ),
      ).toBe(true)
    })

    // Backend persists + broadcasts the user row with its own id; the
    // originator appended it locally as ``msg-<Date.now()>`` already.
    act(() => {
      sse.emit({
        event: "chat.message",
        data: {
          id: "evt-user-echo-1",
          user_id: "anonymous",
          role: "user" as const,
          content: "run the nightly sweep",
          ts: "2026-07-22T10:01:00.200Z",
          timestamp: "2026-07-22T10:01:00.200Z",
        },
      })
    })

    await waitFor(() => {
      const userLines = result.current.messages.filter(
        m => m.role === "user" && m.content === "run the nightly sweep",
      )
      expect(userLines).toHaveLength(1)
    })
  })

  it("still appends a broadcast for content not rendered locally (cross-device)", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    // No local send happened — this line was typed on another device.
    act(() => {
      sse.emit({
        event: "chat.message",
        data: {
          id: "evt-remote-1",
          user_id: "anonymous",
          role: "user" as const,
          content: "hello from device B",
          ts: "2026-07-22T10:02:00.000Z",
          timestamp: "2026-07-22T10:02:00.000Z",
        },
      })
    })

    await waitFor(() => {
      const hit = result.current.messages.find(m => m.id === "evt-remote-1")
      expect(hit).toBeTruthy()
      expect(hit?.content).toBe("hello from device B")
      expect(hit?.role).toBe("user")
    })
  })

  it("appends a remote line whose content matches a DIFFERENT role's recent line", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    // Remote user line…
    act(() => {
      sse.emit({
        event: "chat.message",
        data: {
          id: "evt-role-a",
          user_id: "anonymous",
          role: "user" as const,
          content: "ping",
          ts: "2026-07-22T10:03:00.000Z",
          timestamp: "2026-07-22T10:03:00.000Z",
        },
      })
    })
    // …followed by an orchestrator line with identical content. The
    // dedup keys on (role, content), so this must NOT be swallowed.
    act(() => {
      sse.emit({
        event: "chat.message",
        data: {
          id: "evt-role-b",
          user_id: "anonymous",
          role: "orchestrator" as const,
          content: "ping",
          ts: "2026-07-22T10:03:01.000Z",
          timestamp: "2026-07-22T10:03:01.000Z",
        },
      })
    })

    await waitFor(() => {
      const ids = result.current.messages.map(m => m.id)
      expect(ids).toContain("evt-role-a")
      expect(ids).toContain("evt-role-b")
    })
  })

  it("keeps the id-based dedup for exact replays (idempotent apply)", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    const payload = {
      event: "chat.message" as const,
      data: {
        id: "evt-replay-1",
        user_id: "anonymous",
        role: "orchestrator" as const,
        content: "replayed reply",
        ts: "2026-07-22T10:04:00.000Z",
        timestamp: "2026-07-22T10:04:00.000Z",
      },
    }
    act(() => { sse.emit(payload) })
    act(() => { sse.emit(payload) })

    await waitFor(() => {
      const hits = result.current.messages.filter(m => m.id === "evt-replay-1")
      expect(hits).toHaveLength(1)
    })
  })
})
