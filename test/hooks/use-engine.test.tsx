/** Fix-D D7: useEngine contract smoke.
 *
 * The engine hook is 700+ LOC; we are not testing every branch here —
 * just the public state surface and the pure-state operations
 * (patchAgentLocal, setAgents updater) which are most likely to
 * regress under a careless refactor.
 *
 * The mount-time `init()` path fires ~12 api calls and an SSE
 * subscribe; we mock `@/lib/api` wholesale so the hook returns empty
 * arrays and `connected=false` deterministically.
 */
import { renderHook, act, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

// Mock the entire api module before importing the hook.
vi.mock("@/lib/api", () => {
  const reject = () => Promise.reject(new Error("offline-mock"))
  return {
    listAgents: reject,
    listTasks: reject,
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
    subscribeEvents: () => ({ close: () => {}, readyState: 1 }),
  }
})

import { useEngine } from "@/hooks/use-engine"

afterEach(() => {
  vi.restoreAllMocks()
})


describe("useEngine — initial state", () => {
  it("starts with empty arrays and connected=false", async () => {
    const { result } = renderHook(() => useEngine())
    // Initial synchronous state
    expect(result.current.agents).toEqual([])
    expect(result.current.tasks).toEqual([])
    expect(result.current.messages).toEqual([])
    expect(result.current.connected).toBe(false)
    expect(result.current.isStreaming).toBe(false)
    expect(result.current.notifications).toEqual([])
    expect(result.current.unreadCount).toBe(0)
    // Exposes the expected callable surface
    expect(typeof result.current.addAgent).toBe("function")
    expect(typeof result.current.removeAgent).toBe("function")
    expect(typeof result.current.patchAgentLocal).toBe("function")
    expect(typeof result.current.assignTask).toBe("function")
    expect(typeof result.current.completeTask).toBe("function")
    expect(typeof result.current.invoke).toBe("function")
    expect(typeof result.current.refresh).toBe("function")
    expect(typeof result.current.setProviderSwitchCallback).toBe("function")
  })
})


describe("useEngine — patchAgentLocal", () => {
  it("updates only the matching agent, preserving others", async () => {
    const { result } = renderHook(() => useEngine())

    // Seed two agents directly via the exposed setter.
    act(() => {
      result.current.setAgents([
        { id: "a1", name: "Alpha", type: "firmware", status: "idle",
          progress: { current: 0, total: 5 }, thoughtChain: "" } as never,
        { id: "a2", name: "Beta", type: "validator", status: "idle",
          progress: { current: 0, total: 3 }, thoughtChain: "" } as never,
      ])
    })
    expect(result.current.agents).toHaveLength(2)

    act(() => {
      result.current.patchAgentLocal("a1", { status: "running", thoughtChain: "working" })
    })

    const [a1, a2] = result.current.agents
    expect(a1.id).toBe("a1")
    expect(a1.status).toBe("running")
    expect(a1.thoughtChain).toBe("working")
    expect(a2.id).toBe("a2")
    expect(a2.status).toBe("idle")        // untouched
    expect(a2.thoughtChain).toBe("")      // untouched
  })

  it("is a no-op when the id doesn't match", () => {
    const { result } = renderHook(() => useEngine())
    act(() => {
      result.current.setAgents([
        { id: "a1", name: "Alpha", type: "firmware", status: "idle",
          progress: { current: 0, total: 5 }, thoughtChain: "" } as never,
      ])
    })
    const before = result.current.agents[0]
    act(() => {
      result.current.patchAgentLocal("missing", { status: "error" })
    })
    expect(result.current.agents[0]).toEqual(before)
  })
})


describe("useEngine — setAgents updater", () => {
  it("accepts a function updater (immutable)", () => {
    const { result } = renderHook(() => useEngine())
    act(() => {
      result.current.setAgents([
        { id: "a1", name: "X", type: "firmware", status: "idle",
          progress: { current: 0, total: 1 }, thoughtChain: "" } as never,
      ])
    })
    act(() => {
      result.current.setAgents(prev => prev.map(a => ({ ...a, status: "running" })))
    })
    expect(result.current.agents[0].status).toBe("running")
  })
})


describe("useEngine — offline addAgent fallback", () => {
  it("creates a local agent when connected=false", async () => {
    const { result } = renderHook(() => useEngine())
    // `connected` is false (mock listAgents rejects) — addAgent takes the
    // offline-fallback branch and synthesises an agent locally.
    await act(async () => {
      await result.current.addAgent("firmware", "TEST_AGENT")
    })
    // Wait for React to flush the state update.
    await waitFor(() => {
      expect(result.current.agents.some(a => a.name === "TEST_AGENT")).toBe(true)
    })
    const agent = result.current.agents.find(a => a.name === "TEST_AGENT")!
    expect(agent.status).toBe("booting")
    expect(agent.type).toBe("firmware")
  })
})
