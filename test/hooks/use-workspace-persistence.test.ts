/**
 * V0 #4 — Contract tests for `hooks/use-workspace-persistence.ts`.
 *
 * Covers the pure utilities: schema guards, localStorage sync, backend
 * fetch/push via injectable `fetchImpl`, and the `pickNewerEnvelope`
 * tiebreaker.  Integration with `WorkspaceProvider` lives in the
 * `persistent-workspace-provider.test.tsx` sibling file.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"

import {
  WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
  clearWorkspaceSnapshotFromStorage,
  fetchWorkspaceSnapshotFromBackend,
  loadWorkspaceSnapshotFromStorage,
  parseWorkspaceEnvelope,
  pickNewerEnvelope,
  pushWorkspaceSnapshotToBackend,
  saveWorkspaceSnapshotToStorage,
  workspaceSessionApiPath,
  workspaceStorageKey,
  type WorkspaceSnapshotEnvelope,
} from "@/hooks/use-workspace-persistence"
import { WORKSPACE_TYPES } from "@/app/workspace/[type]/layout"

function makeEnvelope(savedAt: string): WorkspaceSnapshotEnvelope {
  return {
    schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
    savedAt,
    state: {
      project: { id: "p-1", name: "Demo", updatedAt: savedAt },
    },
  }
}

describe("workspaceStorageKey / workspaceSessionApiPath", () => {
  it("produces a namespaced, type-scoped storage key for every workspace type", () => {
    for (const type of WORKSPACE_TYPES) {
      expect(workspaceStorageKey(type)).toBe(`omnisight:workspace:${type}:session`)
    }
  })

  it("produces the matching REST path per workspace type", () => {
    for (const type of WORKSPACE_TYPES) {
      expect(workspaceSessionApiPath(type)).toBe(`/api/workspace/${type}/session`)
    }
  })
})

describe("parseWorkspaceEnvelope", () => {
  it("returns null for non-object input", () => {
    expect(parseWorkspaceEnvelope(null)).toBeNull()
    expect(parseWorkspaceEnvelope("string")).toBeNull()
    expect(parseWorkspaceEnvelope(42)).toBeNull()
    expect(parseWorkspaceEnvelope([])).toBeNull()
  })

  it("rejects envelopes with a mismatched schemaVersion", () => {
    const raw = { schemaVersion: 99, savedAt: "2026-04-18T00:00:00Z", state: {} }
    expect(parseWorkspaceEnvelope(raw)).toBeNull()
  })

  it("rejects envelopes missing savedAt", () => {
    const raw = { schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION, savedAt: "", state: {} }
    expect(parseWorkspaceEnvelope(raw)).toBeNull()
  })

  it("rejects envelopes with non-object state", () => {
    const raw = { schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION, savedAt: "t", state: 1 }
    expect(parseWorkspaceEnvelope(raw)).toBeNull()
  })

  it("accepts a minimal valid envelope with empty state", () => {
    const raw = { schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION, savedAt: "2026-04-18T00:00:00Z", state: {} }
    expect(parseWorkspaceEnvelope(raw)).toEqual(raw)
  })

  it("keeps valid sub-state objects and drops non-object ones", () => {
    const raw = {
      schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
      savedAt: "2026-04-18T00:00:00Z",
      state: {
        project: { id: "p-1", name: "Demo", updatedAt: null },
        agentSession: null, // dropped
        preview: ["not", "an", "object"], // dropped
      },
    }
    const parsed = parseWorkspaceEnvelope(raw)
    expect(parsed).not.toBeNull()
    expect(parsed!.state.project).toEqual({ id: "p-1", name: "Demo", updatedAt: null })
    expect(parsed!.state.agentSession).toBeUndefined()
    expect(parsed!.state.preview).toBeUndefined()
  })
})

describe("loadWorkspaceSnapshotFromStorage / saveWorkspaceSnapshotToStorage", () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("returns null when nothing has been saved", () => {
    expect(loadWorkspaceSnapshotFromStorage("web")).toBeNull()
  })

  it("roundtrips a valid snapshot", () => {
    const state = { project: { id: "p-2", name: "Mobile App", updatedAt: "2026-04-18T00:00:00Z" } }
    const written = saveWorkspaceSnapshotToStorage("mobile", state, { savedAt: "2026-04-18T01:00:00Z" })
    expect(written).not.toBeNull()
    expect(written!.schemaVersion).toBe(WORKSPACE_SNAPSHOT_SCHEMA_VERSION)
    expect(written!.savedAt).toBe("2026-04-18T01:00:00Z")
    expect(written!.state).toEqual(state)

    const loaded = loadWorkspaceSnapshotFromStorage("mobile")
    expect(loaded).toEqual(written)
  })

  it("defaults savedAt to the current time when the caller omits it", () => {
    const written = saveWorkspaceSnapshotToStorage("web", { project: { id: "p-3", name: "X", updatedAt: null } })
    expect(written).not.toBeNull()
    expect(new Date(written!.savedAt).toString()).not.toBe("Invalid Date")
  })

  it("scopes by workspace type (saving to one doesn't leak to another)", () => {
    saveWorkspaceSnapshotToStorage("web", { project: { id: "p-web", name: "W", updatedAt: null } })
    expect(loadWorkspaceSnapshotFromStorage("mobile")).toBeNull()
    expect(loadWorkspaceSnapshotFromStorage("software")).toBeNull()
  })

  it("returns null if the stored payload is not valid JSON", () => {
    localStorage.setItem(workspaceStorageKey("web"), "{this is not json")
    expect(loadWorkspaceSnapshotFromStorage("web")).toBeNull()
  })

  it("returns null if the stored envelope fails schema validation", () => {
    localStorage.setItem(workspaceStorageKey("web"), JSON.stringify({ schemaVersion: 2 }))
    expect(loadWorkspaceSnapshotFromStorage("web")).toBeNull()
  })

  it("clearWorkspaceSnapshotFromStorage removes the stored envelope", () => {
    saveWorkspaceSnapshotToStorage("web", { project: { id: "p-4", name: "Y", updatedAt: null } })
    expect(loadWorkspaceSnapshotFromStorage("web")).not.toBeNull()
    clearWorkspaceSnapshotFromStorage("web")
    expect(loadWorkspaceSnapshotFromStorage("web")).toBeNull()
  })

  it("save returns null when localStorage.setItem throws (quota / private mode)", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota")
    })
    const written = saveWorkspaceSnapshotToStorage("web", {
      project: { id: "p-5", name: "Z", updatedAt: null },
    })
    expect(written).toBeNull()
    spy.mockRestore()
  })

  it("rejects unknown workspace types (load + save + clear)", () => {
    // @ts-expect-error — "desktop" is not a WorkspaceType; testing runtime guard
    expect(loadWorkspaceSnapshotFromStorage("desktop")).toBeNull()
    // @ts-expect-error — "desktop" is not a WorkspaceType; testing runtime guard
    expect(saveWorkspaceSnapshotToStorage("desktop", { project: { id: "p", name: "n", updatedAt: null } })).toBeNull()
    // @ts-expect-error — "desktop" is not a WorkspaceType; testing runtime guard
    expect(() => clearWorkspaceSnapshotFromStorage("desktop")).not.toThrow()
  })
})

describe("fetchWorkspaceSnapshotFromBackend", () => {
  it("returns null on 204", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ status: 204, ok: false, json: async () => ({}) }) as unknown as typeof fetch
    expect(await fetchWorkspaceSnapshotFromBackend("web", { fetchImpl })).toBeNull()
  })

  it("returns null on 404", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ status: 404, ok: false, json: async () => ({}) }) as unknown as typeof fetch
    expect(await fetchWorkspaceSnapshotFromBackend("web", { fetchImpl })).toBeNull()
  })

  it("returns null on network failure", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error("ECONNREFUSED")) as unknown as typeof fetch
    expect(await fetchWorkspaceSnapshotFromBackend("web", { fetchImpl })).toBeNull()
  })

  it("returns null when backend returns malformed envelope", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      status: 200, ok: true, json: async () => ({ foo: "bar" }),
    }) as unknown as typeof fetch
    expect(await fetchWorkspaceSnapshotFromBackend("web", { fetchImpl })).toBeNull()
  })

  it("returns a parsed envelope on 200", async () => {
    const env = makeEnvelope("2026-04-18T10:00:00Z")
    const fetchImpl = vi.fn().mockResolvedValue({
      status: 200, ok: true, json: async () => env,
    }) as unknown as typeof fetch
    const got = await fetchWorkspaceSnapshotFromBackend("web", { fetchImpl })
    expect(got).toEqual(env)
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/workspace/web/session",
      expect.objectContaining({ method: "GET" }),
    )
  })

  it("rejects unknown workspace types before calling fetch", async () => {
    const fetchImpl = vi.fn() as unknown as typeof fetch
    // @ts-expect-error — "desktop" is not a WorkspaceType; testing runtime guard
    await fetchWorkspaceSnapshotFromBackend("desktop", { fetchImpl })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe("pushWorkspaceSnapshotToBackend", () => {
  it("PUTs the envelope with JSON content-type", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 204 }) as unknown as typeof fetch
    const env = await pushWorkspaceSnapshotToBackend(
      "software",
      { project: { id: "p-6", name: "CLI", updatedAt: null } },
      { savedAt: "2026-04-18T02:00:00Z", fetchImpl },
    )
    expect(env).not.toBeNull()
    expect(env!.state.project).toEqual({ id: "p-6", name: "CLI", updatedAt: null })
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/workspace/software/session",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    )
    const call = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(JSON.parse(call[1].body)).toEqual(env)
  })

  it("returns null on non-ok response", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 500 }) as unknown as typeof fetch
    const env = await pushWorkspaceSnapshotToBackend(
      "software",
      { project: { id: "p-7", name: "X", updatedAt: null } },
      { fetchImpl },
    )
    expect(env).toBeNull()
  })

  it("returns null on network failure", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error("offline")) as unknown as typeof fetch
    const env = await pushWorkspaceSnapshotToBackend(
      "mobile",
      { project: { id: "p-8", name: "X", updatedAt: null } },
      { fetchImpl },
    )
    expect(env).toBeNull()
  })
})

describe("pickNewerEnvelope", () => {
  it("returns b when a is null", () => {
    const b = makeEnvelope("2026-04-18T00:00:00Z")
    expect(pickNewerEnvelope(null, b)).toBe(b)
  })

  it("returns a when b is null", () => {
    const a = makeEnvelope("2026-04-18T00:00:00Z")
    expect(pickNewerEnvelope(a, null)).toBe(a)
  })

  it("returns null when both are null", () => {
    expect(pickNewerEnvelope(null, null)).toBeNull()
  })

  it("returns the envelope with the later savedAt", () => {
    const a = makeEnvelope("2026-04-18T00:00:00Z")
    const b = makeEnvelope("2026-04-18T01:00:00Z")
    expect(pickNewerEnvelope(a, b)).toBe(b)
    expect(pickNewerEnvelope(b, a)).toBe(b)
  })

  it("returns b on exact savedAt tie (last-writer-wins)", () => {
    const a = makeEnvelope("2026-04-18T00:00:00Z")
    const b = makeEnvelope("2026-04-18T00:00:00Z")
    expect(pickNewerEnvelope(a, b)).toBe(b)
  })
})

describe("SSR safety", () => {
  const origWindow = globalThis.window

  beforeEach(() => {
    // Simulate an SSR environment — no `window`.
    // @ts-expect-error — deliberately deleting window
    delete (globalThis as { window?: unknown }).window
  })

  afterEach(() => {
    ;(globalThis as { window?: unknown }).window = origWindow
  })

  it("load returns null when window is undefined (server render)", () => {
    expect(loadWorkspaceSnapshotFromStorage("web")).toBeNull()
  })

  it("save returns null when window is undefined (server render)", () => {
    expect(
      saveWorkspaceSnapshotToStorage("web", { project: { id: "p", name: "n", updatedAt: null } }),
    ).toBeNull()
  })
})
