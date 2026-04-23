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


/**
 * Q.3-SUB-3 (#297) — notification.read SSE dispatcher.
 *
 * Device A marks a notification read → device B must decrement its
 * unread counter and flip the matching row in its local notifications
 * list to ``read=true``. The counter is clamped at 0 so a replay or
 * cross-user misfire can't drive the badge negative, and the list
 * patch is guarded so a duplicate event leaves an already-flipped row
 * untouched.
 */
describe("useEngine — notification.read dispatcher", () => {
  function goOnline(tasks: unknown[] = []): void {
    ;(api.listAgents as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve([]))
    ;(api.listTasks as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve(tasks))
  }

  it("decrements unread count and marks the list row as read", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    // Seed one unread notification + a pretend unread counter of 3
    // (the bell badge is fed by a tenant-scoped DB COUNT, not the
    // bounded in-memory list — so the counter can legitimately be
    // larger than the list length).
    act(() => {
      result.current.setUnreadCount(3)
    })
    // Push a prior notification event so the list has something to
    // flip — useEngine populates the list via the SSE "notification"
    // branch, there is no direct setter.
    act(() => {
      sse.emit({
        event: "notification",
        data: {
          id: "n-ui-1",
          level: "warning",
          title: "disk",
          message: "almost full",
          source: "system",
          timestamp: "2026-04-24T00:00:00",
        },
      })
    })
    await waitFor(() => {
      expect(result.current.notifications.some(n => n.id === "n-ui-1")).toBe(true)
    })
    // The notification emit also bumps unread by 1 → 4.
    await waitFor(() => expect(result.current.unreadCount).toBe(4))

    act(() => {
      sse.emit({
        event: "notification.read",
        data: {
          id: "n-ui-1",
          user_id: "user-xyz",
          timestamp: "2026-04-24T00:00:01",
        },
      })
    })

    await waitFor(() => {
      const row = result.current.notifications.find(n => n.id === "n-ui-1")
      expect(row?.read).toBe(true)
      expect(result.current.unreadCount).toBe(3)
    })
  })

  it("clamps unread count at 0 when already zero", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    // Counter starts at 0 (mocked getUnreadCount rejects).
    expect(result.current.unreadCount).toBe(0)

    act(() => {
      sse.emit({
        event: "notification.read",
        data: {
          id: "n-never-seen",
          user_id: "user-xyz",
          timestamp: "2026-04-24T00:00:02",
        },
      })
    })

    // Math.max(0, prev - 1) floor prevents the badge going negative
    // on replay / missed-notification races.
    await waitFor(() => {
      expect(result.current.unreadCount).toBe(0)
    })
  })

  it("leaves the list untouched when the notification is unknown locally", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    act(() => {
      result.current.setUnreadCount(2)
    })
    act(() => {
      sse.emit({
        event: "notification",
        data: {
          id: "n-keep",
          level: "info",
          title: "something else",
          message: "",
          source: "system",
          timestamp: "2026-04-24T00:00:00",
        },
      })
    })
    await waitFor(() => {
      expect(result.current.notifications.some(n => n.id === "n-keep")).toBe(true)
    })

    const beforeList = result.current.notifications.map(n => ({
      id: n.id, read: n.read,
    }))

    // Mark-read event targeting a notification the bell list never
    // saw — unread counter still decrements (truth is the DB COUNT),
    // but the local list must be left alone so ``n-keep``'s read
    // state isn't accidentally flipped.
    act(() => {
      sse.emit({
        event: "notification.read",
        data: {
          id: "n-missing",
          user_id: "user-xyz",
          timestamp: "2026-04-24T00:00:03",
        },
      })
    })

    await waitFor(() => {
      expect(result.current.unreadCount).toBeLessThan(3)
    })
    expect(result.current.notifications.map(n => ({ id: n.id, read: n.read })))
      .toEqual(beforeList)
  })
})


/**
 * Q.3-SUB-4 (#297) — preferences.updated SSE dispatcher (log-only).
 *
 * The REPORTER VORTEX log line is the only thing the engine hook does
 * for ``preferences.updated`` — the actual localStorage patch + in-tab
 * notification fan-out is handled by ``storage-bridge.tsx`` so
 * ``useEngine`` stays auth-context-free. This test locks the contract:
 * an event arriving on the SSE stream produces a single ``[PREFS]``
 * log line so operators can trace cross-device pref sync activity.
 */
describe("useEngine — preferences.updated dispatcher", () => {
  function goOnline(): void {
    ;(api.listAgents as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve([]))
    ;(api.listTasks as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve([]))
  }

  it("appends a REPORTER VORTEX log line on preferences.updated", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    act(() => {
      sse.emit({
        event: "preferences.updated",
        data: {
          pref_key: "locale",
          value: "ja",
          user_id: "user-xyz",
          timestamp: "2026-04-24T00:00:05",
        },
      })
    })

    await waitFor(() => {
      const hit = result.current.logs.find(
        l => l.message.includes("[PREFS]") && l.message.includes("locale"),
      )
      expect(hit).toBeTruthy()
      expect(hit?.message).toContain("ja")
      expect(hit?.level).toBe("info")
    })
  })

  it("does not patch tasks/agents/notifications (scope is log-only)", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    // Snapshot content, not array identity — setLogs triggers a
    // re-render that allocates fresh empty arrays for unchanged
    // slices under StrictMode, so referential equality is the wrong
    // assertion here. The contract is that NO state change is driven
    // by ``preferences.updated`` other than appending a log line.
    const beforeTasks = [...result.current.tasks]
    const beforeAgents = [...result.current.agents]
    const beforeNotifs = [...result.current.notifications]
    const beforeUnread = result.current.unreadCount

    act(() => {
      sse.emit({
        event: "preferences.updated",
        data: {
          pref_key: "tour_seen",
          value: "1",
          user_id: "user-xyz",
          timestamp: "2026-04-24T00:00:06",
        },
      })
    })

    await waitFor(() => {
      expect(result.current.logs.some(l => l.message.includes("[PREFS]"))).toBe(true)
    })
    expect(result.current.tasks).toEqual(beforeTasks)
    expect(result.current.agents).toEqual(beforeAgents)
    expect(result.current.notifications).toEqual(beforeNotifs)
    expect(result.current.unreadCount).toBe(beforeUnread)
  })
})


/**
 * Q.3-SUB-5 (#297) — integration.settings.updated SSE dispatcher (log-only).
 *
 * The REPORTER VORTEX log line is the only thing the engine hook does for
 * ``integration.settings.updated`` — the modal refetch is owned by
 * ``components/omnisight/integration-settings.tsx`` which subscribes
 * separately and re-calls ``getSettings()`` + ``getProviders()`` on push.
 * This test locks the log-only contract on the engine side.
 */
describe("useEngine — integration.settings.updated dispatcher", () => {
  function goOnline(): void {
    ;(api.listAgents as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve([]))
    ;(api.listTasks as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.resolve([]))
  }

  it("appends a REPORTER VORTEX log line on integration.settings.updated", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    act(() => {
      sse.emit({
        event: "integration.settings.updated",
        data: {
          fields_changed: ["gerrit_url", "gerrit_project"],
          timestamp: "2026-04-24T00:00:07",
        },
      })
    })

    await waitFor(() => {
      const hit = result.current.logs.find(
        l => l.message.includes("[INTEGRATION]") && l.message.includes("gerrit_url"),
      )
      expect(hit).toBeTruthy()
      expect(hit?.level).toBe("info")
    })
  })

  it("does not patch tasks/agents/notifications (scope is log-only)", async () => {
    goOnline()
    const sse = primeSSE()
    const { result } = renderHook(() => useEngine())
    await waitFor(() => expect(api.subscribeEvents).toHaveBeenCalled())

    const beforeTasks = [...result.current.tasks]
    const beforeAgents = [...result.current.agents]
    const beforeNotifs = [...result.current.notifications]
    const beforeUnread = result.current.unreadCount

    act(() => {
      sse.emit({
        event: "integration.settings.updated",
        data: {
          fields_changed: ["notification_jira_url"],
          timestamp: "2026-04-24T00:00:08",
        },
      })
    })

    await waitFor(() => {
      expect(
        result.current.logs.some(l => l.message.includes("[INTEGRATION]")),
      ).toBe(true)
    })
    expect(result.current.tasks).toEqual(beforeTasks)
    expect(result.current.agents).toEqual(beforeAgents)
    expect(result.current.notifications).toEqual(beforeNotifs)
    expect(result.current.unreadCount).toBe(beforeUnread)
  })
})
