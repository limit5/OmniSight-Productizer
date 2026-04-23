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
    // vi.fn() for listAgents/listTasks/subscribeEvents so per-test
    // mockImplementation() can flip them online for the dispatcher
    // suite below.
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
  }
})

import * as api from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"
import { useEngine } from "@/hooks/use-engine"

const primeSSE = () => _primeSSE(api)

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


/**
 * Q.3-SUB-2 (#297) — task_update SSE dispatcher switches on ``action``.
 *
 * Before Q.3-SUB-2 the dispatcher only patched status on known task_id.
 * With create+delete emits now wired (tasks.py POST/DELETE), the
 * dispatcher must route on ``action``:
 *   - ``deleted``  → filter out of tasks list
 *   - ``created``  → refetch via api.listTasks (SSE payload lacks full row)
 *   - (missing / updated) → patch status + assignedAgentId in place
 */
describe("useEngine — task_update dispatcher action switch", () => {
  /**
   * The SSE subscribe is only wired after ``Promise.all([listAgents,
   * listTasks])`` resolves in the hook's mount-time init. The top-level
   * mock rejects both so the default suite goes offline; these tests
   * flip them back to resolved stubs via mockImplementation so the hook
   * reaches connectSSE() and primeSSE() can capture the listener.
   */
  function goOnline(tasks: unknown[] = []): ReturnType<typeof vi.fn> {
    ;(api.listAgents as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve([]))
    const listTasks = api.listTasks as ReturnType<typeof vi.fn>
    listTasks.mockImplementation(() => Promise.resolve(tasks))
    return listTasks
  }

  it("removes the task on action='deleted'", async () => {
    goOnline([])
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    act(() => {
      result.current.setTasks([
        { id: "t1", title: "Keep me", status: "backlog", priority: "low",
          createdAt: "2026-04-24T00:00:00" } as never,
        { id: "t2", title: "Drop me", status: "backlog", priority: "low",
          createdAt: "2026-04-24T00:00:00" } as never,
      ])
    })

    act(() => {
      sse.emit({
        event: "task_update",
        data: {
          task_id: "t2",
          status: "deleted",
          assigned_agent_id: null,
          action: "deleted",
          timestamp: "2026-04-24T00:00:01",
        },
      })
    })

    await waitFor(() => {
      expect(result.current.tasks.map(t => t.id)).toEqual(["t1"])
    })
  })

  it("refetches the full task list on action='created'", async () => {
    const listTasks = goOnline([])
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    // Drop the mount-time listTasks call so the assertion targets only
    // the dispatcher-driven refetch.
    listTasks.mockClear()
    // The dispatcher refetch must return the new row.
    listTasks.mockImplementation(() =>
      Promise.resolve([
        {
          id: "t-new", title: "Created on another device",
          description: null, priority: "medium", status: "backlog",
          assigned_agent_id: null, created_at: "2026-04-24T00:00:02",
          completed_at: null, ai_analysis: null,
          suggested_agent_type: null, external_issue_id: null,
          issue_url: null, acceptance_criteria: null, labels: [],
        },
      ]),
    )

    act(() => {
      sse.emit({
        event: "task_update",
        data: {
          task_id: "t-new",
          status: "backlog",
          assigned_agent_id: null,
          action: "created",
          timestamp: "2026-04-24T00:00:02",
        },
      })
    })

    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(1))
    await waitFor(() => {
      expect(result.current.tasks.find(t => t.id === "t-new")).toBeTruthy()
    })
  })

  it("patches status + assignedAgentId on action='updated' (and on missing action)", async () => {
    goOnline([])
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    act(() => {
      result.current.setTasks([
        { id: "t1", title: "Work", status: "backlog", priority: "low",
          createdAt: "2026-04-24T00:00:00" } as never,
      ])
    })

    // Missing action = legacy update path.
    act(() => {
      sse.emit({
        event: "task_update",
        data: {
          task_id: "t1",
          status: "in_progress",
          assigned_agent_id: "agent-1",
          timestamp: "2026-04-24T00:00:03",
        },
      })
    })

    await waitFor(() => {
      const t = result.current.tasks.find(x => x.id === "t1")
      expect(t?.status).toBe("in_progress")
      expect(t?.assignedAgentId).toBe("agent-1")
    })

    // Explicit action='updated' — same patch semantics.
    act(() => {
      sse.emit({
        event: "task_update",
        data: {
          task_id: "t1",
          status: "in_review",
          assigned_agent_id: "agent-1",
          action: "updated",
          timestamp: "2026-04-24T00:00:04",
        },
      })
    })

    await waitFor(() => {
      const t = result.current.tasks.find(x => x.id === "t1")
      expect(t?.status).toBe("in_review")
    })
  })
})
