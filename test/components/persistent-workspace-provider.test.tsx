/**
 * V0 #4 — Integration tests for `PersistentWorkspaceProvider`.
 *
 * Exercises the composition of:
 *   - `hooks/use-workspace-persistence.ts` (load/save utilities)
 *   - `components/omnisight/workspace-context.tsx` (V0 #3 provider)
 * inside the wrapper component.
 *
 * The backend fetch (`global.fetch`) is stubbed per-test with the
 * shapes the real `/api/workspace/[type]/session` route returns.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import * as React from "react"

import { PersistentWorkspaceProvider } from "@/components/omnisight/persistent-workspace-provider"
import {
  useWorkspaceContext,
  type WorkspaceContextValue,
} from "@/components/omnisight/workspace-context"
import {
  WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
  loadWorkspaceSnapshotFromStorage,
  saveWorkspaceSnapshotToStorage,
  workspaceStorageKey,
} from "@/hooks/use-workspace-persistence"

// ─── Test probes ───────────────────────────────────────────────────────────

function Probe({ onCtx }: { onCtx?: (c: WorkspaceContextValue) => void }) {
  const ctx = useWorkspaceContext()
  onCtx?.(ctx)
  return (
    <div data-testid="probe">
      <span data-testid="project-id">{ctx.project.id ?? ""}</span>
      <span data-testid="project-name">{ctx.project.name ?? ""}</span>
      <span data-testid="agent-status">{ctx.agentSession.status}</span>
      <span data-testid="agent-session-id">{ctx.agentSession.sessionId ?? ""}</span>
      <span data-testid="preview-status">{ctx.preview.status}</span>
      <span data-testid="preview-url">{ctx.preview.url ?? ""}</span>
      <button
        data-testid="btn-set-project"
        onClick={() => ctx.setProject({ id: "p-live", name: "Live" })}
      >
        set-project
      </button>
      <button
        data-testid="btn-run"
        onClick={() => ctx.setAgentSession({ sessionId: "s-live", status: "running" })}
      >
        run
      </button>
      <button
        data-testid="btn-ready"
        onClick={() => ctx.setPreviewState({ status: "ready", url: "http://live.local" })}
      >
        ready
      </button>
      <button data-testid="btn-reset" onClick={() => ctx.resetWorkspace()}>
        reset
      </button>
    </div>
  )
}

function mockFetchWithResponses(
  responses: Array<Response | Promise<Response>>,
): ReturnType<typeof vi.fn> {
  let i = 0
  const fn = vi.fn(async () => {
    const r = responses[Math.min(i, responses.length - 1)]
    i++
    return r instanceof Promise ? r : r
  })
  vi.stubGlobal("fetch", fn)
  return fn
}

function envResp(envelope: unknown, status = 200): Response {
  return new Response(JSON.stringify(envelope), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

function noSnapshotResp(): Response {
  return new Response(null, { status: 204 })
}

// ─── Setup / teardown ─────────────────────────────────────────────────────

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ─── Tests ─────────────────────────────────────────────────────────────────

describe("PersistentWorkspaceProvider — localStorage seed", () => {
  it("renders defaults when localStorage is empty", async () => {
    mockFetchWithResponses([noSnapshotResp()])
    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    expect(screen.getByTestId("project-id").textContent).toBe("")
    expect(screen.getByTestId("agent-status").textContent).toBe("idle")
    expect(screen.getByTestId("preview-status").textContent).toBe("idle")
  })

  it("hydrates from a prior localStorage snapshot after mount (SSR-safe)", async () => {
    saveWorkspaceSnapshotToStorage(
      "mobile",
      {
        project: { id: "p-seed", name: "Seeded", updatedAt: "2026-04-18T00:00:00Z" },
        agentSession: {
          sessionId: "s-seed",
          agentId: "a-seed",
          status: "running",
          startedAt: "2026-04-18T00:00:00Z",
          lastEventAt: null,
        },
        preview: {
          status: "ready",
          url: "http://seed.local",
          errorMessage: null,
          updatedAt: "2026-04-18T00:00:00Z",
        },
      },
      { savedAt: "2026-04-18T00:00:00Z" },
    )
    mockFetchWithResponses([noSnapshotResp()])

    render(
      <PersistentWorkspaceProvider type="mobile" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    // After the first client effect runs, the seeded state appears.
    // (React Testing Library flushes effects inside `render()`, so the
    // hydration happens before the first assertion can observe
    // pre-hydration state — the defaults→seeded transition is covered
    // at the SSR level by the defer-to-effect pattern, not by jsdom.)
    await waitFor(() => {
      expect(screen.getByTestId("project-id").textContent).toBe("p-seed")
      expect(screen.getByTestId("project-name").textContent).toBe("Seeded")
      expect(screen.getByTestId("agent-status").textContent).toBe("running")
      expect(screen.getByTestId("agent-session-id").textContent).toBe("s-seed")
      expect(screen.getByTestId("preview-status").textContent).toBe("ready")
      expect(screen.getByTestId("preview-url").textContent).toBe("http://seed.local")
    })
  })

  it("ignores a malformed localStorage payload and falls back to defaults", () => {
    localStorage.setItem(workspaceStorageKey("web"), "{not json")
    mockFetchWithResponses([noSnapshotResp()])

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    expect(screen.getByTestId("project-id").textContent).toBe("")
    expect(screen.getByTestId("agent-status").textContent).toBe("idle")
  })

  it("scopes by workspace type (mobile seed doesn't leak into software)", async () => {
    saveWorkspaceSnapshotToStorage(
      "mobile",
      { project: { id: "p-mobile", name: "M", updatedAt: null } },
      { savedAt: "2026-04-18T00:00:00Z" },
    )
    mockFetchWithResponses([noSnapshotResp()])

    render(
      <PersistentWorkspaceProvider type="software" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    // Give the hydrate effect time to run; software has no seed.
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.getByTestId("project-id").textContent).toBe("")
  })
})

describe("PersistentWorkspaceProvider — localStorage write-through", () => {
  it("writes project mutations back to localStorage", async () => {
    mockFetchWithResponses([noSnapshotResp()])
    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    act(() => {
      fireEvent.click(screen.getByTestId("btn-set-project"))
    })

    await waitFor(() => {
      const loaded = loadWorkspaceSnapshotFromStorage("web")
      expect(loaded?.state.project?.id).toBe("p-live")
      expect(loaded?.state.project?.name).toBe("Live")
    })
  })

  it("writes agent-session + preview mutations back to localStorage", async () => {
    mockFetchWithResponses([noSnapshotResp()])
    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    act(() => {
      fireEvent.click(screen.getByTestId("btn-run"))
    })
    act(() => {
      fireEvent.click(screen.getByTestId("btn-ready"))
    })

    await waitFor(() => {
      const loaded = loadWorkspaceSnapshotFromStorage("web")
      expect(loaded?.state.agentSession?.status).toBe("running")
      expect(loaded?.state.agentSession?.sessionId).toBe("s-live")
      expect(loaded?.state.preview?.status).toBe("ready")
      expect(loaded?.state.preview?.url).toBe("http://live.local")
    })
  })

  it("persists the envelope with the current schemaVersion", async () => {
    mockFetchWithResponses([noSnapshotResp()])
    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    act(() => {
      fireEvent.click(screen.getByTestId("btn-set-project"))
    })
    await waitFor(() => {
      const loaded = loadWorkspaceSnapshotFromStorage("web")
      expect(loaded?.schemaVersion).toBe(WORKSPACE_SNAPSHOT_SCHEMA_VERSION)
      expect(typeof loaded?.savedAt).toBe("string")
    })
  })

  it("round-trips state across unmount → remount (simulates tab switch)", async () => {
    mockFetchWithResponses([noSnapshotResp(), noSnapshotResp()])
    const { unmount } = render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    act(() => {
      fireEvent.click(screen.getByTestId("btn-set-project"))
      fireEvent.click(screen.getByTestId("btn-ready"))
    })

    await waitFor(() => {
      expect(loadWorkspaceSnapshotFromStorage("web")?.state.project?.id).toBe("p-live")
    })

    unmount()

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId("project-id").textContent).toBe("p-live")
      expect(screen.getByTestId("preview-status").textContent).toBe("ready")
      expect(screen.getByTestId("preview-url").textContent).toBe("http://live.local")
    })
  })
})

describe("PersistentWorkspaceProvider — backend sync", () => {
  it("GETs /api/workspace/<type>/session on mount", async () => {
    const fetchFn = mockFetchWithResponses([noSnapshotResp()])
    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(fetchFn).toHaveBeenCalled()
    })
    const firstCall = fetchFn.mock.calls[0]
    expect(firstCall[0]).toBe("/api/workspace/web/session")
    expect(firstCall[1]).toMatchObject({ method: "GET" })
  })

  it("hydrates state from the backend when backend savedAt is newer than localStorage", async () => {
    // Seed localStorage with an older snapshot.
    saveWorkspaceSnapshotToStorage(
      "web",
      { project: { id: "old", name: "Old", updatedAt: "2026-04-17T00:00:00Z" } },
      { savedAt: "2026-04-17T00:00:00Z" },
    )
    // Backend returns a newer snapshot.
    const newer = {
      schemaVersion: 1,
      savedAt: "2026-04-18T00:00:00Z",
      state: {
        project: { id: "p-backend", name: "FromBackend", updatedAt: "2026-04-18T00:00:00Z" },
      },
    }
    mockFetchWithResponses([envResp(newer)])

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    // After hydration, backend wins because its savedAt is newer.
    await waitFor(() => {
      expect(screen.getByTestId("project-id").textContent).toBe("p-backend")
      expect(screen.getByTestId("project-name").textContent).toBe("FromBackend")
    })
  })

  it("does NOT overwrite local state when the backend snapshot is older", async () => {
    saveWorkspaceSnapshotToStorage(
      "web",
      { project: { id: "new-local", name: "NewLocal", updatedAt: "2026-04-18T00:00:00Z" } },
      { savedAt: "2026-04-18T00:00:00Z" },
    )
    const older = {
      schemaVersion: 1,
      savedAt: "2026-04-17T00:00:00Z",
      state: {
        project: { id: "stale-backend", name: "Stale", updatedAt: "2026-04-17T00:00:00Z" },
      },
    }
    mockFetchWithResponses([envResp(older)])

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    // Wait for the localStorage seed to apply, then confirm the backend
    // GET (older) does NOT overwrite it.
    await waitFor(() => {
      expect(screen.getByTestId("project-id").textContent).toBe("new-local")
    })
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.getByTestId("project-id").textContent).toBe("new-local")
  })

  it("PUTs the envelope after state mutation", async () => {
    const fetchFn = mockFetchWithResponses([
      noSnapshotResp(), // GET on mount
      new Response(null, { status: 204 }), // PUT
    ])

    render(
      <PersistentWorkspaceProvider type="software" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    act(() => {
      fireEvent.click(screen.getByTestId("btn-set-project"))
    })

    await waitFor(() => {
      const puts = fetchFn.mock.calls.filter(
        (c) => (c[1] as RequestInit)?.method === "PUT",
      )
      expect(puts.length).toBeGreaterThan(0)
    })

    const putCall = fetchFn.mock.calls.find(
      (c) => (c[1] as RequestInit)?.method === "PUT",
    )!
    expect(putCall[0]).toBe("/api/workspace/software/session")
    const body = JSON.parse((putCall[1] as RequestInit).body as string)
    expect(body.schemaVersion).toBe(WORKSPACE_SNAPSHOT_SCHEMA_VERSION)
    expect(body.state.project).toMatchObject({ id: "p-live", name: "Live" })
  })

  it("tolerates a failed backend GET (keeps localStorage seed + no throw)", async () => {
    saveWorkspaceSnapshotToStorage(
      "web",
      { project: { id: "keep-me", name: "Keep", updatedAt: null } },
      { savedAt: "2026-04-18T00:00:00Z" },
    )
    // Simulate network failure
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("ECONNREFUSED")),
    )

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId("project-id").textContent).toBe("keep-me")
    })
  })

  it("does not issue a PUT when disableBackendSync=true, but still writes localStorage", async () => {
    const fetchFn = vi.fn()
    vi.stubGlobal("fetch", fetchFn)

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0} disableBackendSync>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    act(() => {
      fireEvent.click(screen.getByTestId("btn-set-project"))
    })
    await waitFor(() => {
      expect(loadWorkspaceSnapshotFromStorage("web")?.state.project?.id).toBe("p-live")
    })
    expect(fetchFn).not.toHaveBeenCalled()
  })
})

describe("PersistentWorkspaceProvider — reset semantics", () => {
  it("resetWorkspace clears state and writes the reset snapshot to localStorage", async () => {
    saveWorkspaceSnapshotToStorage(
      "web",
      { project: { id: "p-seed", name: "Seed", updatedAt: null } },
      { savedAt: "2026-04-18T00:00:00Z" },
    )
    mockFetchWithResponses([noSnapshotResp()])

    render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId("project-id").textContent).toBe("p-seed")
    })

    act(() => {
      fireEvent.click(screen.getByTestId("btn-reset"))
    })

    expect(screen.getByTestId("project-id").textContent).toBe("")
    expect(screen.getByTestId("agent-status").textContent).toBe("idle")
    expect(screen.getByTestId("preview-status").textContent).toBe("idle")

    await waitFor(() => {
      const loaded = loadWorkspaceSnapshotFromStorage("web")
      expect(loaded?.state.project?.id ?? null).toBeNull()
    })
  })
})

describe("PersistentWorkspaceProvider — V0 #6 SSE workspace-type registration", () => {
  it("registers the workspace type on mount and clears it on unmount", async () => {
    const { getCurrentWorkspaceType, setCurrentWorkspaceType } = await import(
      "@/lib/api"
    )
    setCurrentWorkspaceType(null)
    mockFetchWithResponses([noSnapshotResp()])

    expect(getCurrentWorkspaceType()).toBeNull()

    const { unmount } = render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )

    await waitFor(() => {
      expect(getCurrentWorkspaceType()).toBe("web")
    })

    unmount()
    expect(getCurrentWorkspaceType()).toBeNull()
  })

  it("cleanup does not overwrite a different workspace that took the slot", async () => {
    const { getCurrentWorkspaceType, setCurrentWorkspaceType } = await import(
      "@/lib/api"
    )
    setCurrentWorkspaceType(null)
    mockFetchWithResponses([noSnapshotResp()])

    const { unmount } = render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(getCurrentWorkspaceType()).toBe("web")
    })

    // A route transition races in: the next layout's effect runs
    // before the outgoing layout's cleanup.  Simulate the new
    // registration.
    setCurrentWorkspaceType("mobile")

    // Now the outgoing provider unmounts.  Its cleanup must not
    // wipe the newly-mounted "mobile" registration.
    unmount()
    expect(getCurrentWorkspaceType()).toBe("mobile")

    setCurrentWorkspaceType(null)
  })

  it("switching workspace type re-registers with lib/api", async () => {
    const { getCurrentWorkspaceType, setCurrentWorkspaceType } = await import(
      "@/lib/api"
    )
    setCurrentWorkspaceType(null)
    mockFetchWithResponses([noSnapshotResp(), noSnapshotResp()])

    const { rerender } = render(
      <PersistentWorkspaceProvider type="web" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(getCurrentWorkspaceType()).toBe("web")
    })

    rerender(
      <PersistentWorkspaceProvider type="software" backendDebounceMs={0}>
        <Probe />
      </PersistentWorkspaceProvider>,
    )
    await waitFor(() => {
      expect(getCurrentWorkspaceType()).toBe("software")
    })

    setCurrentWorkspaceType(null)
  })
})
