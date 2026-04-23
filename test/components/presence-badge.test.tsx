import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  getSessionsPresence: vi.fn(),
}))

const mockUseAuth = vi.fn()
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => mockUseAuth(),
}))

import { PresenceBadge } from "@/components/omnisight/presence-badge"
import * as api from "@/lib/api"

const AUTHED = {
  user: { id: "u1", email: "alice@example.com", role: "operator" },
  authMode: "session",
  loading: false,
}

const OPEN_MODE = {
  user: null,
  authMode: "open",
  loading: false,
}

const PRESENCE_THREE: api.PresenceResponse = {
  active_count: 3,
  window_seconds: 60,
  now: 1_700_000_000,
  devices: [
    {
      session_id: "sid-current",
      token_hint: "abc1***xyz1",
      device_name: "Chrome on macOS",
      ua_hash: "h1",
      last_heartbeat_at: 1_700_000_000,
      idle_seconds: 1.2,
      status: "active",
      is_current: true,
    },
    {
      session_id: "sid-firefox",
      token_hint: "def2***uvw2",
      device_name: "Firefox on Linux",
      ua_hash: "h2",
      last_heartbeat_at: 1_699_999_995,
      idle_seconds: 5.0,
      status: "active",
      is_current: false,
    },
    {
      session_id: "sid-safari",
      token_hint: "ghi3***rst3",
      device_name: "Safari on iOS",
      ua_hash: "h3",
      last_heartbeat_at: 1_699_999_955,
      idle_seconds: 45.0,
      status: "idle",
      is_current: false,
    },
  ],
}

describe("PresenceBadge", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUseAuth.mockReturnValue(AUTHED)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("renders nothing in auth_mode=open", async () => {
    mockUseAuth.mockReturnValue(OPEN_MODE)
    const { container } = render(<PresenceBadge />)
    expect(container.firstChild).toBeNull()
    expect(api.getSessionsPresence).not.toHaveBeenCalled()
  })

  it("renders active count badge from /auth/sessions/presence", async () => {
    ;(api.getSessionsPresence as ReturnType<typeof vi.fn>).mockResolvedValue(PRESENCE_THREE)

    render(<PresenceBadge />)
    await waitFor(() =>
      expect(screen.getByTestId("presence-badge-count")).toHaveTextContent("3")
    )
  })

  it("opens hover popover with one row per device", async () => {
    const user = userEvent.setup()
    ;(api.getSessionsPresence as ReturnType<typeof vi.fn>).mockResolvedValue(PRESENCE_THREE)

    render(<PresenceBadge />)
    await waitFor(() =>
      expect(screen.getByTestId("presence-badge-count")).toHaveTextContent("3")
    )

    await user.hover(screen.getByTestId("presence-badge"))

    expect(screen.getByTestId("presence-badge-popover")).toBeInTheDocument()
    expect(screen.getByTestId("presence-row-sid-current")).toBeInTheDocument()
    expect(screen.getByTestId("presence-row-sid-firefox")).toBeInTheDocument()
    expect(screen.getByTestId("presence-row-sid-safari")).toBeInTheDocument()
    expect(screen.getByText("Chrome on macOS")).toBeInTheDocument()
    expect(screen.getByText("Firefox on Linux")).toBeInTheDocument()
    expect(screen.getByText("Safari on iOS")).toBeInTheDocument()
    expect(screen.getByTestId("presence-row-current-sid-current")).toHaveTextContent(
      "This device"
    )
  })

  it("shows zero count + empty list when no devices", async () => {
    ;(api.getSessionsPresence as ReturnType<typeof vi.fn>).mockResolvedValue({
      active_count: 0,
      window_seconds: 60,
      now: 1_700_000_000,
      devices: [],
    } as api.PresenceResponse)

    const user = userEvent.setup()
    render(<PresenceBadge />)
    await waitFor(() =>
      expect(screen.getByTestId("presence-badge-count")).toHaveTextContent("0")
    )

    // Hover should not auto-open the popover when count is 0; click does.
    await user.click(screen.getByTestId("presence-badge-button"))
    expect(screen.getByText("No active devices")).toBeInTheDocument()
  })

  it("renders idle status row with idle styling label", async () => {
    ;(api.getSessionsPresence as ReturnType<typeof vi.fn>).mockResolvedValue(PRESENCE_THREE)

    const user = userEvent.setup()
    render(<PresenceBadge />)
    await waitFor(() =>
      expect(screen.getByTestId("presence-badge-count")).toHaveTextContent("3")
    )
    await user.hover(screen.getByTestId("presence-badge"))

    const idleRow = screen.getByTestId("presence-row-sid-safari")
    expect(idleRow).toHaveTextContent("idle")
  })

  it("polls /auth/sessions/presence on an interval", async () => {
    vi.useFakeTimers()
    ;(api.getSessionsPresence as ReturnType<typeof vi.fn>).mockResolvedValue(PRESENCE_THREE)

    render(<PresenceBadge />)
    // Initial fetch fires synchronously inside the mount effect.
    await vi.waitFor(() => expect(api.getSessionsPresence).toHaveBeenCalledTimes(1))

    await act(async () => {
      vi.advanceTimersByTime(15_000)
    })
    await vi.waitFor(() => expect(api.getSessionsPresence).toHaveBeenCalledTimes(2))
  })

  it("surfaces fetch errors inside the popover", async () => {
    ;(api.getSessionsPresence as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("network down")
    )

    const user = userEvent.setup()
    render(<PresenceBadge />)
    await waitFor(() =>
      expect(api.getSessionsPresence).toHaveBeenCalled()
    )

    await user.click(screen.getByTestId("presence-badge-button"))
    await waitFor(() =>
      expect(screen.getByTestId("presence-badge-error")).toHaveTextContent("network down")
    )
  })
})
