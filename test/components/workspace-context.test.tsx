/**
 * V0 #3 — Contract tests for `components/omnisight/workspace-context.tsx`.
 *
 * Covers:
 *   - Default state shape (project / agentSession / preview) and `type`
 *     wiring for each of the three workspace types.
 *   - `useWorkspaceContext()` throws outside the provider (no silent global
 *     fallback — the command-center state must stay isolated).
 *   - Partial-merge semantics for all three setters.
 *   - `null`-reset semantics for all three setters.
 *   - `resetWorkspace()` returns to defaults across all three sub-states.
 *   - `initialState` hydration (V0 #4 persistence seam).
 *   - Isolation: two sibling providers (different types) do not share state.
 *   - Nested provider: inner scope overrides outer for its subtree.
 *   - `updatedAt` auto-bump on project / preview setters.
 *   - Provider rejects unknown workspace types (typo guard).
 *   - Exposed defaults are frozen (immutable).
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen, fireEvent, act } from "@testing-library/react"
import * as React from "react"

import {
  WorkspaceProvider,
  useWorkspaceContext,
  useWorkspaceType,
  defaultWorkspaceState,
  DEFAULT_PROJECT_STATE,
  DEFAULT_AGENT_SESSION_STATE,
  DEFAULT_PREVIEW_STATE,
  type WorkspaceContextValue,
} from "@/components/omnisight/workspace-context"
import { WORKSPACE_TYPES, type WorkspaceType } from "@/app/workspace/[type]/layout"

// ─── Helpers ───────────────────────────────────────────────────────────────

function Probe({ onCtx }: { onCtx: (ctx: WorkspaceContextValue) => void }) {
  const ctx = useWorkspaceContext()
  onCtx(ctx)
  return (
    <div data-testid="probe">
      <span data-testid="probe-type">{ctx.type}</span>
      <span data-testid="probe-project-id">{ctx.project.id ?? ""}</span>
      <span data-testid="probe-project-name">{ctx.project.name ?? ""}</span>
      <span data-testid="probe-agent-status">{ctx.agentSession.status}</span>
      <span data-testid="probe-agent-session-id">{ctx.agentSession.sessionId ?? ""}</span>
      <span data-testid="probe-preview-status">{ctx.preview.status}</span>
      <span data-testid="probe-preview-url">{ctx.preview.url ?? ""}</span>
    </div>
  )
}

function captureCtx() {
  const ref: { current: WorkspaceContextValue | null } = { current: null }
  const onCtx = (c: WorkspaceContextValue) => { ref.current = c }
  return { ref, onCtx }
}

// ─── defaultWorkspaceState ────────────────────────────────────────────────

describe("defaultWorkspaceState", () => {
  it.each(WORKSPACE_TYPES)("returns fresh defaults for %s", (type) => {
    const s = defaultWorkspaceState(type)
    expect(s.type).toBe(type)
    expect(s.project).toEqual(DEFAULT_PROJECT_STATE)
    expect(s.agentSession).toEqual(DEFAULT_AGENT_SESSION_STATE)
    expect(s.preview).toEqual(DEFAULT_PREVIEW_STATE)
  })

  it("returns a fresh object each call (no shared mutable state)", () => {
    const a = defaultWorkspaceState("web")
    const b = defaultWorkspaceState("web")
    expect(a).not.toBe(b)
    expect(a.project).not.toBe(b.project)
    expect(a.agentSession).not.toBe(b.agentSession)
    expect(a.preview).not.toBe(b.preview)
  })

  it("exposed DEFAULT_* constants are frozen so callers can't mutate them", () => {
    expect(Object.isFrozen(DEFAULT_PROJECT_STATE)).toBe(true)
    expect(Object.isFrozen(DEFAULT_AGENT_SESSION_STATE)).toBe(true)
    expect(Object.isFrozen(DEFAULT_PREVIEW_STATE)).toBe(true)
  })
})

// ─── useWorkspaceContext — guard ──────────────────────────────────────────

describe("useWorkspaceContext (outside provider)", () => {
  it("throws a descriptive error when no provider wraps the tree", () => {
    // Swallow the console.error that React emits for the thrown render.
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {})
    expect(() => render(<Probe onCtx={() => {}} />)).toThrow(
      /useWorkspaceContext must be used inside <WorkspaceProvider>/,
    )
    errSpy.mockRestore()
  })
})

// ─── Provider — type wiring + defaults ────────────────────────────────────

describe("WorkspaceProvider — defaults + type wiring", () => {
  it.each(WORKSPACE_TYPES)("stamps type=%s and seeds default state", (type) => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type={type}>
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    expect(ref.current!.type).toBe(type)
    expect(ref.current!.project).toEqual(DEFAULT_PROJECT_STATE)
    expect(ref.current!.agentSession).toEqual(DEFAULT_AGENT_SESSION_STATE)
    expect(ref.current!.preview).toEqual(DEFAULT_PREVIEW_STATE)
    expect(screen.getByTestId("probe-type").textContent).toBe(type)
    expect(screen.getByTestId("probe-agent-status").textContent).toBe("idle")
    expect(screen.getByTestId("probe-preview-status").textContent).toBe("idle")
  })

  it("throws for unknown workspace types", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {})
    expect(() =>
      render(
        // @ts-expect-error — deliberately wrong type to hit the guard
        <WorkspaceProvider type="desktop">
          <Probe onCtx={() => {}} />
        </WorkspaceProvider>,
      ),
    ).toThrow(/unknown workspace type "desktop"/)
    errSpy.mockRestore()
  })
})

// ─── setProject ───────────────────────────────────────────────────────────

describe("WorkspaceProvider — setProject", () => {
  it("merges partial updates and auto-bumps updatedAt", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="web">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() => ref.current!.setProject({ id: "p-1", name: "Alpha" }))
    expect(ref.current!.project.id).toBe("p-1")
    expect(ref.current!.project.name).toBe("Alpha")
    expect(typeof ref.current!.project.updatedAt).toBe("string")
    expect(() => new Date(ref.current!.project.updatedAt!).toISOString()).not.toThrow()
  })

  it("honours caller-supplied updatedAt when provided", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="web">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    const fixed = "2020-01-01T00:00:00.000Z"
    act(() => ref.current!.setProject({ id: "p-9", updatedAt: fixed }))
    expect(ref.current!.project.updatedAt).toBe(fixed)
  })

  it("null resets project back to defaults", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="web">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() => ref.current!.setProject({ id: "p-1", name: "Alpha" }))
    act(() => ref.current!.setProject(null))
    expect(ref.current!.project).toEqual(DEFAULT_PROJECT_STATE)
  })

  it("consecutive partial merges accumulate fields", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="web">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() => ref.current!.setProject({ id: "p-1" }))
    act(() => ref.current!.setProject({ name: "Beta" }))
    expect(ref.current!.project.id).toBe("p-1")
    expect(ref.current!.project.name).toBe("Beta")
  })
})

// ─── setAgentSession ──────────────────────────────────────────────────────

describe("WorkspaceProvider — setAgentSession", () => {
  it("merges partial updates including status transitions", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="mobile">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() =>
      ref.current!.setAgentSession({
        sessionId: "sess-42",
        agentId: "ui-designer",
        status: "running",
        startedAt: "2026-04-17T12:00:00.000Z",
      }),
    )
    expect(ref.current!.agentSession.sessionId).toBe("sess-42")
    expect(ref.current!.agentSession.agentId).toBe("ui-designer")
    expect(ref.current!.agentSession.status).toBe("running")
    expect(ref.current!.agentSession.startedAt).toBe("2026-04-17T12:00:00.000Z")
  })

  it("null resets the agent session to idle defaults", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="mobile">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() =>
      ref.current!.setAgentSession({ sessionId: "sess-42", status: "running" }),
    )
    act(() => ref.current!.setAgentSession(null))
    expect(ref.current!.agentSession).toEqual(DEFAULT_AGENT_SESSION_STATE)
  })

  it("does NOT auto-bump updatedAt (agent session uses lastEventAt)", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="mobile">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() => ref.current!.setAgentSession({ status: "running" }))
    // lastEventAt should stay null unless caller supplied one explicitly.
    expect(ref.current!.agentSession.lastEventAt).toBeNull()
  })
})

// ─── setPreviewState ──────────────────────────────────────────────────────

describe("WorkspaceProvider — setPreviewState", () => {
  it("merges preview updates and auto-bumps updatedAt", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="software">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() =>
      ref.current!.setPreviewState({
        status: "ready",
        url: "https://preview.test/xyz",
      }),
    )
    expect(ref.current!.preview.status).toBe("ready")
    expect(ref.current!.preview.url).toBe("https://preview.test/xyz")
    expect(typeof ref.current!.preview.updatedAt).toBe("string")
  })

  it("null resets preview to idle defaults", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="software">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() =>
      ref.current!.setPreviewState({
        status: "ready",
        url: "https://preview.test/xyz",
      }),
    )
    act(() => ref.current!.setPreviewState(null))
    expect(ref.current!.preview).toEqual(DEFAULT_PREVIEW_STATE)
  })

  it("surfaces error messages for error status", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="software">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() =>
      ref.current!.setPreviewState({
        status: "error",
        errorMessage: "build failed: missing dep",
      }),
    )
    expect(ref.current!.preview.status).toBe("error")
    expect(ref.current!.preview.errorMessage).toBe("build failed: missing dep")
  })
})

// ─── resetWorkspace ───────────────────────────────────────────────────────

describe("WorkspaceProvider — resetWorkspace", () => {
  it("clears all three sub-states back to defaults in one call", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="web">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() => {
      ref.current!.setProject({ id: "p-1", name: "Alpha" })
      ref.current!.setAgentSession({ sessionId: "sess-1", status: "running" })
      ref.current!.setPreviewState({ status: "ready", url: "https://x" })
    })
    act(() => ref.current!.resetWorkspace())
    expect(ref.current!.project).toEqual(DEFAULT_PROJECT_STATE)
    expect(ref.current!.agentSession).toEqual(DEFAULT_AGENT_SESSION_STATE)
    expect(ref.current!.preview).toEqual(DEFAULT_PREVIEW_STATE)
  })

  it("retains the workspace type (type is structural, not per-session state)", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider type="mobile">
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    act(() => ref.current!.resetWorkspace())
    expect(ref.current!.type).toBe("mobile")
  })
})

// ─── initialState hydration (V0 #4 seam) ──────────────────────────────────

describe("WorkspaceProvider — initialState hydration", () => {
  it("seeds project / agentSession / preview from initialState", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider
        type="web"
        initialState={{
          project: { id: "p-seed", name: "Seeded", updatedAt: "2026-04-17T00:00:00.000Z" },
          agentSession: {
            sessionId: "sess-seed",
            agentId: "ui-designer",
            status: "paused",
            startedAt: "2026-04-17T00:00:00.000Z",
            lastEventAt: "2026-04-17T00:00:01.000Z",
          },
          preview: {
            status: "ready",
            url: "https://seeded.test",
            errorMessage: null,
            updatedAt: "2026-04-17T00:00:02.000Z",
          },
        }}
      >
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    expect(ref.current!.project.id).toBe("p-seed")
    expect(ref.current!.project.name).toBe("Seeded")
    expect(ref.current!.agentSession.sessionId).toBe("sess-seed")
    expect(ref.current!.agentSession.status).toBe("paused")
    expect(ref.current!.preview.url).toBe("https://seeded.test")
  })

  it("fills absent initialState fields with defaults (partial seed allowed)", () => {
    const { ref, onCtx } = captureCtx()
    render(
      <WorkspaceProvider
        type="web"
        initialState={{ project: { id: "only-id" } as { id: string } }}
      >
        <Probe onCtx={onCtx} />
      </WorkspaceProvider>,
    )
    expect(ref.current!.project.id).toBe("only-id")
    expect(ref.current!.project.name).toBeNull()
    expect(ref.current!.project.updatedAt).toBeNull()
    // Untouched sub-states still equal defaults.
    expect(ref.current!.agentSession).toEqual(DEFAULT_AGENT_SESSION_STATE)
    expect(ref.current!.preview).toEqual(DEFAULT_PREVIEW_STATE)
  })
})

// ─── Isolation between workspaces ─────────────────────────────────────────

describe("WorkspaceProvider — isolation", () => {
  it("two sibling providers (different types) have independent state", () => {
    const webRef = captureCtx()
    const mobileRef = captureCtx()
    render(
      <>
        <WorkspaceProvider type="web">
          <Probe onCtx={webRef.onCtx} />
        </WorkspaceProvider>
        <WorkspaceProvider type="mobile">
          <Probe onCtx={mobileRef.onCtx} />
        </WorkspaceProvider>
      </>,
    )
    act(() => webRef.ref.current!.setProject({ id: "web-proj" }))
    act(() => mobileRef.ref.current!.setProject({ id: "mob-proj" }))
    expect(webRef.ref.current!.project.id).toBe("web-proj")
    expect(mobileRef.ref.current!.project.id).toBe("mob-proj")
    // Cross-check: neither leaks into the other.
    expect(webRef.ref.current!.type).toBe("web")
    expect(mobileRef.ref.current!.type).toBe("mobile")
  })

  it("nested providers: inner scope wins for its subtree", () => {
    const outerRef = captureCtx()
    const innerRef = captureCtx()
    render(
      <WorkspaceProvider type="web">
        <Probe onCtx={outerRef.onCtx} />
        <WorkspaceProvider type="software">
          <Probe onCtx={innerRef.onCtx} />
        </WorkspaceProvider>
      </WorkspaceProvider>,
    )
    expect(outerRef.ref.current!.type).toBe("web")
    expect(innerRef.ref.current!.type).toBe("software")
    act(() => innerRef.ref.current!.setProject({ id: "inner" }))
    expect(innerRef.ref.current!.project.id).toBe("inner")
    // Outer scope is untouched by inner mutations.
    expect(outerRef.ref.current!.project.id).toBeNull()
  })
})

// ─── useWorkspaceType convenience hook ────────────────────────────────────

describe("useWorkspaceType", () => {
  function TypeOnly() {
    const type = useWorkspaceType()
    return <span data-testid="only-type">{type}</span>
  }

  it.each(WORKSPACE_TYPES)("returns the provider type for %s", (type: WorkspaceType) => {
    render(
      <WorkspaceProvider type={type}>
        <TypeOnly />
      </WorkspaceProvider>,
    )
    expect(screen.getByTestId("only-type").textContent).toBe(type)
  })
})

// ─── End-to-end click flow (sanity) ───────────────────────────────────────

describe("WorkspaceProvider — integration via interactive consumer", () => {
  function Interactive() {
    const ctx = useWorkspaceContext()
    return (
      <div>
        <button
          data-testid="btn-run"
          onClick={() =>
            ctx.setAgentSession({ sessionId: "s-1", status: "running" })
          }
        >
          run
        </button>
        <button
          data-testid="btn-ready"
          onClick={() =>
            ctx.setPreviewState({ status: "ready", url: "https://p/1" })
          }
        >
          ready
        </button>
        <button data-testid="btn-reset" onClick={() => ctx.resetWorkspace()}>
          reset
        </button>
        <span data-testid="agent-status">{ctx.agentSession.status}</span>
        <span data-testid="preview-status">{ctx.preview.status}</span>
        <span data-testid="preview-url">{ctx.preview.url ?? ""}</span>
      </div>
    )
  }

  it("click flow: run → preview ready → reset wipes both", () => {
    render(
      <WorkspaceProvider type="web">
        <Interactive />
      </WorkspaceProvider>,
    )
    fireEvent.click(screen.getByTestId("btn-run"))
    expect(screen.getByTestId("agent-status").textContent).toBe("running")

    fireEvent.click(screen.getByTestId("btn-ready"))
    expect(screen.getByTestId("preview-status").textContent).toBe("ready")
    expect(screen.getByTestId("preview-url").textContent).toBe("https://p/1")

    fireEvent.click(screen.getByTestId("btn-reset"))
    expect(screen.getByTestId("agent-status").textContent).toBe("idle")
    expect(screen.getByTestId("preview-status").textContent).toBe("idle")
    expect(screen.getByTestId("preview-url").textContent).toBe("")
  })
})
