/**
 * W14.6 — `<LivePreviewPanel />` unit tests.
 *
 * Coverage matrix (one test per row):
 *
 *   1.  Mount with existing `running` sandbox renders iframe pointed
 *       at `ingress_url`.
 *   2.  Mount with existing `running` sandbox falls back to
 *       `preview_url` when `ingress_url` is null.
 *   3.  Connection LED reflects every status in the WebSandboxStatus
 *       enum.
 *   4.  404 on mount → "Launch preview" CTA renders + click POSTs.
 *   5.  Reload bumps the iframe `src` query string.
 *   6.  External button opens the preview URL in a new tab with
 *       `noopener,noreferrer`.
 *   7.  Kill calls `stopWebSandbox` with `reason="operator_request"`
 *       and reflects the resulting `stopped` snapshot.
 *   8.  Kill on a 404 sandbox clears local state + fires `onClosed`.
 *   9.  Viewport switcher swaps iframe width / height and exposes the
 *       choice via `data-viewport`.
 *  10.  Touch loop fires every `touchIntervalMs`.
 *  11.  Touch loop catches a 404 mid-touch and surfaces the idle
 *       state (sandbox went away while we were watching).
 *  12.  `installing` status renders the body loader instead of the
 *       iframe.
 *  13.  `failed` status surfaces the error + a Retry button that
 *       relaunches.
 *  14.  Generic ApiError on mount renders inline error + RETRY.
 *  15.  Warnings array renders as a footer list.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    kind: string
    status: number
    body: string
    parsed: unknown
    traceId: string | null
    path: string
    method: string
    constructor(args: {
      kind?: string
      status?: number
      body?: string
      message?: string
    } = {}) {
      super(args.message ?? `API ${args.status ?? 0}: ${args.body ?? ""}`)
      this.name = "ApiError"
      this.kind = args.kind ?? "unknown"
      this.status = args.status ?? 0
      this.body = args.body ?? ""
      this.parsed = null
      this.traceId = null
      this.path = ""
      this.method = "GET"
    }
  },
  getWebSandbox: vi.fn(),
  launchWebSandbox: vi.fn(),
  touchWebSandbox: vi.fn(),
  stopWebSandbox: vi.fn(),
}))

import { LivePreviewPanel } from "@/components/omnisight/live-preview-panel"
import * as api from "@/lib/api"
import type {
  WebSandboxInstanceWire,
  WebSandboxStatus,
} from "@/lib/api"

function mkInstance(
  overrides: Partial<WebSandboxInstanceWire> = {},
): WebSandboxInstanceWire {
  return {
    schema_version: "1.0.0",
    workspace_id: "ws-abc123",
    sandbox_id: "ws-deadbeef0001",
    container_name: "omnisight-web-preview-ws-deadbeef0001",
    config: {
      schema_version: "1.0.0",
      workspace_id: "ws-abc123",
      workspace_path: "/workspaces/ws-abc123",
      image_tag: "omnisight-web-preview:dev",
      git_ref: null,
      install_command: ["pnpm", "install", "--frozen-lockfile"],
      dev_command: ["pnpm", "dev", "--host", "0.0.0.0"],
      container_port: 5173,
      env: {},
      preview_url_path: "/",
      startup_timeout_s: 180,
      install_timeout_s: 600,
      log_tail_lines: 200,
      allowed_emails: ["op@example.com"],
    },
    status: "running",
    container_id: "fake-cid-0001",
    host_port: 41001,
    preview_url: "http://127.0.0.1:41001/",
    ingress_url: "https://preview-ws-deadbeef0001.ai.sora-dev.app/",
    access_app_id: "app-uuid-0001",
    created_at: 1714650000,
    started_at: 1714650001,
    ready_at: 1714650020,
    stopped_at: null,
    last_request_at: 1714650020,
    error: null,
    killed_reason: null,
    warnings: [],
    ...overrides,
  }
}

const getWebSandbox = api.getWebSandbox as ReturnType<typeof vi.fn>
const launchWebSandbox = api.launchWebSandbox as ReturnType<typeof vi.fn>
const touchWebSandbox = api.touchWebSandbox as ReturnType<typeof vi.fn>
const stopWebSandbox = api.stopWebSandbox as ReturnType<typeof vi.fn>

const ApiErrorCtor = (api as unknown as { ApiError: new (args: {
  kind?: string; status?: number; body?: string; message?: string
}) => Error }).ApiError

beforeEach(() => {
  vi.clearAllMocks()
  // Stop the touch loop from firing in tests that don't drive timers.
  touchWebSandbox.mockResolvedValue(mkInstance())
})

describe("LivePreviewPanel — happy path", () => {
  it("renders iframe pointed at ingress_url for a running sandbox", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    const iframe = await screen.findByTestId("live-preview-iframe")
    expect(iframe).toHaveAttribute(
      "src",
      expect.stringContaining(
        "https://preview-ws-deadbeef0001.ai.sora-dev.app/",
      ),
    )
    expect(getWebSandbox).toHaveBeenCalledWith("ws-abc123")
    expect(screen.getByTestId("live-preview-status")).toHaveAttribute(
      "data-status",
      "running",
    )
  })

  it("falls back to preview_url when ingress_url is null", async () => {
    getWebSandbox.mockResolvedValue(mkInstance({ ingress_url: null }))
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    const iframe = await screen.findByTestId("live-preview-iframe")
    expect(iframe.getAttribute("src")).toContain("http://127.0.0.1:41001/")
  })

  it.each<WebSandboxStatus>([
    "pending",
    "installing",
    "running",
    "stopping",
    "stopped",
    "failed",
  ])("LED data-status reflects status=%s", async (status) => {
    getWebSandbox.mockResolvedValue(mkInstance({ status }))
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await waitFor(() =>
      expect(screen.getByTestId("live-preview-status")).toHaveAttribute(
        "data-status",
        status,
      ),
    )
    // The LED dot is rendered alongside the badge.
    expect(screen.getByTestId("live-preview-status-led")).toBeInTheDocument()
  })

  it("warnings array renders as a footer list", async () => {
    getWebSandbox.mockResolvedValue(
      mkInstance({ warnings: ["cf_ingress_create_failed: 503"] }),
    )
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await screen.findByTestId("live-preview-iframe")
    expect(screen.getByTestId("live-preview-warnings")).toHaveTextContent(
      "cf_ingress_create_failed: 503",
    )
  })
})

describe("LivePreviewPanel — launch flow", () => {
  it("renders Launch CTA on 404 and calls launchWebSandbox on click", async () => {
    getWebSandbox.mockRejectedValue(
      new ApiErrorCtor({ kind: "not_found", status: 404, body: "" }),
    )
    launchWebSandbox.mockResolvedValue(mkInstance({ status: "installing" }))
    const user = userEvent.setup()
    render(
      <LivePreviewPanel
        workspaceId="ws-abc123"
        workspacePath="/workspaces/ws-abc123"
      />,
    )
    const launchBtn = await screen.findByTestId("live-preview-launch")
    await user.click(launchBtn)
    expect(launchWebSandbox).toHaveBeenCalledWith({
      workspace_id: "ws-abc123",
      workspace_path: "/workspaces/ws-abc123",
    })
    await waitFor(() =>
      expect(screen.getByTestId("live-preview-installing")).toBeInTheDocument(),
    )
  })
})

describe("LivePreviewPanel — toolbar actions", () => {
  it("Reload bumps the iframe src query string", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    const iframe = await screen.findByTestId("live-preview-iframe")
    const before = iframe.getAttribute("src")
    await user.click(screen.getByTestId("live-preview-reload"))
    const after = (await screen.findByTestId("live-preview-iframe")).getAttribute(
      "src",
    )
    expect(after).not.toBe(before)
    expect(after).toMatch(/__omnisight_reload=1/)
  })

  it("External opens preview URL in a new tab with noopener,noreferrer", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null)
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await screen.findByTestId("live-preview-iframe")
    await user.click(screen.getByTestId("live-preview-external"))
    expect(openSpy).toHaveBeenCalledWith(
      "https://preview-ws-deadbeef0001.ai.sora-dev.app/",
      "_blank",
      "noopener,noreferrer",
    )
  })

  it("Kill calls stopWebSandbox with operator_request reason", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    stopWebSandbox.mockResolvedValue(
      mkInstance({
        status: "stopped",
        killed_reason: "operator_request",
        ingress_url: null,
        preview_url: null,
      }),
    )
    const onClosed = vi.fn()
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" onClosed={onClosed} />)
    await screen.findByTestId("live-preview-iframe")
    await user.click(screen.getByTestId("live-preview-kill"))
    expect(stopWebSandbox).toHaveBeenCalledWith("ws-abc123", {
      reason: "operator_request",
    })
    await waitFor(() =>
      expect(screen.getByTestId("live-preview-stopped")).toBeInTheDocument(),
    )
    expect(onClosed).toHaveBeenCalledTimes(1)
  })

  it("Kill on already-removed sandbox clears state and fires onClosed", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    stopWebSandbox.mockRejectedValue(
      new ApiErrorCtor({ kind: "not_found", status: 404, body: "" }),
    )
    const onClosed = vi.fn()
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" onClosed={onClosed} />)
    await screen.findByTestId("live-preview-iframe")
    await user.click(screen.getByTestId("live-preview-kill"))
    await waitFor(() =>
      expect(screen.getByTestId("live-preview-idle")).toBeInTheDocument(),
    )
    expect(onClosed).toHaveBeenCalledTimes(1)
  })
})

describe("LivePreviewPanel — viewport simulator", () => {
  it("clicking Mobile sets viewport=mobile and 375x667 iframe size", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await screen.findByTestId("live-preview-iframe")
    await user.click(screen.getByTestId("live-preview-viewport-mobile"))
    const wrap = screen.getByTestId("live-preview-viewport")
    expect(wrap).toHaveAttribute("data-viewport", "mobile")
    const iframe = screen.getByTestId("live-preview-iframe")
    expect(iframe.style.width).toBe("375px")
    expect(iframe.style.height).toBe("667px")
  })

  it("clicking Tablet swaps to 768x1024", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await screen.findByTestId("live-preview-iframe")
    await user.click(screen.getByTestId("live-preview-viewport-tablet"))
    const iframe = screen.getByTestId("live-preview-iframe")
    expect(iframe.style.width).toBe("768px")
    expect(iframe.style.height).toBe("1024px")
  })

  it("Auto stretches the iframe to 100%", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await screen.findByTestId("live-preview-iframe")
    // mobile then auto to ensure auto resets pixel sizes
    await user.click(screen.getByTestId("live-preview-viewport-mobile"))
    await user.click(screen.getByTestId("live-preview-viewport-auto"))
    const iframe = screen.getByTestId("live-preview-iframe")
    expect(iframe.style.width).toBe("100%")
    expect(iframe.style.height).toBe("100%")
    expect(screen.getByTestId("live-preview-viewport")).toHaveAttribute(
      "data-viewport",
      "auto",
    )
  })
})

describe("LivePreviewPanel — touch loop (W14.5 idle reaper defence)", () => {
  // Real timers + a short interval keeps the test deterministic without
  // wrestling with fake-timers vs. `userEvent.setup()` / `findByTestId`
  // (both internally rely on real timers for retry budgets).
  it("fires touchWebSandbox at touchIntervalMs cadence", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    touchWebSandbox.mockResolvedValue(mkInstance())
    render(
      <LivePreviewPanel workspaceId="ws-abc123" touchIntervalMs={40} />,
    )
    await screen.findByTestId("live-preview-iframe")
    await waitFor(
      () => expect(touchWebSandbox).toHaveBeenCalledTimes(1),
      { timeout: 1000 },
    )
    await waitFor(
      () => expect(touchWebSandbox.mock.calls.length).toBeGreaterThanOrEqual(2),
      { timeout: 1000 },
    )
  })

  it("touch 404 mid-flight returns the panel to the idle CTA", async () => {
    getWebSandbox.mockResolvedValue(mkInstance())
    touchWebSandbox.mockRejectedValue(
      new ApiErrorCtor({ kind: "not_found", status: 404, body: "" }),
    )
    render(
      <LivePreviewPanel workspaceId="ws-abc123" touchIntervalMs={40} />,
    )
    await screen.findByTestId("live-preview-iframe")
    await waitFor(
      () => expect(screen.getByTestId("live-preview-idle")).toBeInTheDocument(),
      { timeout: 1000 },
    )
  })
})

describe("LivePreviewPanel — non-running lifecycle states", () => {
  it("installing status renders the body loader", async () => {
    getWebSandbox.mockResolvedValue(mkInstance({ status: "installing" }))
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await waitFor(() =>
      expect(screen.getByTestId("live-preview-installing")).toBeInTheDocument(),
    )
    expect(screen.queryByTestId("live-preview-iframe")).not.toBeInTheDocument()
  })

  it("failed status surfaces error + retry that relaunches", async () => {
    getWebSandbox.mockResolvedValue(
      mkInstance({ status: "failed", error: "docker run failed: image missing" }),
    )
    launchWebSandbox.mockResolvedValue(mkInstance())
    const user = userEvent.setup()
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    const failedPanel = await screen.findByTestId("live-preview-failed")
    expect(failedPanel).toHaveTextContent("docker run failed: image missing")
    await user.click(screen.getByTestId("live-preview-retry-failed"))
    expect(launchWebSandbox).toHaveBeenCalledWith({
      workspace_id: "ws-abc123",
      workspace_path: null,
    })
  })

  it("generic ApiError on mount renders inline error with RETRY", async () => {
    const user = userEvent.setup()
    getWebSandbox.mockRejectedValueOnce(new Error("API 500: boom"))
    getWebSandbox.mockResolvedValueOnce(mkInstance())
    render(<LivePreviewPanel workspaceId="ws-abc123" />)
    await screen.findByText(/API 500: boom/)
    await user.click(screen.getByRole("button", { name: /Retry/i }))
    await screen.findByTestId("live-preview-iframe")
    expect(getWebSandbox).toHaveBeenCalledTimes(2)
  })
})
