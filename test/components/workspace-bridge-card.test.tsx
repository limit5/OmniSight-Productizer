/**
 * V0 #5 — Contract tests for `components/omnisight/workspace-bridge-card.tsx`.
 *
 * Covers:
 *   - Three workspace rows always rendered in order (web / mobile / software).
 *   - Header active-count and data attributes.
 *   - Per-row agent status / preview status / project name / timestamp.
 *   - Default "active" predicate classifies running / paused / error as active.
 *   - Override `isWorkspaceActive` for custom taste.
 *   - Each row is a Link to `/workspace/<type>` and fires `onNavigate`.
 *   - Relative timestamp helper (`formatRelativeSince`).
 *   - Controlled mode: `workspaces` prop is authoritative; missing types
 *     are filled with empty rows; no storage reads.
 *   - Uncontrolled mode: hydrates from localStorage on mount.
 *   - Uncontrolled mode: fetches backend snapshot and uses the newer one.
 *   - `disableBackendSync` path: no fetch; storage-only.
 *   - Helpers: `emptyWorkspaceSummary`, `summariseEnvelope`, `defaultIsWorkspaceActive`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import * as React from "react"

import {
  WorkspaceBridgeCard,
  defaultIsWorkspaceActive,
  emptyWorkspaceSummary,
  formatRelativeSince,
  summariseEnvelope,
  type WorkspaceSummary,
} from "@/components/omnisight/workspace-bridge-card"
import {
  WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
  saveWorkspaceSnapshotToStorage,
  workspaceStorageKey,
} from "@/hooks/use-workspace-persistence"
import { WORKSPACE_TYPES, type WorkspaceType } from "@/app/workspace/[type]/layout"

// ─── Helpers ───────────────────────────────────────────────────────────────

const FIXED_NOW = Date.parse("2026-04-18T12:00:00.000Z")
const pinnedNow = () => FIXED_NOW

function makeSummary(
  type: WorkspaceType,
  overrides: {
    projectId?: string | null
    projectName?: string | null
    projectUpdatedAt?: string | null
    agentStatus?: WorkspaceSummary["agentSession"]["status"]
    agentId?: string | null
    sessionId?: string | null
    startedAt?: string | null
    lastEventAt?: string | null
    previewStatus?: WorkspaceSummary["preview"]["status"]
    previewUrl?: string | null
    previewErrorMessage?: string | null
    previewUpdatedAt?: string | null
    savedAt?: string | null
  } = {},
): WorkspaceSummary {
  return {
    type,
    project: {
      id: overrides.projectId ?? null,
      name: overrides.projectName ?? null,
      updatedAt: overrides.projectUpdatedAt ?? null,
    },
    agentSession: {
      sessionId: overrides.sessionId ?? null,
      agentId: overrides.agentId ?? null,
      status: overrides.agentStatus ?? "idle",
      startedAt: overrides.startedAt ?? null,
      lastEventAt: overrides.lastEventAt ?? null,
    },
    preview: {
      status: overrides.previewStatus ?? "idle",
      url: overrides.previewUrl ?? null,
      errorMessage: overrides.previewErrorMessage ?? null,
      updatedAt: overrides.previewUpdatedAt ?? null,
    },
    savedAt: overrides.savedAt ?? null,
  }
}

function envelope(
  savedAt: string,
  state: Partial<WorkspaceSummary["project"] | unknown>,
): {
  schemaVersion: number
  savedAt: string
  state: Record<string, unknown>
} {
  return {
    schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
    savedAt,
    state: state as Record<string, unknown>,
  }
}

function envResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

function noContent(): Response {
  return new Response(null, { status: 204 })
}

// ─── Teardown ──────────────────────────────────────────────────────────────

beforeEach(() => {
  if (typeof window !== "undefined" && window.localStorage) {
    window.localStorage.clear()
  }
})

afterEach(() => {
  if (typeof window !== "undefined" && window.localStorage) {
    window.localStorage.clear()
  }
  vi.unstubAllGlobals()
})

// ─── Pure helper tests ─────────────────────────────────────────────────────

describe("emptyWorkspaceSummary", () => {
  it.each(WORKSPACE_TYPES)("returns idle defaults for %s", (type) => {
    const s = emptyWorkspaceSummary(type)
    expect(s.type).toBe(type)
    expect(s.project).toEqual({ id: null, name: null, updatedAt: null })
    expect(s.agentSession.status).toBe("idle")
    expect(s.agentSession.sessionId).toBeNull()
    expect(s.preview.status).toBe("idle")
    expect(s.savedAt).toBeNull()
  })

  it("returns a fresh object each call (not a shared reference)", () => {
    const a = emptyWorkspaceSummary("web")
    const b = emptyWorkspaceSummary("web")
    expect(a).not.toBe(b)
    expect(a.project).not.toBe(b.project)
    a.project.name = "dirty"
    expect(b.project.name).toBeNull()
  })
})

describe("summariseEnvelope", () => {
  it("returns empty summary when envelope is null", () => {
    const s = summariseEnvelope("mobile", null)
    expect(s).toEqual(emptyWorkspaceSummary("mobile"))
  })

  it("merges project / agentSession / preview fields from the envelope", () => {
    const s = summariseEnvelope("web", {
      schemaVersion: 1,
      savedAt: "2026-04-18T10:00:00.000Z",
      state: {
        project: { id: "p-1", name: "Alpha", updatedAt: "2026-04-18T09:55:00.000Z" },
        agentSession: {
          sessionId: "s-1",
          agentId: "agent-a",
          status: "running",
          startedAt: "2026-04-18T09:00:00.000Z",
          lastEventAt: "2026-04-18T11:59:00.000Z",
        },
        preview: {
          status: "ready",
          url: "http://preview.local/alpha",
          errorMessage: null,
          updatedAt: "2026-04-18T10:00:00.000Z",
        },
      },
    })
    expect(s.project.name).toBe("Alpha")
    expect(s.agentSession.status).toBe("running")
    expect(s.preview.status).toBe("ready")
    expect(s.savedAt).toBe("2026-04-18T10:00:00.000Z")
  })

  it("fills missing sub-states with defaults (partial snapshot)", () => {
    const s = summariseEnvelope("software", {
      schemaVersion: 1,
      savedAt: "2026-04-18T10:00:00.000Z",
      state: { project: { id: "p-2", name: "Beta", updatedAt: null } },
    })
    expect(s.project.name).toBe("Beta")
    expect(s.agentSession.status).toBe("idle")
    expect(s.preview.status).toBe("idle")
  })

  it("does not mutate the frozen default objects", () => {
    summariseEnvelope("web", {
      schemaVersion: 1,
      savedAt: "2026-04-18T10:00:00.000Z",
      state: { project: { id: "p-x", name: "X", updatedAt: null } },
    })
    const fresh = emptyWorkspaceSummary("web")
    expect(fresh.project.name).toBeNull()
  })
})

describe("defaultIsWorkspaceActive", () => {
  it("treats running / paused / error as active", () => {
    for (const s of ["running", "paused", "error"] as const) {
      expect(defaultIsWorkspaceActive(makeSummary("web", { agentStatus: s }))).toBe(true)
    }
  })

  it("treats idle / done as inactive", () => {
    for (const s of ["idle", "done"] as const) {
      expect(defaultIsWorkspaceActive(makeSummary("web", { agentStatus: s }))).toBe(false)
    }
  })
})

describe("formatRelativeSince", () => {
  it("returns em-dash for null or invalid input", () => {
    expect(formatRelativeSince(null, FIXED_NOW)).toBe("—")
    expect(formatRelativeSince("not-a-date", FIXED_NOW)).toBe("—")
  })

  it("returns 'just now' for <1m", () => {
    const t = new Date(FIXED_NOW - 10_000).toISOString()
    expect(formatRelativeSince(t, FIXED_NOW)).toBe("just now")
  })

  it("returns minutes for <1h", () => {
    const t = new Date(FIXED_NOW - 5 * 60_000).toISOString()
    expect(formatRelativeSince(t, FIXED_NOW)).toBe("5m ago")
  })

  it("returns hours for <1d", () => {
    const t = new Date(FIXED_NOW - 3 * 60 * 60_000).toISOString()
    expect(formatRelativeSince(t, FIXED_NOW)).toBe("3h ago")
  })

  it("returns days for ≥1d", () => {
    const t = new Date(FIXED_NOW - 2 * 24 * 60 * 60_000).toISOString()
    expect(formatRelativeSince(t, FIXED_NOW)).toBe("2d ago")
  })
})

// ─── Render contract tests (controlled mode) ───────────────────────────────

describe("WorkspaceBridgeCard — controlled render", () => {
  it("renders all three workspace rows in canonical order", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    const list = screen.getByTestId("workspace-bridge-list")
    const rows = within(list).getAllByRole("listitem")
    expect(rows.map((r) => r.getAttribute("data-workspace-type"))).toEqual([
      "web",
      "mobile",
      "software",
    ])
  })

  it("fills missing workspace types with empty rows when controlled", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[makeSummary("mobile", { projectName: "Only Mobile" })]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    expect(screen.getByTestId("workspace-bridge-project-web").textContent).toBe(
      "No project loaded",
    )
    expect(screen.getByTestId("workspace-bridge-project-mobile").textContent).toBe(
      "Only Mobile",
    )
    expect(screen.getByTestId("workspace-bridge-project-software").textContent).toBe(
      "No project loaded",
    )
  })

  it("computes active count = running + paused + error", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[
          makeSummary("web", { agentStatus: "running" }),
          makeSummary("mobile", { agentStatus: "paused" }),
          makeSummary("software", { agentStatus: "idle" }),
        ]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    expect(
      screen.getByTestId("workspace-bridge-active-count").textContent,
    ).toBe("2")
    expect(
      screen.getByTestId("workspace-bridge-total-count").textContent,
    ).toBe("3")
    expect(
      screen.getByTestId("workspace-bridge-card").getAttribute("data-active-count"),
    ).toBe("2")
  })

  it("summary text reads 'N / 3 workspaces running'", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[makeSummary("web", { agentStatus: "running" })]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    expect(
      screen.getByTestId("workspace-bridge-summary").textContent,
    ).toMatch(/^1\s*\/\s*3\s+workspaces running$/)
  })

  it("uses custom title", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[]}
        disableBackendSync
        nowMs={pinnedNow}
        title="Active Workspaces"
      />,
    )
    expect(screen.getByText("Active Workspaces")).toBeInTheDocument()
  })

  it("accepts custom isWorkspaceActive predicate", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[
          makeSummary("web", { agentStatus: "done" }),
          makeSummary("mobile", { agentStatus: "idle" }),
          makeSummary("software", { agentStatus: "running" }),
        ]}
        disableBackendSync
        nowMs={pinnedNow}
        isWorkspaceActive={(w) => w.agentSession.status === "done"}
      />,
    )
    expect(
      screen.getByTestId("workspace-bridge-active-count").textContent,
    ).toBe("1")
  })

  it("renders status label, preview label, last-event text, and project name", () => {
    const ts = new Date(FIXED_NOW - 5 * 60_000).toISOString()
    render(
      <WorkspaceBridgeCard
        workspaces={[
          makeSummary("web", {
            projectName: "Landing Page",
            agentStatus: "running",
            previewStatus: "ready",
            lastEventAt: ts,
          }),
        ]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    expect(screen.getByTestId("workspace-bridge-status-web").textContent).toBe(
      "Running",
    )
    expect(screen.getByTestId("workspace-bridge-preview-web").textContent).toBe(
      "Preview ready",
    )
    expect(
      screen.getByTestId("workspace-bridge-last-event-web").textContent,
    ).toBe("5m ago")
    expect(
      screen.getByTestId("workspace-bridge-project-web").textContent,
    ).toBe("Landing Page")
  })

  it("stamps data-active + data-agent-status + data-preview-status per row", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[
          makeSummary("web", { agentStatus: "error", previewStatus: "error" }),
        ]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    const row = screen.getByTestId("workspace-bridge-row-web")
    expect(row.getAttribute("data-active")).toBe("true")
    expect(row.getAttribute("data-agent-status")).toBe("error")
    expect(row.getAttribute("data-preview-status")).toBe("error")
  })

  it("renders '—' for last-event when session has no lastEventAt", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[makeSummary("web")]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    expect(
      screen.getByTestId("workspace-bridge-last-event-web").textContent,
    ).toBe("—")
  })
})

// ─── Navigation ────────────────────────────────────────────────────────────

describe("WorkspaceBridgeCard — navigation", () => {
  it("each row links to /workspace/<type>", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    for (const t of WORKSPACE_TYPES) {
      const link = screen.getByTestId(`workspace-bridge-link-${t}`)
      expect(link.getAttribute("href")).toBe(`/workspace/${t}`)
    }
  })

  it("fires onNavigate with the workspace type when a row is clicked", () => {
    const onNavigate = vi.fn()
    render(
      <WorkspaceBridgeCard
        workspaces={[]}
        disableBackendSync
        nowMs={pinnedNow}
        onNavigate={onNavigate}
      />,
    )
    fireEvent.click(screen.getByTestId("workspace-bridge-link-mobile"))
    expect(onNavigate).toHaveBeenCalledWith("mobile")
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })

  it("link aria-label includes workspace + agent status", () => {
    render(
      <WorkspaceBridgeCard
        workspaces={[makeSummary("software", { agentStatus: "running" })]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    const link = screen.getByTestId("workspace-bridge-link-software")
    expect(link.getAttribute("aria-label")).toMatch(/Software/)
    expect(link.getAttribute("aria-label")).toMatch(/Running/)
  })
})

// ─── Uncontrolled mode — storage hydration ─────────────────────────────────

describe("WorkspaceBridgeCard — uncontrolled (localStorage)", () => {
  it("hydrates from localStorage on mount when no workspaces prop", async () => {
    saveWorkspaceSnapshotToStorage(
      "web",
      {
        project: { id: "p-1", name: "Storage Web", updatedAt: null },
        agentSession: {
          sessionId: "s-1",
          agentId: "a",
          status: "running",
          startedAt: null,
          lastEventAt: null,
        },
        preview: { status: "idle", url: null, errorMessage: null, updatedAt: null },
      },
      { savedAt: "2026-04-18T10:00:00.000Z" },
    )

    render(
      <WorkspaceBridgeCard disableBackendSync nowMs={pinnedNow} />,
    )

    await waitFor(() => {
      expect(
        screen.getByTestId("workspace-bridge-project-web").textContent,
      ).toBe("Storage Web")
    })
    expect(
      screen.getByTestId("workspace-bridge-status-web").textContent,
    ).toBe("Running")
    expect(
      screen.getByTestId("workspace-bridge-active-count").textContent,
    ).toBe("1")
  })

  it("does not read storage when controlled (workspaces prop supplied)", async () => {
    saveWorkspaceSnapshotToStorage(
      "web",
      {
        project: { id: "p-1", name: "Storage Wins?", updatedAt: null },
      },
      { savedAt: "2026-04-18T10:00:00.000Z" },
    )
    render(
      <WorkspaceBridgeCard
        workspaces={[makeSummary("web", { projectName: "Prop Wins" })]}
        disableBackendSync
        nowMs={pinnedNow}
      />,
    )
    await waitFor(() => {
      expect(
        screen.getByTestId("workspace-bridge-project-web").textContent,
      ).toBe("Prop Wins")
    })
    // All other slots remain default
    expect(
      screen.getByTestId("workspace-bridge-project-mobile").textContent,
    ).toBe("No project loaded")
  })

  it("tolerates a corrupt localStorage payload (falls back to default row)", async () => {
    window.localStorage.setItem(workspaceStorageKey("web"), "{not json")
    render(<WorkspaceBridgeCard disableBackendSync nowMs={pinnedNow} />)
    // Should render without throwing; web row stays idle.
    await waitFor(() => {
      expect(
        screen.getByTestId("workspace-bridge-status-web").textContent,
      ).toBe("Idle")
    })
  })

  it("does not call fetch when disableBackendSync=true", async () => {
    const fetchSpy = vi.fn(async () => noContent())
    render(
      <WorkspaceBridgeCard
        disableBackendSync
        fetchImpl={fetchSpy as unknown as typeof fetch}
        nowMs={pinnedNow}
      />,
    )
    // Let the effect settle.
    await act(async () => {
      await Promise.resolve()
    })
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

// ─── Uncontrolled mode — backend sync ──────────────────────────────────────

describe("WorkspaceBridgeCard — uncontrolled (backend)", () => {
  it("fetches /api/workspace/<type>/session for all three types on mount", async () => {
    const calls: string[] = []
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString()
      calls.push(url)
      return noContent()
    })
    render(
      <WorkspaceBridgeCard
        fetchImpl={fetchImpl as unknown as typeof fetch}
        nowMs={pinnedNow}
      />,
    )
    await waitFor(() => {
      expect(fetchImpl).toHaveBeenCalledTimes(3)
    })
    expect(calls.sort()).toEqual([
      "/api/workspace/mobile/session",
      "/api/workspace/software/session",
      "/api/workspace/web/session",
    ])
  })

  it("hydrates from a newer backend envelope, overriding storage", async () => {
    saveWorkspaceSnapshotToStorage(
      "web",
      {
        project: { id: "p-old", name: "Stored", updatedAt: null },
      },
      { savedAt: "2026-04-18T09:00:00.000Z" },
    )

    const backendEnvelope = {
      schemaVersion: 1,
      savedAt: "2026-04-18T11:00:00.000Z",
      state: {
        project: { id: "p-new", name: "Backend Wins", updatedAt: null },
        agentSession: {
          sessionId: "s-b",
          agentId: "a-b",
          status: "paused",
          startedAt: null,
          lastEventAt: null,
        },
      },
    }
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString()
      if (url === "/api/workspace/web/session") return envResp(backendEnvelope)
      return noContent()
    })
    render(
      <WorkspaceBridgeCard
        fetchImpl={fetchImpl as unknown as typeof fetch}
        nowMs={pinnedNow}
      />,
    )
    await waitFor(() => {
      expect(
        screen.getByTestId("workspace-bridge-project-web").textContent,
      ).toBe("Backend Wins")
    })
    expect(
      screen.getByTestId("workspace-bridge-status-web").textContent,
    ).toBe("Paused")
  })

  it("keeps the localStorage row when backend savedAt is older", async () => {
    saveWorkspaceSnapshotToStorage(
      "mobile",
      {
        project: { id: "p-new", name: "Storage Wins", updatedAt: null },
        agentSession: {
          sessionId: "s-s",
          agentId: "a",
          status: "running",
          startedAt: null,
          lastEventAt: null,
        },
      },
      { savedAt: "2026-04-18T11:30:00.000Z" },
    )
    const olderBackend = {
      schemaVersion: 1,
      savedAt: "2026-04-18T09:00:00.000Z",
      state: {
        project: { id: "p-old", name: "Backend Loses", updatedAt: null },
      },
    }
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString()
      if (url === "/api/workspace/mobile/session") return envResp(olderBackend)
      return noContent()
    })
    render(
      <WorkspaceBridgeCard
        fetchImpl={fetchImpl as unknown as typeof fetch}
        nowMs={pinnedNow}
      />,
    )
    // Wait for fetch to settle before asserting "Storage Wins" is stable.
    await waitFor(() => {
      expect(fetchImpl).toHaveBeenCalled()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(
      screen.getByTestId("workspace-bridge-project-mobile").textContent,
    ).toBe("Storage Wins")
  })

  it("tolerates network failure without throwing", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error("offline")
    })
    render(
      <WorkspaceBridgeCard
        fetchImpl={fetchImpl as unknown as typeof fetch}
        nowMs={pinnedNow}
      />,
    )
    await waitFor(() => {
      expect(fetchImpl).toHaveBeenCalledTimes(3)
    })
    // Should still render the three default rows.
    expect(
      screen.getAllByRole("listitem").map((n) => n.getAttribute("data-workspace-type")),
    ).toEqual(["web", "mobile", "software"])
    expect(
      screen.getByTestId("workspace-bridge-active-count").textContent,
    ).toBe("0")
  })
})
