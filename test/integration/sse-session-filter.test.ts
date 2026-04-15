/**
 * J1 — SSE per-session filter integration test.
 *
 * Verifies that the shared SSE manager correctly filters events based on
 * session_id and broadcast_scope when the filter mode is set to
 * "this_session" vs "all_sessions".
 */

import { describe, expect, it, vi, beforeEach } from "vitest"

let ctorCount = 0
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
    ctorCount++
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
  fireError() {
    this.onerror?.(new Event("error"))
  }
}

;(globalThis as unknown as { EventSource: typeof TrackedEventSource }).EventSource = TrackedEventSource

beforeEach(async () => {
  ctorCount = 0
  latestInstance = null
  vi.resetModules()
})

async function importApi() {
  return await import("@/lib/api")
}

describe("SSE per-session filter", () => {
  it("global-scope events are always delivered regardless of filter mode", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("heartbeat", {
      subscribers: 1,
      _session_id: "sess-bbb",
      _broadcast_scope: "global",
    })

    expect(seen).toEqual(["heartbeat"])
    sub.close()
  })

  it("session-scope events from another session are filtered in this_session mode", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-bbb",
      _broadcast_scope: "session",
    })

    expect(seen).toEqual([])
    sub.close()
  })

  it("session-scope events from own session are delivered in this_session mode", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-aaa",
      _broadcast_scope: "session",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("user-scope events are always delivered in this_session mode", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("token_warning", {
      level: "warn", message: "budget low", usage: 80, budget: 100, timestamp: "t",
      _session_id: "sess-bbb",
      _broadcast_scope: "user",
    })

    expect(seen).toEqual(["token_warning"])
    sub.close()
  })

  it("all_sessions mode delivers session-scope events from other sessions", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("all_sessions")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-bbb",
      _broadcast_scope: "session",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("events without _session_id are always delivered (backward compat)", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("heartbeat", { subscribers: 1 })

    expect(seen).toEqual(["heartbeat"])
    sub.close()
  })

  it("when no current session is set, all events pass through", async () => {
    const api = await importApi()
    api.setCurrentSessionId(null)
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => { seen.push(ev.event) })

    latestInstance?.fire("agent_update", {
      agent_id: "a1", status: "running", thought_chain: "", timestamp: "t",
      _session_id: "sess-bbb",
      _broadcast_scope: "session",
    })

    expect(seen).toEqual(["agent_update"])
    sub.close()
  })

  it("multi-session fixture: two sessions see correct events", async () => {
    const api = await importApi()
    api.setCurrentSessionId("sess-aaa")
    api.setSSEFilterMode("this_session")

    const seen: string[] = []
    const sub = api.subscribeEvents(ev => {
      const sid = (ev.data as Record<string, unknown>)._session_id
      seen.push(`${ev.event}:${sid ?? "none"}`)
    })

    // Global event — always delivered
    latestInstance?.fire("heartbeat", {
      subscribers: 2, _session_id: "", _broadcast_scope: "global",
    })
    // Own session event — delivered
    latestInstance?.fire("task_update", {
      task_id: "t1", status: "done", assigned_agent_id: null, timestamp: "t",
      _session_id: "sess-aaa", _broadcast_scope: "session",
    })
    // Other session event — filtered
    latestInstance?.fire("task_update", {
      task_id: "t2", status: "done", assigned_agent_id: null, timestamp: "t",
      _session_id: "sess-bbb", _broadcast_scope: "session",
    })
    // User-level from other session — delivered
    latestInstance?.fire("token_warning", {
      level: "warn", message: "hi", usage: 0, budget: 0, timestamp: "t",
      _session_id: "sess-bbb", _broadcast_scope: "user",
    })

    expect(seen).toEqual([
      "heartbeat:",
      "task_update:sess-aaa",
      "token_warning:sess-bbb",
    ])

    // Switch to all_sessions mode — now the other session event would pass
    api.setSSEFilterMode("all_sessions")
    latestInstance?.fire("task_update", {
      task_id: "t3", status: "pending", assigned_agent_id: null, timestamp: "t",
      _session_id: "sess-bbb", _broadcast_scope: "session",
    })
    expect(seen).toContain("task_update:sess-bbb")

    sub.close()
  })

  it("filter mode change notifies listeners", async () => {
    const api = await importApi()
    const modes: string[] = []
    const unsub = api.onFilterModeChange(m => modes.push(m))

    api.setSSEFilterMode("all_sessions")
    api.setSSEFilterMode("this_session")

    expect(modes).toEqual(["all_sessions", "this_session"])
    unsub()
  })
})
