/**
 * V0 #6 — SSE workspace-type filter contract tests.
 *
 * Pins the behaviour of `_shouldDeliverEvent` in `lib/api.ts` for
 * events that carry a `_workspace_type` envelope field:
 *
 *   - Events with a matching `_workspace_type` are delivered to the
 *     corresponding workspace's SSE subscribers.
 *   - Events with a mismatched `_workspace_type` are dropped.
 *   - When no workspace is attached (the command-center dashboard),
 *     every `_workspace_type`-bearing event is dropped — that is
 *     the "don't pollute the command center" rule from the V0 spec.
 *   - Events without `_workspace_type` flow through the existing
 *     session / tenant / scope gates unchanged (backward compat with
 *     the J1 session filter and I3 tenant filter).
 *
 * Mirrors the `TrackedEventSource` harness used by
 * `sse-session-filter.test.ts` and `sse-tenant-filter.test.ts` so
 * the three filter suites exercise the same wire-level fixture.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"

let latestInstance: TrackedEventSource | null = null

class TrackedEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSED = 2
  url: string
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  private listeners: Record<string, Array<(e: Event) => void>> = {}

  constructor(url: string) {
    this.url = url
    latestInstance = this // eslint-disable-line @typescript-eslint/no-this-alias
  }
  addEventListener(type: string, listener: (e: Event) => void) {
    ;(this.listeners[type] ||= []).push(listener)
  }
  removeEventListener(type: string, listener: (e: Event) => void) {
    this.listeners[type] = (this.listeners[type] || []).filter(l => l !== listener)
  }
  close() {
    this.readyState = 2
    this.listeners = {}
  }
  fire(type: string, data: unknown) {
    if (this.readyState === 2) return
    const ev = new MessageEvent(type, { data: JSON.stringify(data) })
    for (const l of this.listeners[type] || []) l(ev)
  }
}

;(globalThis as unknown as { EventSource: typeof TrackedEventSource }).EventSource =
  TrackedEventSource

beforeEach(async () => {
  latestInstance = null
  vi.resetModules()
})

async function importApi() {
  return await import("@/lib/api")
}

describe("SSE workspace-type filter — getter / setter surface", () => {
  it("setCurrentWorkspaceType / getCurrentWorkspaceType roundtrip", async () => {
    const api = await importApi()

    expect(api.getCurrentWorkspaceType()).toBeNull()

    api.setCurrentWorkspaceType("web")
    expect(api.getCurrentWorkspaceType()).toBe("web")

    api.setCurrentWorkspaceType("mobile")
    expect(api.getCurrentWorkspaceType()).toBe("mobile")

    api.setCurrentWorkspaceType("software")
    expect(api.getCurrentWorkspaceType()).toBe("software")

    api.setCurrentWorkspaceType(null)
    expect(api.getCurrentWorkspaceType()).toBeNull()
  })
})

describe("SSE workspace-type filter — matching delivery", () => {
  it("web workspace receives web-scoped agent_update events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a1",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("mobile workspace receives mobile-scoped tool_progress events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("mobile")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("tool_progress", {
      tool_name: "build",
      phase: "start",
      output: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })

    expect(seen).toEqual(["tool_progress"])
    sub.close()
  })

  it("software workspace receives software-scoped task_update events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("software")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("task_update", {
      task_id: "t1",
      status: "done",
      assigned_agent_id: null,
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "software",
    })

    expect(seen).toEqual(["task_update"])
    sub.close()
  })
})

describe("SSE workspace-type filter — mismatch rejection", () => {
  it("web workspace drops mobile-scoped events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a2",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("mobile workspace drops software-scoped events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("mobile")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a3",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "software",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("software workspace drops web-scoped events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("software")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a4",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })

    expect(seen).toEqual([])
    sub.close()
  })
})

describe("SSE workspace-type filter — command-center isolation", () => {
  it("command center (no workspace) drops ALL _workspace_type events", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType(null)

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    // All three workspace types — none should reach the dashboard.
    latestInstance?.fire("agent_update", {
      agent_id: "a1",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a2",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a3",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "software",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("command center still receives global events without _workspace_type", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType(null)

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("heartbeat", {
      subscribers: 3,
      _broadcast_scope: "global",
    })
    latestInstance?.fire("mode_changed", {
      mode: "autonomous",
      previous: "supervised",
      parallel_cap: 4,
      in_flight: 1,
      over_cap: 0,
      timestamp: "t",
      _broadcast_scope: "global",
    })

    expect(seen).toEqual(["heartbeat", "mode_changed"])
    sub.close()
  })
})

describe("SSE workspace-type filter — backward compat", () => {
  it("events without _workspace_type pass through all workspaces unchanged", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("heartbeat", {
      subscribers: 1,
      _broadcast_scope: "global",
    })

    expect(seen).toEqual(["heartbeat"])
    sub.close()
  })

  it("empty-string _workspace_type is treated as absent (no filtering)", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("mobile")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a1",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })
})

describe("SSE workspace-type filter — composition with session/tenant gates", () => {
  it("workspace match + own session = delivered", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("task_update", {
      task_id: "t1",
      status: "done",
      assigned_agent_id: null,
      timestamp: "t",
      _broadcast_scope: "session",
      _session_id: "sess-aaa",
      _workspace_type: "web",
    })

    expect(seen).toEqual(["task_update"])
    sub.close()
  })

  it("workspace match + other session = filtered by session gate", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("task_update", {
      task_id: "t2",
      status: "done",
      assigned_agent_id: null,
      timestamp: "t",
      _broadcast_scope: "session",
      _session_id: "sess-bbb",
      _workspace_type: "web",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("workspace mismatch + own session = filtered by workspace gate", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("task_update", {
      task_id: "t3",
      status: "done",
      assigned_agent_id: null,
      timestamp: "t",
      _broadcast_scope: "session",
      _session_id: "sess-aaa",
      _workspace_type: "mobile",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("workspace match + own tenant = delivered", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("software")
    api.setCurrentTenantId("t-acme")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a1",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "tenant",
      _tenant_id: "t-acme",
      _workspace_type: "software",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("workspace mismatch + own tenant = filtered by workspace gate", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("software")
    api.setCurrentTenantId("t-acme")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      seen.push(ev.event)
    })

    latestInstance?.fire("agent_update", {
      agent_id: "a2",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "tenant",
      _tenant_id: "t-acme",
      _workspace_type: "mobile",
    })

    expect(seen).toEqual([])
    sub.close()
  })
})

describe("SSE workspace-type filter — multi-workspace fixture", () => {
  it("three workspaces + command center see only their own events", async () => {
    // Same wire, four subscribers — each mimics a different surface.
    const api = await importApi()

    // Start in command-center mode and fire all four kinds of event.
    api.setCurrentWorkspaceType(null)

    const seenByCommandCenter: string[] = []
    const ccSub = api.subscribeEvents(ev => {
      const ws = (ev.data as Record<string, unknown>)._workspace_type ?? ""
      seenByCommandCenter.push(`${ev.event}:${ws}`)
    })

    latestInstance?.fire("heartbeat", {
      subscribers: 4,
      _broadcast_scope: "global",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-web",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-mobile",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-software",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "software",
    })

    // Command center sees heartbeat only.
    expect(seenByCommandCenter).toEqual(["heartbeat:"])
    ccSub.close()

    // Switch to web and re-fire.
    api.setCurrentWorkspaceType("web")
    const seenByWeb: string[] = []
    const webSub = api.subscribeEvents(ev => {
      const ws = (ev.data as Record<string, unknown>)._workspace_type ?? ""
      seenByWeb.push(`${ev.event}:${ws}`)
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-web",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-mobile",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })
    expect(seenByWeb).toEqual(["agent_update:web"])
    webSub.close()

    // Switch to mobile and re-fire.
    api.setCurrentWorkspaceType("mobile")
    const seenByMobile: string[] = []
    const mobileSub = api.subscribeEvents(ev => {
      const ws = (ev.data as Record<string, unknown>)._workspace_type ?? ""
      seenByMobile.push(`${ev.event}:${ws}`)
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-mobile",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })
    latestInstance?.fire("agent_update", {
      agent_id: "a-software",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "software",
    })
    expect(seenByMobile).toEqual(["agent_update:mobile"])
    mobileSub.close()
  })
})

describe("SSE workspace-type filter — switching workspace at runtime", () => {
  it("changing workspace redirects subsequent event delivery", async () => {
    const api = await importApi()
    api.setCurrentWorkspaceType("web")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      const ws = (ev.data as Record<string, unknown>)._workspace_type ?? ""
      seen.push(`${ev.event}:${ws}`)
    })

    // Fire a web event — delivered.
    latestInstance?.fire("agent_update", {
      agent_id: "a1",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })
    // Fire a mobile event — filtered.
    latestInstance?.fire("agent_update", {
      agent_id: "a2",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })
    // Switch to mobile — now the next mobile event should land.
    api.setCurrentWorkspaceType("mobile")
    latestInstance?.fire("agent_update", {
      agent_id: "a3",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "mobile",
    })
    // And a web event is now filtered.
    latestInstance?.fire("agent_update", {
      agent_id: "a4",
      status: "running",
      thought_chain: "",
      timestamp: "t",
      _broadcast_scope: "global",
      _workspace_type: "web",
    })

    expect(seen).toEqual(["agent_update:web", "agent_update:mobile"])
    sub.close()
  })
})
