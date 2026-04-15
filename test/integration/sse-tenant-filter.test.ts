/**
 * I3 — SSE per-tenant filter integration test.
 *
 * Verifies that the shared SSE manager correctly filters events based on
 * _tenant_id and broadcast_scope="tenant".
 */

import { describe, expect, it, vi, beforeEach } from "vitest"

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

;(globalThis as unknown as { EventSource: typeof TrackedEventSource }).EventSource = TrackedEventSource

beforeEach(async () => {
  latestInstance = null
  vi.resetModules()
})

async function importApi() {
  return await import("@/lib/api")
}

describe("SSE per-tenant filter", () => {
  it("tenant-scope events from own tenant are delivered", async () => {
    const api = await importApi()
    api.setCurrentTenantId("t-acme")
    api.setCurrentSessionId("sess-1")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-1",
      _broadcast_scope: "tenant",
      _tenant_id: "t-acme",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("tenant-scope events from another tenant are filtered out", async () => {
    const api = await importApi()
    api.setCurrentTenantId("t-acme")
    api.setCurrentSessionId("sess-1")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a2", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-2",
      _broadcast_scope: "tenant",
      _tenant_id: "t-globex",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("tenant-scope events without _tenant_id are delivered (backward compat)", async () => {
    const api = await importApi()
    api.setCurrentTenantId("t-acme")
    api.setCurrentSessionId("sess-1")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("task_update", {
      task_id: "t1", status: "done", assigned_agent_id: null, timestamp: "t",
      _session_id: "sess-1",
      _broadcast_scope: "tenant",
      _tenant_id: "",
    })

    expect(seen).toEqual(["task_update"])
    sub.close()
  })

  it("when no current tenant is set, all tenant events pass through", async () => {
    const api = await importApi()
    api.setCurrentTenantId(null)
    api.setCurrentSessionId("sess-1")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-1",
      _broadcast_scope: "tenant",
      _tenant_id: "t-globex",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("global-scope events still delivered regardless of tenant", async () => {
    const api = await importApi()
    api.setCurrentTenantId("t-acme")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("heartbeat", {
      subscribers: 1,
      _broadcast_scope: "global",
      _tenant_id: "t-globex",
    })

    expect(seen).toEqual(["heartbeat"])
    sub.close()
  })

  it("multi-tenant fixture: mixed scopes filter correctly", async () => {
    const api = await importApi()
    api.setCurrentTenantId("t-acme")
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      const tid = (ev.data as Record<string, unknown>)._tenant_id ?? ""
      seen.push(`${ev.event}:${tid}`)
    })

    // Global — always delivered
    latestInstance?.fire("heartbeat", {
      subscribers: 2, _broadcast_scope: "global", _tenant_id: "",
    })
    // Tenant-scoped own tenant — delivered
    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-aaa", _broadcast_scope: "tenant", _tenant_id: "t-acme",
    })
    // Tenant-scoped other tenant — filtered
    latestInstance?.fire("agent_update", {
      agent_id: "a2", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-bbb", _broadcast_scope: "tenant", _tenant_id: "t-globex",
    })
    // Session-scoped from own session — delivered
    latestInstance?.fire("task_update", {
      task_id: "t1", status: "done", assigned_agent_id: null, timestamp: "t",
      _session_id: "sess-aaa", _broadcast_scope: "session", _tenant_id: "t-acme",
    })
    // Session-scoped from other session — filtered (session filter)
    latestInstance?.fire("task_update", {
      task_id: "t2", status: "done", assigned_agent_id: null, timestamp: "t",
      _session_id: "sess-bbb", _broadcast_scope: "session", _tenant_id: "t-acme",
    })

    expect(seen).toEqual([
      "heartbeat:",
      "agent_update:t-acme",
      "task_update:t-acme",
    ])

    sub.close()
  })
})
